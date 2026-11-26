"""Phase R1.1 Stage A (P0) Fail-First Reproduction & Gate Tests (QA-R1-01 .. QA-R1-09)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import MappingProxyType
from typing import Any, List, Optional, Tuple

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.core.clock import MockClock
from app.main import app
from app.runtime.langgraph_r1 import LangGraphR1Runtime
from app.runtime.policy.ag_policy import PolicyProposal, StateView
from app.runtime.policy.constraints import ActionType, ConstraintSpec
from app.runtime.policy.validator import ActionValidator
from app.runtime.state import AgentState
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# QA-R1-01 & QA-R1-02: ValidatedAction Contract & Tool Budget Bypass Prevention
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_01_validated_action_direct_instantiation_blocked():
    from app.runtime.policy.validator import ValidatedAction

    with pytest.raises(PermissionError):
        ValidatedAction(
            action=ActionType.CALL_TOOLS.value,
            parameters=MappingProxyType({}),
            validated_tool_calls=(),
            actual_call_count=0,
            scope_owner_id="attacker",
            scope_service_id="hd2ai-admin",
            budget_reservation=0,
            deadline_monotonic=999999.0,
            fallback_applied=False,
            reason_code=None,
            _mint_token=object(),  # forged token must be rejected
        )


@pytest.mark.asyncio
async def test_qa_r1_01_and_02_only_validated_tool_calls_execute_and_forged_count_rejected(tmp_path):
    """Pending 5 calls -> policy tries to spoof tool_count=1 -> validator rejects by actual count;
    when policy proposes 1 valid call ID out of 5 pending, ONLY that 1 call executes."""
    from app.runtime.policy.validator import ValidatedAction

    validator = ActionValidator()
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin",
        owner_id="alice",
        authenticated=True,
        allowed_tools=["system.echo"],
        max_steps=5,
        max_tool_calls=2,
        time_budget_seconds=30.0,
        context_budget_tokens=4000,
        check_live_tool_policy=False,
    )
    pending_5 = [
        ToolCall(id=f"tc_{i}", function=FunctionCall(name="system.echo", arguments=f'{{"message": "m{i}"}}'))
        for i in range(5)
    ]
    sv = StateView(
        run_id="run-va-1",
        service_id="hd2ai-admin",
        step_count=1,
        tool_call_count=0,
        elapsed_seconds=0.5,
        remaining_time_budget_seconds=29.5,
        estimated_context_tokens=200,
        has_pending_tool_calls=True,
        pending_tool_names=tuple(tc.function.name for tc in pending_5),
        pending_tool_count=5,
    )

    # 1) Spoofed tool_count=1 while referencing 4 call IDs -> MUST reject (budget=2, actual=4)
    spoof = PolicyProposal(
        decision_type="tool_dispatch",
        chosen_action=ActionType.CALL_TOOLS.value,
        proposed_parameters=MappingProxyType(
            {
                "tool_call_ids": ("tc_0", "tc_1", "tc_2", "tc_3"),
                "tool_names": ("system.echo",) * 4,
                "tool_count": 1,  # forged!
            }
        ),
    )
    res_spoof = validator.validate(spoof, sv, constraints, pending_tool_calls=pending_5)
    assert not res_spoof.accepted
    assert res_spoof.reason_code == "tool_call_budget_exceeded"

    # 2) Duplicate or non-existent tool_call_id -> MUST reject
    dup = PolicyProposal(
        decision_type="tool_dispatch",
        chosen_action=ActionType.CALL_TOOLS.value,
        proposed_parameters=MappingProxyType({"tool_call_ids": ("tc_0", "tc_0")}),
    )
    res_dup = validator.validate(dup, sv, constraints, pending_tool_calls=pending_5)
    assert not res_dup.accepted

    missing = PolicyProposal(
        decision_type="tool_dispatch",
        chosen_action=ActionType.CALL_TOOLS.value,
        proposed_parameters=MappingProxyType({"tool_call_ids": ("tc_nonexistent",)}),
    )
    res_missing = validator.validate(missing, sv, constraints, pending_tool_calls=pending_5)
    assert not res_missing.accepted

    # 3) Approve ONLY 1 of 5 pending calls -> Runtime executes ONLY that 1 call
    class SingleSelectPolicy:
        version = "single-select-test"

        def propose(self, state_view: StateView, c: ConstraintSpec) -> PolicyProposal:
            if state_view.has_pending_tool_calls:
                return PolicyProposal(
                    decision_type="tool_dispatch",
                    chosen_action=ActionType.CALL_TOOLS.value,
                    proposed_parameters=MappingProxyType({"tool_call_ids": ("tc_2",)}),
                    behavior_policy_version=self.version,
                )
            if state_view.has_final_answer:
                return PolicyProposal("step_action", ActionType.TERMINATE.value, MappingProxyType({"terminal_status": "completed"}))
            return PolicyProposal("step_action", ActionType.CALL_MODEL.value)

    class TwoStepGateway:
        def __init__(self):
            self.step = 0

        async def execute(self, **kwargs):
            self.step += 1
            if self.step == 1:
                return None, list(pending_5), "tool_calls"
            return "완료했습니다.", None, "stop"

    clock = MockClock()
    store = TrajectoryEventStore(tmp_path / "va.db")
    tlogger = TrajectoryLogger(store, clock=clock)
    state = AgentState(
        run_id="run-va-exec-1",
        service_id="hd2ai-admin",
        owner_id="alice",
        messages=[GatewayMessage(role="user", content="echo test")],
        max_steps=5,
        max_tool_calls=2,
        status="running",
        custom_client=TwoStepGateway(),
    )
    rt = LangGraphR1Runtime(policy=SingleSelectPolicy(), validator=validator, trajectory_logger=tlogger, clock=clock)
    await rt.execute(state, constraints)
    assert state.status == "completed"
    assert state.tool_call_count == 1
    assert len(state.traces) == 1
    assert state.traces[0].tool_call_id == "tc_2"


# ---------------------------------------------------------------------------
# QA-R1-03 & QA-R1-04: Trajectory Owner Isolation & Feedback Referential Integrity
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_03_and_04_trajectory_isolation_and_feedback_integrity(tmp_path, monkeypatch):
    store = TrajectoryEventStore(tmp_path / "acl.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())
    tlogger.log_decision(
        trajectory_id="traj-alice-1",
        run_id="run-alice0001",
        decision_id="dec-alice-1",
        step_index=1,
        decision_type="step_action",
        state_features={"owner_id": "alice", "service_id": "hd2ai-admin"},
        allowed_actions=["CALL_MODEL"],
        constraints={"service_id": "hd2ai-admin", "owner_id": "alice"},
        chosen_action="CALL_MODEL",
        proposed_parameters={},
        behavior_policy_version="agpolicy-r1-v1",
        config_digest="aaaa",
        action_probability=None,
        validator_result={"accepted": True},
        owner_id="alice",
        service_id="hd2ai-admin",
        tenant_id="default_tenant",
    )
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) Another user (bob) blocked from reading alice's trajectory
        r_other = await client.get(
            "/api/v1/agent/runs/run-alice0001/trajectory",
            headers={"X-HD2Lab-User": "bob", "X-HD2Lab-Service": "hd2ai-admin"},
        )
        assert r_other.status_code in (403, 404)

        # 2) Non-existent run blocked from feedback creation
        r_missing_run = await client.post(
            "/api/v1/agent/runs/run-doesnotexist/feedback",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"},
            json={"feedback_type": "thumbs_up"},
        )
        assert r_missing_run.status_code == 404

        # 3) Non-existent decision_id blocked
        r_bad_dec = await client.post(
            "/api/v1/agent/runs/run-alice0001/feedback",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"},
            json={"feedback_type": "thumbs_up", "decision_id": "dec-forged-999"},
        )
        assert r_bad_dec.status_code == 422

        # 4) Cross-run trajectory_id blocked
        r_bad_traj = await client.post(
            "/api/v1/agent/runs/run-alice0001/feedback",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"},
            json={"feedback_type": "thumbs_up", "trajectory_id": "traj-other-run"},
        )
        assert r_bad_traj.status_code == 422

        # 5) Another user (bob) blocked from submitting feedback on alice's run
        r_bob_fb = await client.post(
            "/api/v1/agent/runs/run-alice0001/feedback",
            headers={"X-HD2Lab-User": "bob", "X-HD2Lab-Service": "hd2ai-admin"},
            json={"feedback_type": "thumbs_down"},
        )
        assert r_bob_fb.status_code in (403, 404)

        # 6) Owner (alice) succeeds; server normalizes reward_signal (ignores arbitrary weight)
        r_ok = await client.post(
            "/api/v1/agent/runs/run-alice0001/feedback",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"},
            json={"feedback_type": "thumbs_up", "reward_signal": 0.123, "decision_id": "dec-alice-1"},
        )
        assert r_ok.status_code == 201
        assert r_ok.json()["reward_signal"] == 1.0  # server-normalized!

        # 7) Duplicate feedback blocked (409)
        r_dup = await client.post(
            "/api/v1/agent/runs/run-alice0001/feedback",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"},
            json={"feedback_type": "thumbs_up", "decision_id": "dec-alice-1"},
        )
        assert r_dup.status_code == 409


# ---------------------------------------------------------------------------
# QA-R1-05 & QA-R1-06: SSE Event Bus (1000+ non-stream, slow sub, 410 Gone, cancel)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_05_and_06_event_bus_non_blocking_replay_and_410():
    from app.runtime.event_bus import EventReplayExpiredError, RunEventBus

    bus = RunEventBus(run_id="run-bus-1", max_buffer_size=50, subscriber_queue_size=16)

    # Slow subscriber that never drains its queue
    slow_sub = bus.subscribe()

    # Non-stream / saturated publish of 1,050 events MUST NOT block
    for i in range(1050):
        bus.publish("answer.delta", "running", {"delta": f"c{i}"})
    bus.publish_terminal("run.completed", "completed", {"answer": "done"})

    # Terminal event preserved separately even though buffer is capped at 50
    assert bus.terminal_event is not None
    assert bus.terminal_event.event == "run.completed"
    # Duplicate terminal publish is ignored (exactly-once)
    assert bus.publish_terminal("run.failed", "failed", {"code": "late"}) is None
    assert bus.terminal_event.event == "run.completed"

    # Expired Last-Event-ID raises EventReplayExpiredError (HTTP 410)
    with pytest.raises(EventReplayExpiredError):
        bus.validate_cursor("run-bus-1-1")

    # Cross-run cursor rejected
    with pytest.raises(ValueError):
        bus.validate_cursor("run-OTHER-1040")

    # Valid recent cursor replays in exact order + terminal event
    recent_cursor = f"run-bus-1-{1048}"
    replayed = bus.replay_after(recent_cursor)
    assert [e.event_id for e in replayed] == ["run-bus-1-1049", "run-bus-1-1050", "run-bus-1-1051"]

    bus.unsubscribe(slow_sub)


# ---------------------------------------------------------------------------
# QA-R1-07 & QA-R1-08: Hard Deadline & Terminal State Fence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_07_and_08_hard_deadline_and_terminal_fence(tmp_path):
    from app.runtime.fence import TerminalStateFence

    fence = TerminalStateFence()
    assert fence.try_finalize("cancelled") is True
    # Subsequent transitions (race with completed/failed) MUST fail
    assert fence.try_finalize("completed") is False
    assert fence.terminal_state == "cancelled"
    assert fence.record_late_result() == 1

    # Hard deadline kills slow Gateway call inside LangGraphR1Runtime
    class SlowGateway:
        async def execute(self, **kwargs):
            await asyncio.sleep(3.0)
            return "too late", None, "stop"

    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin",
        owner_id="alice",
        authenticated=True,
        allowed_tools=[],
        max_steps=3,
        max_tool_calls=0,
        time_budget_seconds=0.15,
        context_budget_tokens=4000,
        check_live_tool_policy=False,
    )
    state = AgentState(
        run_id="run-deadline-1",
        service_id="hd2ai-admin",
        owner_id="alice",
        messages=[GatewayMessage(role="user", content="slow")],
        max_steps=3,
        max_tool_calls=0,
        status="running",
        custom_client=SlowGateway(),
    )
    rt = LangGraphR1Runtime(trajectory_logger=TrajectoryLogger(TrajectoryEventStore(tmp_path / "dl.db")))
    res = await rt.execute(state, constraints)
    assert state.status == "failed"
    assert res["terminal_status"] == "timeout"


# ---------------------------------------------------------------------------
# QA-R1-09: Authentication & Secret Fail-closed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_09_auth_and_secret_fail_closed(monkeypatch):
    # 1) ConstraintSpec.authenticated default MUST be False (fail-closed)
    c_default = ConstraintSpec(service_id="hd2ai-admin", owner_id="u1")
    assert c_default.authenticated is False

    # 2) No hardcoded production AGTools token in app/config.py
    cfg_src = (Path(__file__).resolve().parents[1] / "app" / "config.py").read_text(encoding="utf-8")
    assert "hd2-doc-secret-token-key-2026" not in cfg_src

    # 3) Empty/whitespace token rejected fail-closed by auth helper
    from app.core.auth import validate_service_token

    assert validate_service_token("", "valid-tok") is False
    assert validate_service_token("   ", "valid-tok") is False
    assert validate_service_token("wrong", "valid-tok") is False
    assert validate_service_token("valid-tok", "valid-tok") is True

    # 4) In production mode, unauthenticated admin config & forged header MUST be rejected
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SERVICE_TOKENS", "prod-secret-token")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_forged = await client.get(
            "/api/v1/config/tools/snapshot",
            headers={"X-HD2Lab-Role": "admiral", "X-HD2Lab-User": "spoof"},
        )
        assert r_forged.status_code in (401, 403)

        r_bad_cl = await client.post(
            "/api/v1/agent/runs",
            headers={"Content-Length": "not-an-int"},
            content=b"{}",
        )
        assert r_bad_cl.status_code == 400
