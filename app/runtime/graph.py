"""LangGraph StateGraph Engine for AGCore."""
import json
from typing import Any, Dict, Literal
from langgraph.graph import StateGraph, END
from app.gateway.client import AIGatewayClient, gateway_client, gateway_for_service
from app.runtime.context import context_manager, is_context_overflow_error
from app.runtime.state import AgentState
from app.runtime.public_grounding import public_outline_calls, public_section_calls, public_chunk_calls, public_grounded_messages
from app.schemas.gateway import GatewayMessage
from app.tools.dispatcher import dispatcher
from app.tools.registry import registry
from app.adapters.document import document_owner_id, tool_token_budget

async def model_node(
    state: AgentState,
    on_delta=None,
    on_reasoning_delta=None,
    *,
    context_budget_override=None,
    reasoning_effort_override=None,
    disable_tools: bool = False,
) -> Dict[str, Any]:
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

    # 1. Dynamic budget resolution and 3-stage compaction
    from app.core.runtime_config import get_effective_route_budget
    client = state.custom_client or gateway_for_service(state.service_id, gateway_client)
    route_budget, has_contract = await get_effective_route_budget(client, state.service_id)
    effective_input_budget = route_budget.input_budget_tokens if has_contract else None
    if context_budget_override is not None:
        effective_input_budget = (
            min(effective_input_budget, int(context_budget_override))
            if effective_input_budget is not None
            else int(context_budget_override)
        )
    pruned_msgs, _report = context_manager.compact_with_report(
        state.messages,
        budget_tokens=effective_input_budget,
        route=state.service_id,
    )
    public_grounding = state.service_id == "hd2ai-public" and bool(state.context_refs)
    if public_grounding:
        max_body_chars = max(1200, int((route_budget.input_budget_tokens if has_contract else context_manager.budget_tokens) * 2))
        pruned_msgs = public_grounded_messages(state, max_body_chars=max_body_chars)
    
    # 2. Get tool definitions for this service
    tools = registry.get_definitions(service=state.service_id)
    first_document_step = bool(state.context_refs) and state.tool_call_count == 0
    if first_document_step:
        tools = [tool for tool in tools if tool.function.name == "document.get_outline"]
    
    # 3. Call Client (with 1-shot reactive compaction recovery on context overflow)
    model_label = None

    def capture_label(label):
        nonlocal model_label
        model_label = label

    label_options = {'on_model_label': capture_label} if isinstance(client, AIGatewayClient) else {}
    allow_tools = (not disable_tools) and (not public_grounding) and state.tool_call_count < state.max_tool_calls
    call_tools = tools if allow_tools else None
    call_choice = ("required" if first_document_step else "auto") if allow_tools else "none"
    effective_effort = reasoning_effort_override if reasoning_effort_override is not None else state.reasoning_effort
    try:
        content, tool_calls, finish_reason = await client.execute(
            messages=pruned_msgs,
            tools=call_tools,
            tool_choice=call_choice,
            stream=state.stream,
            reasoning_effort=effective_effort,
            on_reasoning_delta=on_reasoning_delta,
            on_content_delta=on_delta,
            **label_options,
        )
    except Exception as exc:
        if not is_context_overflow_error(exc):
            raise
        context_manager.bump_calibration_on_overflow(state.service_id)
        pruned_msgs, _ = context_manager.compact_with_report(
            pruned_msgs,
            budget_tokens=effective_input_budget,
            route=state.service_id,
            force_ratio=0.72,
        )
        model_label = None
        content, tool_calls, finish_reason = await client.execute(
            messages=pruned_msgs,
            tools=call_tools,
            tool_choice=call_choice,
            stream=state.stream,
            reasoning_effort=state.reasoning_effort,
            on_reasoning_delta=on_reasoning_delta,
            on_content_delta=on_delta,
            **label_options,
        )
    if public_grounding and not content and not tool_calls:
        model_label = None
        content, tool_calls, finish_reason = await client.execute(
            messages=pruned_msgs,
            tools=None,
            tool_choice="none",
            stream=state.stream,
            reasoning_effort=state.reasoning_effort,
            on_reasoning_delta=on_reasoning_delta,
            on_content_delta=on_delta,
            **label_options,
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
        updates["model_label"] = model_label
        
    return updates

async def tool_node(
    state: AgentState,
    on_tool_event=None,
    *,
    validated_calls=None,
    constraints=None,
    timeout_override=None,
) -> Dict[str, Any]:
    """Executes pending (or strictly ValidatedAction-approved) tool calls and updates message state."""
    calls = list(validated_calls) if validated_calls is not None else list(state.pending_tool_calls)
    approved_ids = {c.id for c in calls}
    unapproved_calls = [c for c in state.pending_tool_calls if c.id not in approved_ids] if validated_calls is not None else []
    new_traces = list(state.traces)
    new_messages = list(state.messages)
    citations = list(state.citations)
    if validated_calls is None and state.context_refs and state.tool_call_count == 0:
        for index, call in enumerate(calls):
            if call.function.name == "document.get_outline":
                call.function.arguments = json.dumps({"doc_id": state.context_refs[min(index, len(state.context_refs) - 1)]})
        if new_messages and new_messages[-1].role == "assistant":
            new_messages[-1].tool_calls = calls
    new_tool_count = state.tool_call_count + len(calls)
    
    if on_tool_event:
        for call in calls:
            await on_tool_event("started", call, state.step_count)

    # Execute batch of calls concurrently with dynamic per-tool token_budget
    from app.core.runtime_config import resolve_route_budget
    active_budget = resolve_route_budget(state.service_id)
    per_tool_budget = context_manager.compute_tool_token_budget(
        state.messages,
        budget_tokens=active_budget.input_budget_tokens if active_budget else None,
        route=state.service_id,
        tool_count=max(1, len(calls)),
    )
    owner_token = document_owner_id.set(state.owner_id)
    budget_token = tool_token_budget.set(per_tool_budget)
    try:
        results = await dispatcher.execute_batch(
            calls=calls,
            step=state.step_count,
            service=state.service_id,
            timeout=timeout_override,
            allowed_document_ids=state.context_refs or None,
            constraints=constraints,
        )
    finally:
        tool_token_budget.reset(budget_token)
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

    # R1-FIX-01.12: Record explicit rejected tool messages for any pending calls excluded from ValidatedAction
    for rej in unapproved_calls:
        new_messages.append(
            GatewayMessage(
                role="tool",
                content=json.dumps({"error": True, "code": "tool_call_not_approved"}, ensure_ascii=False),
                tool_call_id=rej.id,
                name=rej.function.name,
            )
        )

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
