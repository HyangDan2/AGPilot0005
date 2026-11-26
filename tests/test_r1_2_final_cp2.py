"""R1.2 Final Closure — Checkpoint 2 (P1) Fail-First Tests: NG-02 & NG-04 .. NG-08."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.clock import MockClock
from app.gateway.client import AIGatewayClient
from app.main import app
from app.runtime.event_bus import RunEventBus
from app.runtime.langgraph_r1 import LangGraphR1Runtime
from app.runtime.policy.constraints import ConstraintSpec
from app.runtime.runner import AgentRunner
from app.runtime.state import AgentState
from app.schemas.agent import AgentRunRequest
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tools.dispatcher import ToolDispatcher
from app.tools.registry import ToolRegistration, ToolRegistry
from app.tracing.async_writer import AsyncTrajectoryWriter
from app.tracing.store import FeedbackIntegrityError, TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# NG-02: Real AIGatewayClient HTTP Fixture (Non-stream, Stream, Concurrent Isolation)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ng_02_real_ai_gateway_client_http_fixture_usage(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/chat/completions" not in request.url.path:
            return httpx.Response(404, json={"detail": "not found"})
        body = json.loads(request.content.decode("utf-8"))
        user_msg = body["messages"][-1]["content"]
        in_tok = 111 if "runA" in user_msg else 222
        if body.get("stream"):
            sse = (
                f'data: {json.dumps({"choices":[{"delta":{"content":"hi-stream"}}]})}\n\n'
                f'data: {json.dumps({"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":in_tok,"completion_tokens":15,"total_tokens":in_tok+15},"gateway_complete":True,"model_label":"test-model"})}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": f"ok-{in_tok}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": in_tok, "completion_tokens": 10, "total_tokens": in_tok + 10},
                "model_label": "test-model",
            },
        )

    transport = httpx.MockTransport(handler)
    gw = AIGatewayClient(base_url="http://mock-gw", route="hd2ai-admin", token="tok-123", mock_mode=False)
    gw._get_transport = lambda: transport  # type: ignore[method-assign]

    store = TrajectoryEventStore(tmp_path / "ng02.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        allowed_tools=[], max_steps=3, max_tool_calls=0,
        time_budget_seconds=30.0, context_budget_tokens=4000, check_live_tool_policy=False,
    )

    async def run_one(rid: str, msg: str, stream: bool):
        st = AgentState(
            run_id=rid, service_id="hd2ai-admin", owner_id="alice", authenticated=True,
            messages=[GatewayMessage(role="user", content=msg)],
            max_steps=3, max_tool_calls=0, status="running", stream=stream,
            custom_client=gw,
        )
        await LangGraphR1Runtime(trajectory_logger=tlogger, clock=MockClock()).execute(st, constraints)

    await asyncio.gather(
        run_one("run-ng02-A", "runA", False),
        run_one("run-ng02-B", "runB", True),
    )
    out_a = [e for e in store.list_events(run_id="run-ng02-A") if e["event_type"] == "OutcomeEvent" and e["token_usage"]]
    out_b = [e for e in store.list_events(run_id="run-ng02-B") if e["event_type"] == "OutcomeEvent" and e["token_usage"]]
    assert out_a and out_a[0]["token_usage"]["input_tokens"] == 111
    assert out_b and out_b[0]["token_usage"]["input_tokens"] == 222


# ---------------------------------------------------------------------------
# NG-04: Pre-Handler Zero-Budget Gate (0 handler invocations when remaining == 0)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ng_04_zero_budget_blocks_handler_execution():
    called = {"count": 0}

    def side_handler(q: str):
        called["count"] += 1
        return {"ok": True}

    local_reg = ToolRegistry()
    local_reg.register(
        "budget.probe",
        "probe",
        {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        side_handler,
        is_read_only=True,
    )
    disp = ToolDispatcher(tool_registry=local_reg)
    res = await disp.execute_tool_call(
        ToolCall(id="c_zero", function=FunctionCall(name="budget.probe", arguments='{"q":"test"}')),
        input_budget_tokens=4000,
        remaining_input_tokens=0,
        constraints=ConstraintSpec.from_runtime(
            service_id="hd2ai-admin", owner_id="alice", authenticated=True,
            allowed_tools=["budget.probe"], max_steps=3, max_tool_calls=3,
            time_budget_seconds=10.0, context_budget_tokens=4000, check_live_tool_policy=False,
        ),
    )
    assert called["count"] == 0  # Handler MUST NOT be called when remaining budget == 0!
    assert res.is_error is True
    assert res.trace.status == "failed"
    assert res.trace.error_code == "tool_budget_exhausted"


# ---------------------------------------------------------------------------
# NG-05: Feedback Fail-Closed HTTP 503 when UNIQUE Index Missing / Corrupt
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ng_05_feedback_fail_closed_503_when_dedup_index_missing(tmp_path, monkeypatch):
    db_file = tmp_path / "broken_dedup.db"
    store = TrajectoryEventStore(db_file)
    # Simulate missing unique index on an existing DB
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("DROP INDEX IF EXISTS uq_trajectory_feedback_dedup;")
        conn.commit()
    finally:
        conn.close()
    store.feedback_dedup_integrity_ok = False

    tlogger = TrajectoryLogger(store, clock=MockClock())
    tlogger.log_decision(
        trajectory_id="traj-ng05", run_id="run-ng05-1", decision_id="dec-1", step_index=1,
        decision_type="step_action", state_features={}, allowed_actions=["CALL_MODEL"],
        constraints={}, chosen_action="CALL_MODEL", proposed_parameters={},
        behavior_policy_version="agpolicy-r1-v1", config_digest="1111",
        action_probability=None, validator_result={"accepted": True},
        owner_id="alice", service_id="hd2ai-admin",
    )
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/runs/run-ng05-1/feedback",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"},
            json={"feedback_type": "thumbs_up"},
        )
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# NG-06: SSE Terminal Cursor Reconnect MUST NOT Duplicate Terminal Event
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ng_06_reconnect_at_terminal_cursor_does_not_duplicate_terminal():
    bus = RunEventBus(run_id="run-ng06-1")
    bus.publish("run.started", "running", {})
    term = bus.publish_terminal("run.completed", "completed", {"answer": "done"})
    assert term is not None

    frames = []
    async for f in bus.stream(last_event_id=term.event_id):
        frames.append(f)
    assert frames == []  # Already received terminal cursor -> 0 duplicate frames!


# ---------------------------------------------------------------------------
# NG-07: Bounded Session TTL, Tombstone HTTP 410 (Owner) vs 404 (Other/Missing)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ng_07_bounded_session_tombstone_410_and_404(monkeypatch):
    import time as _time
    now_ts = _time.time()
    local_runner = AgentRunner(finished_session_ttl_seconds=0.05, max_finished_sessions=2)
    sess = await local_runner.create_run(AgentRunRequest(message="hi", stream=False), owner_id="alice", service_id="hd2ai-admin")
    sess.state.status = "completed"
    sess.finished_at = now_ts - 10.0  # expired 10s ago
    local_runner.evict_expired_sessions(now=now_ts)

    # Owner lookup -> SESSION_EXPIRED (410); other user -> SESSION_NOT_FOUND (404)
    s_own, code_own = local_runner.lookup_session(sess.state.run_id, owner_id="alice", service_id="hd2ai-admin")
    assert s_own is None and code_own == "SESSION_EXPIRED"

    s_other, code_other = local_runner.lookup_session(sess.state.run_id, owner_id="bob", service_id="hd2ai-admin")
    assert s_other is None and code_other == "SESSION_NOT_FOUND"

    # Active running session MUST NOT be evicted
    active = await local_runner.create_run(AgentRunRequest(message="still running", stream=False), owner_id="alice", service_id="hd2ai-admin")
    active.state.status = "running"
    local_runner.evict_expired_sessions(now=999999.0)
    assert local_runner.get_session(active.state.run_id) is not None


# ---------------------------------------------------------------------------
# NG-08: Dirty Set Persistence (Bypassing Saturated Queue) & Training Export Filtering
# ---------------------------------------------------------------------------
def test_ng_08_dirty_set_persistence_and_training_export_filter(tmp_path):
    store = TrajectoryEventStore(tmp_path / "ng08.db")
    writer = AsyncTrajectoryWriter(store, queue_capacity=1)
    tlogger = TrajectoryLogger(store, clock=MockClock(), async_writer=writer)

    # Mark dirty directly (simulating queue drop) -> persisted into existing incomplete_trajectories table
    writer.mark_dirty("traj-dirty-1", "queue_dropped")
    writer.flush(timeout_seconds=3.0)
    assert "traj-dirty-1" in store.list_incomplete_trajectories()
    assert "traj-dirty-1" not in writer.dirty_trajectories  # cleared ONLY after DB commit!

    # Log a complete trajectory vs incomplete trajectory
    tlogger_sync = TrajectoryLogger(store, clock=MockClock())
    tlogger_sync.log_decision(
        trajectory_id="traj-dirty-1", run_id="run-dirty-1", decision_id="dec-d1", step_index=1,
        decision_type="step_action", state_features={}, allowed_actions=["CALL_MODEL"],
        constraints={}, chosen_action="CALL_MODEL", proposed_parameters={},
        behavior_policy_version="agpolicy-r1-v1", config_digest="1111",
        action_probability=None, validator_result={"accepted": True},
        owner_id="alice", service_id="hd2ai-admin",
    )
    tlogger_sync.log_action(
        trajectory_id="traj-dirty-1", run_id="run-dirty-1", decision_id="dec-d1", action_id="act-d1",
        executed_action="CALL_MODEL", executed_parameters={}, fallback_applied=False,
        owner_id="alice", service_id="hd2ai-admin",
    )
    tlogger_sync.log_outcome(
        trajectory_id="traj-dirty-1", run_id="run-dirty-1", decision_id="dec-d1", action_id="act-d1",
        outcome="success", next_state_features={}, terminal_status="completed",
        owner_id="alice", service_id="hd2ai-admin",
    )

    rec = store.reconstruct_trajectory(trajectory_id="traj-dirty-1")
    # Even with terminal_status="completed", incomplete_trajectories marker excludes it from training!
    assert rec["eligible_for_training"] is False

    out_file = tmp_path / "train.jsonl"
    exported = store.export_training_jsonl(out_file)
    assert exported == 0

    writer.shutdown(timeout_seconds=3.0)
