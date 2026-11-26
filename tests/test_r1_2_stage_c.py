"""R1.2 Stage C (P1) Fail-First Reproduction Tests: FIX-209 .. FIX-212."""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.core.clock import MockClock
from app.main import app
from app.runtime.langgraph_r1 import LangGraphR1Runtime
from app.runtime.policy.constraints import ConstraintSpec
from app.runtime.runner import runner
from app.runtime.state import AgentState
from app.schemas.agent import AgentRunRequest
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger
from tests.replay.harness import RecordedGatewayClient, RecordedGatewayStep


# ---------------------------------------------------------------------------
# FIX-209: Compiled LangGraph Reuse (compile_count == 1) & 50-Run Isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_209_compiled_graph_reuse_and_50_run_isolation(tmp_path):
    from app.runtime.langgraph_r1 import get_graph_compile_count

    class IsolatedGateway:
        def __init__(self, tag: str):
            self.tag = tag

        async def execute(self, **kwargs):
            await asyncio.sleep(0.002)
            return f"reply-{self.tag}", None, "stop"

    store = TrajectoryEventStore(tmp_path / "iso50.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())

    async def one_run(i: int):
        tag = f"u{i}"
        emitted = []
        constraints = ConstraintSpec.from_runtime(
            service_id="hd2ai-admin", owner_id=tag, authenticated=True,
            allowed_tools=[], max_steps=3, max_tool_calls=0,
            time_budget_seconds=10.0, context_budget_tokens=4000, check_live_tool_policy=False,
        )
        st = AgentState(
            run_id=f"run-iso-{i}", service_id="hd2ai-admin", owner_id=tag, authenticated=True,
            messages=[GatewayMessage(role="user", content=tag)],
            max_steps=3, max_tool_calls=0, status="running",
            custom_client=IsolatedGateway(tag),
        )
        rt = LangGraphR1Runtime(trajectory_logger=tlogger, clock=MockClock())
        await rt.execute(st, constraints, emit=lambda ev, d: emitted.append((ev, d)))
        assert st.final_answer == f"reply-{tag}"
        return len(emitted)

    before = get_graph_compile_count()
    counts = await asyncio.gather(*[one_run(i) for i in range(50)])
    after = get_graph_compile_count()

    assert all(c > 0 for c in counts)
    # Graph compiled AT MOST ONCE (0 new compiles if already cached, or 1 if cold)
    assert after <= max(1, before)


# ---------------------------------------------------------------------------
# FIX-210: Full Production AgentRunner Cross-Runtime Replay
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_210_full_agent_runner_cross_runtime_replay(tmp_path, monkeypatch):
    from tests.replay.harness import run_agent_runner_replay

    steps = [
        RecordedGatewayStep(
            content=None,
            tool_calls=[ToolCall(id="tc_fr_1", function=FunctionCall(name="system.get_datetime", arguments="{}"))],
            finish_reason="tool_calls",
            advance_seconds=0.5,
        ),
        RecordedGatewayStep(content="풀 런타임 리플레이 완료", tool_calls=None, finish_reason="stop", advance_seconds=0.5),
    ]
    run_a = await run_agent_runner_replay(
        engine="langgraph_r1", message="full replay", recorded_steps=steps, db_path=str(tmp_path / "r1_a.db"), monkeypatch=monkeypatch
    )
    run_b = await run_agent_runner_replay(
        engine="langgraph_r1", message="full replay", recorded_steps=steps, db_path=str(tmp_path / "r1_b.db"), monkeypatch=monkeypatch
    )
    assert run_a["status"] == "completed" and run_b["status"] == "completed"
    assert run_a["final_answer"] == run_b["final_answer"]
    assert run_a["tool_selection"] == run_b["tool_selection"] == ["system.get_datetime"]
    assert run_a["unused_fixtures"] == 0 and run_b["unused_fixtures"] == 0


# ---------------------------------------------------------------------------
# FIX-211: Real API HTTP 410 Expired Cursor & Disconnect Isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_211_sse_http_410_api_and_disconnect_does_not_cancel_run():
    # 1) Create a session, overflow its replay buffer, and verify real API returns HTTP 410
    session = await runner.create_run(AgentRunRequest(message="410 test", stream=False), owner_id="alice", service_id="hd2ai-admin")
    session.event_bus.max_buffer_size = 5
    session.event_bus._buffer = type(session.event_bus._buffer)(maxlen=5)
    for i in range(12):
        session.event_bus.publish("answer.delta", "running", {"delta": f"x{i}"})
    session.event_bus.publish_terminal("run.completed", "completed", {"answer": "ok"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_410 = await client.get(
            f"/api/v1/agent/runs/{session.state.run_id}/events",
            headers={
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Service": "hd2ai-admin",
                "Last-Event-ID": f"{session.state.run_id}-1",
            },
        )
        assert r_410.status_code == 410
        assert r_410.json()["error_code"] == "event_replay_expired"

    # 2) Disconnecting SSE consumer early MUST NOT cancel the underlying AgentRunner task
    class DelayedGateway:
        async def execute(self, **kwargs):
            await asyncio.sleep(0.08)
            return "completed after disconnect", None, "stop"

    sess2 = await runner.create_run(
        AgentRunRequest(message="disconnect test", stream=True),
        owner_id="alice",
        service_id="hd2ai-admin",
        custom_client=DelayedGateway(),
    )
    await runner.start_run(sess2)
    gen = runner.stream_events(sess2.state.run_id)
    first_frame = await gen.__anext__()
    assert "run.started" in first_frame
    await gen.aclose()  # simulate client network disconnect
    await sess2.task
    assert sess2.state.status == "completed"
    assert sess2.state.final_answer == "completed after disconnect"


# ---------------------------------------------------------------------------
# FIX-212: Legacy Terminal State Protection Against Late Gateway Results
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_212_legacy_terminal_fence_blocks_late_mutation():
    class UncancellableLateGateway:
        async def execute(self, **kwargs):
            try:
                await asyncio.sleep(0.15)
            except asyncio.CancelledError:
                # Simulate non-cooperative external call that finishes anyway
                return "LATE_CORRUPTED_ANSWER", None, "stop"
            return "LATE_CORRUPTED_ANSWER", None, "stop"

    sess = await runner.create_run(
        AgentRunRequest(message="late test", stream=False),
        owner_id="alice",
        service_id="hd2ai-admin",
        custom_client=UncancellableLateGateway(),
    )
    await runner.start_run(sess)
    await asyncio.sleep(0.02)
    await runner.cancel_run(sess.state.run_id)
    await sess.task

    assert sess.state.status == "cancelled"
    assert sess.state.final_answer is None  # late answer MUST NOT mutate state!
    assert sess.event_bus.terminal_event is not None
    assert sess.event_bus.terminal_event.event == "run.cancelled"
