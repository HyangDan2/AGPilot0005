"""Phase R1.1 Stage B (P1) Fail-First Reproduction & Gate Tests (QA-R1-10 .. QA-R1-13)."""
from __future__ import annotations

import sqlite3
from types import MappingProxyType
from typing import Any, Dict, List, Optional

import pytest

from app.core.clock import MockClock
from app.runtime.langgraph_r1 import LangGraphR1Runtime
from app.runtime.policy.ag_policy import DecisionType, PolicyProposal, StateView
from app.runtime.policy.constraints import ActionType, ConstraintSpec
from app.runtime.policy.validator import ActionValidator, FallbackAction
from app.runtime.state import AgentState
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# QA-R1-10 (R1-FIX-06): Policy Parameter (reasoning_effort & context_budget) Actually Applied
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_10_policy_parameters_actually_applied(tmp_path):
    observed: Dict[str, Any] = {}

    class CapturingGateway:
        async def execute(self, *, messages, reasoning_effort=None, **kwargs):
            observed["reasoning_effort"] = reasoning_effort
            observed["message_count"] = len(messages)
            return "정책 파라미터 적용 완료", None, "stop"

    class CustomParamPolicy:
        version = "custom-param-policy-v1"

        def propose(self, state_view: StateView, c: ConstraintSpec) -> PolicyProposal:
            if state_view.has_final_answer:
                return PolicyProposal(DecisionType.TERMINATION.value, ActionType.TERMINATE.value, MappingProxyType({"terminal_status": "completed"}), self.version)
            return PolicyProposal(
                DecisionType.REASONING_CONFIG.value,
                ActionType.CALL_MODEL.value,
                MappingProxyType({"reasoning_effort": "high", "context_budget_tokens": 1024}),
                self.version,
            )

    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        allowed_tools=[], max_steps=3, max_tool_calls=0,
        time_budget_seconds=30.0, context_budget_tokens=4000, check_live_tool_policy=False,
    )
    state = AgentState(
        run_id="run-param-1", service_id="hd2ai-admin", owner_id="alice",
        messages=[GatewayMessage(role="user", content="hello")],
        max_steps=3, max_tool_calls=0, status="running",
        reasoning_effort="low",  # state starts at low; policy proposes high!
        custom_client=CapturingGateway(),
    )
    rt = LangGraphR1Runtime(
        policy=CustomParamPolicy(),
        trajectory_logger=TrajectoryLogger(TrajectoryEventStore(tmp_path / "p.db"), clock=MockClock()),
        clock=MockClock(),
    )
    await rt.execute(state, constraints)
    assert state.status == "completed"
    assert observed["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# QA-R1-11 (R1-FIX-07): Safe Fallback preserves tool_call_count, checks context budget, rejects empty answer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_11_safe_fallback_preserves_counts_and_blocks_empty_completed(tmp_path):
    validator = ActionValidator()
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        allowed_tools=["system.echo"], max_steps=4, max_tool_calls=1,
        time_budget_seconds=30.0, context_budget_tokens=500, check_live_tool_policy=False,
    )
    # Non-terminal fallback SAFE_RESPOND MUST fail validation when context exceeds budget
    sv_over_ctx = StateView(
        run_id="run-fb-ctx", service_id="hd2ai-admin", step_count=1, tool_call_count=1,
        elapsed_seconds=1.0, remaining_time_budget_seconds=29.0,
        estimated_context_tokens=1500,  # exceeds 500!
        has_pending_tool_calls=False,
    )
    fb = FallbackAction(action=ActionType.SAFE_RESPOND.value, parameters=MappingProxyType({"disable_tools": True}), reason_code="tool_call_budget_exceeded")
    res = validator.validate_fallback(fb, sv_over_ctx, constraints)
    assert not res.accepted
    assert res.reason_code == "context_budget_insufficient"

    # SAFE_RESPOND executes with tools disabled WITHOUT overwriting tool_call_count, and empty response -> failed
    class EmptySafeGateway:
        def __init__(self):
            self.step = 0

        async def execute(self, *, tools=None, **kwargs):
            self.step += 1
            if self.step == 1:
                return None, [
                    ToolCall(id="tc1", function=FunctionCall(name="system.echo", arguments='{"message":"1"}')),
                    ToolCall(id="tc2", function=FunctionCall(name="system.echo", arguments='{"message":"2"}')),
                ], "tool_calls"
            assert tools is None  # tools disabled in SAFE_RESPOND!
            return "   ", None, "stop"  # empty answer must NOT be marked completed!

    state = AgentState(
        run_id="run-empty-fb", service_id="hd2ai-admin", owner_id="alice",
        messages=[GatewayMessage(role="user", content="go")],
        max_steps=4, max_tool_calls=1, status="running",
        custom_client=EmptySafeGateway(),
    )
    rt = LangGraphR1Runtime(
        trajectory_logger=TrajectoryLogger(TrajectoryEventStore(tmp_path / "fb.db"), clock=MockClock()),
        clock=MockClock(),
    )
    await rt.execute(state, constraints)
    assert state.tool_call_count == 0  # actual count NOT overwritten to max_tool_calls!
    assert state.status == "failed"  # empty answer never completed!


