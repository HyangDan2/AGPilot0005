import asyncio
import json
import pytest
from app.runtime.runner import runner
from app.schemas.agent import AgentRunOptions, AgentRunRequest

@pytest.mark.asyncio
async def test_sse_streaming_events():
    """Verifies end-to-end SSE event emissions (run.started -> step.* -> answer.delta -> run.completed)."""
    req = AgentRunRequest(
        message="오늘 날짜 알려줘",
        stream=True,
    )
    session = await runner.create_run(req)
    await runner.start_run(session)
    
    events_received = []
    async for sse_text in runner.stream_events(session.state.run_id):
        events_received.append(sse_text)
        if "event: run.completed" in sse_text:
            break
            
    assert any("event: run.started" in evt for evt in events_received)
    assert any("event: step.started" in evt for evt in events_received)
    assert any("event: tool.started" in evt for evt in events_received)
    assert any("event: tool.completed" in evt for evt in events_received)
    assert any("event: run.completed" in evt for evt in events_received)

@pytest.mark.asyncio
async def test_max_tool_calls_limit_enforced():
    """Verifies that max_tool_calls limit stops runaway loops safely."""
    req = AgentRunRequest(
        message="날짜 계속 확인해줘",
        options=AgentRunOptions(max_tool_calls=2, max_steps=5),
        stream=False,
    )
    session = await runner.create_run(req)
    await runner.start_run(session)
    await session.task
    
    assert session.state.status == "completed"
    assert session.state.tool_call_count <= 2
