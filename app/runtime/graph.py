"""LangGraph StateGraph Engine for AGCore."""
from typing import Any, Dict, Literal
from langgraph.graph import StateGraph, END
from app.gateway.client import gateway_client
from app.runtime.context import context_manager
from app.runtime.state import AgentState
from app.schemas.gateway import GatewayMessage
from app.tools.dispatcher import dispatcher
from app.tools.registry import registry

async def model_node(state: AgentState, on_delta=None) -> Dict[str, Any]:
    """Invokes AI Gateway with current messages and tools."""
    state.step_count += 1
    
    # 1. Prune and budget check
    pruned_msgs, trimmed = context_manager.prune_and_compress(state.messages)
    
    # 2. Get tool definitions for this service
    tools = registry.get_definitions(service=state.service_id)
    
    # 3. Call AI Gateway Adapter
    content, tool_calls, finish_reason = await gateway_client.execute(
        messages=pruned_msgs,
        tools=tools if state.tool_call_count < state.max_tool_calls else None,
        tool_choice="auto" if state.tool_call_count < state.max_tool_calls else "none",
        stream=state.stream,
        on_content_delta=on_delta,
    )
    
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
    new_tool_count = state.tool_call_count + len(calls)
    
    if on_tool_event:
        for call in calls:
            await on_tool_event("started", call, state.step_count)

    # Execute batch of calls concurrently
    results = await dispatcher.execute_batch(
        calls=calls,
        step=state.step_count,
        service=state.service_id,
    )
    
    for res in results:
        new_traces.append(res.trace)
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