# ---------------------------------------------------------------------------
# QA-R1-12 (R1-FIX-08): Async Telemetry Queue Isolation, Failure Isolation, Explicit Flush & Shutdown
# ---------------------------------------------------------------------------
def test_qa_r1_12_async_telemetry_isolation_flush_and_shutdown(tmp_path):
    from app.tracing.async_writer import AsyncTrajectoryWriter

    store = TrajectoryEventStore(tmp_path / "async_t.db")
    writer = AsyncTrajectoryWriter(store, queue_capacity=4)
    tlogger = TrajectoryLogger(store, clock=MockClock(), async_writer=writer)

    for i in range(6):
        tlogger.log_feedback(
            trajectory_id="traj-async-1",
            run_id="run-async-1",
            feedback_type="thumbs_up",
            reward_signal=1.0,
            event_id=f"fb-async-{i}",
        )

    flush_res = writer.flush(timeout_seconds=3.0)
    assert flush_res.completed is True
    health = writer.health()
    for key in (
        "queue_depth", "queue_capacity", "dropped_event_count",
        "persistence_failure_count", "worker_status", "telemetry_health", "incomplete_trajectory_count",
    ):
        assert key in health

    # Simulate SQLite write failure -> runtime MUST NOT raise!
    def broken_append(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    store.append = broken_append  # type: ignore[method-assign]
    tlogger.log_feedback(
        trajectory_id="traj-broken-1",
        run_id="run-broken-1",
        feedback_type="thumbs_down",
        reward_signal=-1.0,
        event_id="fb-broken-1",
    )
    writer.flush(timeout_seconds=3.0)
    h2 = writer.health()
    assert h2["persistence_failure_count"] >= 1
    assert "traj-broken-1" in writer.incomplete_trajectories

    shut = writer.shutdown(timeout_seconds=3.0)
    assert shut["clean"] is True


# ---------------------------------------------------------------------------
# QA-R1-13 (R1-FIX-09): Actual Provider Token Usage & Accurate Tool Outcome Recording
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_13_actual_token_usage_and_tool_outcome_recorded(tmp_path):
    class UsageGateway:
        def __init__(self):
            self.last_usage = None

        async def execute(self, **kwargs):
            self.last_usage = {"input_tokens": 120, "output_tokens": 45, "total_tokens": 165}
            return "토큰 사용량 측정 완료", None, "stop"

    store = TrajectoryEventStore(tmp_path / "usage.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        allowed_tools=[], max_steps=3, max_tool_calls=0,
        time_budget_seconds=30.0, context_budget_tokens=4000, check_live_tool_policy=False,
    )
    state = AgentState(
        run_id="run-usage-1", service_id="hd2ai-admin", owner_id="alice",
        messages=[GatewayMessage(role="user", content="usage test")],
        max_steps=3, max_tool_calls=0, status="running",
        custom_client=UsageGateway(),
    )
    rt = LangGraphR1Runtime(trajectory_logger=tlogger, clock=MockClock())
    await rt.execute(state, constraints)

    events = store.list_events(run_id="run-usage-1")
    outcomes = [e for e in events if e["event_type"] == "OutcomeEvent"]
    assert any(o["token_usage"].get("input_tokens") == 120 and o["token_usage"].get("output_tokens") == 45 for o in outcomes)
    # Never fabricates unmeasured rewards
    for o in outcomes:
        assert o["reward_signals"] == {}
