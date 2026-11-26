"""R1.2 Stage B (P1) Fail-First Reproduction Tests: FIX-205 .. FIX-208."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from types import MappingProxyType

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.clock import MockClock
from app.main import app
from app.runtime.langgraph_r1 import LangGraphR1Runtime
from app.runtime.policy.constraints import ConstraintSpec
from app.runtime.state import AgentState
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tools.dispatcher import ToolDispatcher, _apply_dynamic_tool_budget
from app.tools.registry import ToolRegistration, ToolRegistry
from app.tracing.async_writer import AsyncTrajectoryWriter
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# FIX-205: ToolExecutionTrace SSoT ("completed" not "success")
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_205_tool_outcome_uses_completed_status_ssot(tmp_path):
    class OneToolGateway:
        def __init__(self):
            self.step = 0

        async def execute(self, **kwargs):
            self.step += 1
            if self.step == 1:
                return None, [ToolCall(id="tc_ok_1", function=FunctionCall(name="system.echo", arguments='{"message":"hello"}'))], "tool_calls"
            return "echo done", None, "stop"

    store = TrajectoryEventStore(tmp_path / "status.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        allowed_tools=["system.echo"], max_steps=5, max_tool_calls=4,
        time_budget_seconds=30.0, context_budget_tokens=4000, check_live_tool_policy=False,
    )
    state = AgentState(
        run_id="run-stat-1", service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        messages=[GatewayMessage(role="user", content="echo")],
        max_steps=5, max_tool_calls=4, status="running",
        custom_client=OneToolGateway(),
    )
    emitted = []

    async def capture(ev_name, data):
        emitted.append((ev_name, data))

    rt = LangGraphR1Runtime(trajectory_logger=tlogger, clock=MockClock())
    await rt.execute(state, constraints, emit=capture)

    # SSE tool.completed MUST report is_error=False for completed tool!
    tool_done = [d for name, d in emitted if name == "tool.completed"]
    assert len(tool_done) == 1
    assert tool_done[0]["is_error"] is False

    # Trajectory OutcomeEvent for CALL_TOOLS MUST report outcome="success" (not "failed"!)
    events = store.list_events(run_id="run-stat-1")
    act_ids = {e["action_id"] for e in events if e["event_type"] == "ActionEvent" and e["executed_action"] == "CALL_TOOLS"}
    tool_outcomes = [e for e in events if e["event_type"] == "OutcomeEvent" and e["action_id"] in act_ids]
    assert len(tool_outcomes) == 1
    assert tool_outcomes[0]["outcome"] == "success"


# ---------------------------------------------------------------------------
# FIX-206: Strict Dynamic Tool Budget (remaining=0 -> 0 budget, remaining=1, additionalProperties)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_206_strict_zero_and_tiny_budget_and_additional_properties():
    reg = ToolRegistration(
        name="strict.tool",
        description="strict",
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 100, "maximum": 10000}},
            "required": ["q"],
            "additionalProperties": False,
        },
        is_read_only=True,
    )
    # 1) remaining_input_tokens=0 -> dynamic_max_bytes MUST be 0 (no 256-token floor!)
    _, max_bytes_0 = _apply_dynamic_tool_budget(reg, {"q": "a"}, input_budget_tokens=8000, remaining_input_tokens=0, batch_size=2)
    assert max_bytes_0 == 0

    # 2) remaining_input_tokens=10 across batch_size=4 -> sum of allocations <= remaining * 4 bytes
    _, max_bytes_small = _apply_dynamic_tool_budget(reg, {"q": "a"}, input_budget_tokens=8000, remaining_input_tokens=10, batch_size=4)
    assert max_bytes_small * 4 <= 10 * 4

    # 3) additionalProperties=False rejects unexpected extra keys
    local_reg = ToolRegistry()
    local_reg.register(reg.name, reg.description, reg.parameters, lambda q, max_chars=100: {"ok": True}, is_read_only=True)
    disp = ToolDispatcher(tool_registry=local_reg)
    res = await disp.execute_tool_call(
        ToolCall(id="c_extra", function=FunctionCall(name="strict.tool", arguments='{"q":"hi","injected":"bad"}')),
        constraints=ConstraintSpec.from_runtime(
            service_id="hd2ai-admin", owner_id="u1", authenticated=True,
            allowed_tools=["strict.tool"], max_steps=3, max_tool_calls=3,
            time_budget_seconds=10.0, context_budget_tokens=4000, check_live_tool_policy=False,
        ),
    )
    assert res.is_error and res.trace.error_code == "invalid_arguments"


# ---------------------------------------------------------------------------
# FIX-207: Production Gateway Usage Contract (Per-Invocation, Concurrent Isolation)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_207_per_invocation_usage_isolated_across_concurrent_runs(tmp_path):
    from app.schemas.gateway import ModelReply

    class SharedProductionLikeGateway:
        """Simulates shared AIGatewayClient returning per-call ModelReply(usage=...) with NO shared last_usage."""

        async def execute(self, *, messages, return_reply=False, **kwargs):
            user_txt = messages[-1].content or ""
            tok = 100 if "runA" in user_txt else 500
            await asyncio.sleep(0.02 if tok == 100 else 0.005)
            if return_reply:
                return ModelReply(
                    content=f"ok-{tok}",
                    tool_calls=[],
                    finish_reason="stop",
                    usage={"prompt_tokens": tok, "completion_tokens": 20, "total_tokens": tok + 20},
                )
            return f"ok-{tok}", [], "stop"

    shared_gw = SharedProductionLikeGateway()
    store = TrajectoryEventStore(tmp_path / "gw_usage.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        allowed_tools=[], max_steps=3, max_tool_calls=0,
        time_budget_seconds=30.0, context_budget_tokens=4000, check_live_tool_policy=False,
    )

    async def run_one(rid: str, text: str):
        st = AgentState(
            run_id=rid, service_id="hd2ai-admin", owner_id="alice", authenticated=True,
            messages=[GatewayMessage(role="user", content=text)],
            max_steps=3, max_tool_calls=0, status="running",
            custom_client=shared_gw,
        )
        await LangGraphR1Runtime(trajectory_logger=tlogger, clock=MockClock()).execute(st, constraints)

    await asyncio.gather(run_one("run-conc-A", "runA"), run_one("run-conc-B", "runB"))
    out_a = [e for e in store.list_events(run_id="run-conc-A") if e["event_type"] == "OutcomeEvent" and e["token_usage"]]
    out_b = [e for e in store.list_events(run_id="run-conc-B") if e["event_type"] == "OutcomeEvent" and e["token_usage"]]
    assert out_a and out_a[0]["token_usage"]["input_tokens"] == 100
    assert out_b and out_b[0]["token_usage"]["input_tokens"] == 500


# ---------------------------------------------------------------------------
# FIX-208: Atomic DB Feedback Uniqueness (NULL decision_id, alternating thumbs, concurrent) & WAL/Migration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_208_atomic_feedback_uniqueness_concurrent_and_idempotent_migration(tmp_path, monkeypatch):
    db_file = tmp_path / "fb_atomic.db"
    store = TrajectoryEventStore(db_file)
    # Re-initializing store on existing DB MUST be idempotent
    store2 = TrajectoryEventStore(db_file)
    # Verify WAL mode active
    conn = sqlite3.connect(str(db_file))
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0].lower()
        assert mode == "wal"
    finally:
        conn.close()

    tlogger = TrajectoryLogger(store2, clock=MockClock())
    tlogger.log_decision(
        trajectory_id="traj-fb-atom", run_id="run-fbatom01", decision_id="dec-1", step_index=1,
        decision_type="step_action", state_features={}, allowed_actions=["CALL_MODEL"],
        constraints={}, chosen_action="CALL_MODEL", proposed_parameters={},
        behavior_policy_version="agpolicy-r1-v1", config_digest="1111",
        action_probability=None, validator_result={"accepted": True},
        owner_id="alice", service_id="hd2ai-admin", tenant_id="default_tenant",
    )
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) Concurrent identical run-level (decision_id=NULL) feedback -> exactly one 201, rest 409!
        res_pair = await asyncio.gather(
            client.post("/api/v1/agent/runs/run-fbatom01/feedback", headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"}, json={"feedback_type": "thumbs_up"}),
            client.post("/api/v1/agent/runs/run-fbatom01/feedback", headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"}, json={"feedback_type": "thumbs_up"}),
        )
        codes = sorted(r.status_code for r in res_pair)
        assert codes == [201, 409]

        # 2) Alternating opposite feedback type (thumbs_down after thumbs_up) on same run -> blocked (409)!
        r_flip = await client.post(
            "/api/v1/agent/runs/run-fbatom01/feedback",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"},
            json={"feedback_type": "thumbs_down"},
        )
        assert r_flip.status_code == 409
