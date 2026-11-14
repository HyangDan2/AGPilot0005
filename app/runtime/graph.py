"""LangGraph StateGraph Engine for AGCore."""
import json
from typing import Any, Dict, Literal
from langgraph.graph import StateGraph, END
from app.gateway.client import gateway_client
from app.runtime.context import context_manager
from app.runtime.state import AgentState
from app.runtime.public_grounding import public_outline_calls, public_section_calls, public_chunk_calls, public_grounded_messages
from app.schemas.gateway import GatewayMessage
from app.tools.dispatcher import dispatcher
from app.tools.registry import registry
from app.adapters.document import document_owner_id

async def model_node(state: AgentState, on_delta=None, on_reasoning_delta=None) -> Dict[str, Any]:
    """Invokes AI Gateway with current messages and tools."""
    state.step_count += 1

    if state.service_id == "hd2ai-public" and state.context_refs and state.tool_call_count == 0:
        calls = public_outline_calls(state)
        return {
            "messages": list(state.messages) + [GatewayMessage(role="assistant", content=None, tool_calls=calls)],
            "step_count": state.step_count,
            "pending_tool_calls": calls,
            "status": "running",
        }
    if state.service_id == "hd2ai-public" and state.context_refs and not any(
        message.role == "tool" and message.name in ("document.read_section", "document.search") for message in state.messages
    ):
        calls = public_section_calls(state)
        if calls:
            return {
                "messages": list(state.messages) + [GatewayMessage(role="assistant", content=None, tool_calls=calls)],
                "step_count": state.step_count,
                "pending_tool_calls": calls,
                "status": "running",
            }
    
    if (state.service_id == 'hd2ai-public' and state.context_refs
            and any(m.role == 'tool' and m.name == 'document.search' for m in state.messages)
            and not any(m.role == 'tool' and m.name == 'document.read_chunk' for m in state.messages)):
        calls = public_chunk_calls(state)
        if calls:
            return {'messages':list(state.messages)+[GatewayMessage(role='assistant',content=None,tool_calls=calls)],
                    'step_count':state.step_count,'pending_tool_calls':calls,'status':'running'}

    # 1. Prune and budget check
    pruned_msgs, trimmed = context_manager.prune_and_compress(state.messages)
    public_grounding = state.service_id == "hd2ai-public" and bool(state.context_refs)
    if public_grounding:
        pruned_msgs = public_grounded_messages(state)
    
    # 2. Get tool definitions for this service
    tools = registry.get_definitions(service=state.service_id)
    first_document_step = bool(state.context_refs) and state.tool_call_count == 0
    if first_document_step:
        tools = [tool for tool in tools if tool.function.name == "document.get_outline"]
    
    # 3. Call Client (custom_client if provided, else default gateway_client)
    client = state.custom_client or gateway_client
    content, tool_calls, finish_reason = await client.execute(
        messages=pruned_msgs,
        tools=tools if not public_grounding and state.tool_call_count < state.max_tool_calls else None,
        tool_choice=("required" if first_document_step else "auto") if not public_grounding and state.tool_call_count < state.max_tool_calls else "none",
        stream=state.stream,
        reasoning_effort=state.reasoning_effort,
        on_reasoning_delta=on_reasoning_delta,
        on_content_delta=on_delta,
    )
    if public_grounding and not content and not tool_calls:
        content, tool_calls, finish_reason = await client.execute(
            messages=pruned_msgs,
            tools=None,
            tool_choice="none",
            stream=state.stream,
            reasoning_effort=state.reasoning_effort,
            on_reasoning_delta=on_reasoning_delta,
            on_content_delta=on_delta,
        )
    if public_grounding and not content and not tool_calls:
        raise RuntimeError("Public document answer was empty")
    
    new_messages = list(state.messages)
    assistant_msg = GatewayMessage(
        role="assistant",
        content=content,
        tool_calls=tool_calls if tool_calls else None,
    )
    new_messages.append(assistant_msg)
    
    updates: Dict[str, Any] = {
        "messages": new_messages,
        "step_count": state.step_count,
        "pending_tool_calls": tool_calls or [],
    }
    
    if tool_calls:
        updates["status"] = "running"
    else:
        updates["status"] = "completed"
        updates["final_answer"] = content or ""
        
    return updates

async def tool_node(state: AgentState, on_tool_event=None) -> Dict[str, Any]:
    """Executes pending tool calls and updates message state."""
    calls = state.pending_tool_calls
    new_traces = list(state.traces)
    new_messages = list(state.messages)
    citations = list(state.citations)
    if state.context_refs and state.tool_call_count == 0:
        for index, call in enumerate(calls):
            if call.function.name == "document.get_outline":
                call.function.arguments = json.dumps({"doc_id": state.context_refs[min(index, len(state.context_refs) - 1)]})
        if new_messages and new_messages[-1].role == "assistant":
            new_messages[-1].tool_calls = calls
    new_tool_count = state.tool_call_count + len(calls)
    
    if on_tool_event:
        for call in calls:
            await on_tool_event("started", call, state.step_count)

    # Execute batch of calls concurrently
    owner_token = document_owner_id.set(state.owner_id)
    try:
        results = await dispatcher.execute_batch(
            calls=calls,
            step=state.step_count,
            service=state.service_id,
            allowed_document_ids=state.context_refs or None,
        )
    finally:
        document_owner_id.reset(owner_token)
    
    for res in results:
        new_traces.append(res.trace)
        if res.name in ('document.read_section','document.read_chunk') and not res.is_error:
            try:
                data = json.loads(res.content);doc_id=data.get('doc_id') or data.get('document_id')
                chunk_id=data.get('chunk_id') or data.get('section_id')
                if doc_id in state.context_refs and chunk_id:
                    key=doc_id+'/'+chunk_id
                    if not any(c['key']==key for c in citations):
                        citations.append({'key':key,'doc_id':doc_id,'chunk_id':chunk_id,
                                          'title':state.document_titles.get(doc_id,'첨부 문서'),
                                          'page':(data.get('source') or {}).get('page')})
            except (ValueError,TypeError,AttributeError):
                pass
        # Append tool message response
        new_messages.append(
            GatewayMessage(
                role="tool",
                content=res.content,
                tool_call_id=res.tool_call_id,
                name=res.name,
            )
        )
        if on_tool_event:
            await on_tool_event("completed", res, state.step_count)
            
    return {
        "messages": new_messages,
        "tool_call_count": new_tool_count,
        'citations': citations,
        "pending_tool_calls": [],
        "traces": new_traces,
        "status": "running",
    }

def should_continue(state: AgentState) -> Literal["tool_node", "end"]:
    # 1. Error state
    if state.error:
        return "end"
    
    # 2. Check limits
    if state.step_count >= state.max_steps:
        return "end"
    
    # 3. Check tool calls
    if state.pending_tool_calls and state.tool_call_count < state.max_tool_calls:
        return "tool_node"
        
    return "end"

def build_agent_graph():
    graph = StateGraph(AgentState)
    
    # Add nodes
    graph.add_node("model_node", model_node)
    graph.add_node("tool_node", tool_node)
    
    # Set entry point
    graph.set_entry_point("model_node")
    
    # Add conditional edge from model_node
    graph.add_conditional_edges(
        "model_node",
        should_continue,
        {
            "tool_node": "tool_node",
            "end": END,
        },
    )
    
    # Tool node always loops back to model_node
    graph.add_edge("tool_node", "model_node")
    
    return graph.compile()

agent_executor = build_agent_graph()
