"""Phase R1 Amendment 02 Release Gates: REG-POL-11 through REG-POL-20."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import FrozenInstanceError
from types import MappingProxyType
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.core.clock import MockClock
from app.main import app
from app.runtime.langgraph_r1 import LangGraphR1Runtime
from app.runtime.policy.ag_policy import DefaultAGPolicy, PolicyProposal, StateView
from app.runtime.policy.constraints import ActionType, ConstraintSpec
from app.runtime.policy.validator import ActionValidator, FallbackAction, RejectionCategory
from app.runtime.runner import runner
from app.runtime.state import AgentState
from app.schemas.agent import AgentRunRequest
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tracing.store import DuplicateEventError, TrajectoryEventStore, TrajectoryLogger
from tests.replay.harness import RecordedGatewayClient, RecordedGatewayStep, run_deterministic_replay


# ---------------------------------------------------------------------------
# REG-POL-11: AGPolicy direct state mutation & tool execution blocked
# ---------------------------------------------------------------------------
def test_reg_pol_11_agpolicy_cannot_mutate_state_or_execute_tools():
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin",
        owner_id="tester",
        allowed_tools=["system.echo"],
        max_steps=5,
        max_tool_calls=4,
        time_budget_seconds=60.0,
        context_budget_tokens=4000,
        check_live_tool_policy=False,
    )
    sv = StateView(
        run_id="run-imm-1",
        service_id="hd2ai-admin",
        step_count=0,
        tool_call_count=0,
        elapsed_seconds=0.0,
        remaining_time_budget_seconds=60.0,
        estimated_context_tokens=100,
        has_pending_tool_calls=False,
    )
    # 1. StateView is frozen (immutable) and exposes no tool dispatcher or gateway client
    with pytest.raises(FrozenInstanceError):
        sv.step_count = 99  # type: ignore[misc]
    assert not hasattr(sv, "dispatcher")
    assert not hasattr(sv, "custom_client")
    assert not hasattr(sv, "messages")

    # 2. Policy returns a frozen Proposal only
    proposal = DefaultAGPolicy().propose(sv, constraints)
    assert proposal.chosen_action == ActionType.CALL_MODEL.value
    with pytest.raises(FrozenInstanceError):
        proposal.chosen_action = ActionType.TERMINATE.value  # type: ignore[misc]


# ---------------------------------------------------------------------------
# REG-POL-12: Validator Reject -> Deterministic Safe Fallback (all 8 categories)
# ---------------------------------------------------------------------------
def test_reg_pol_12_validator_reject_and_safe_fallback():
    validator = ActionValidator()
    base_sv = StateView(
        run_id="run-val-1",
        service_id="hd2ai-public",
        step_count=0,
        tool_call_count=0,
        elapsed_seconds=1.0,
        remaining_time_budget_seconds=59.0,
        estimated_context_tokens=500,
        has_pending_tool_calls=False,
    )
    base_constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-public",
        owner_id="guest",
        authenticated=True,
        allowed_tools=["system.echo"],
        max_steps=4,
        max_tool_calls=2,
        time_budget_seconds=60.0,
        context_budget_tokens=2000,
        check_live_tool_policy=False,
    )

    # 1. Auth failure -> DENY (no data exposure)
    unauth = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="anon", allowed_tools=["system.echo"],
        max_steps=4, max_tool_calls=2, time_budget_seconds=60.0, context_budget_tokens=2000,
        authenticated=False, check_live_tool_policy=False,
    )
    r_auth = validator.validate(PolicyProposal("step_action", ActionType.CALL_MODEL.value), base_sv, unauth)
    assert not r_auth.accepted and r_auth.reason_code == RejectionCategory.AUTH_FAILURE.value
    assert r_auth.fallback_action and r_auth.fallback_action.action == ActionType.DENY.value
    assert r_auth.fallback_action.parameters.get("expose_data") is False

    # 2. Permission violation -> DENY (never SAFE_RESPOND with denied data)
    r_perm = validator.validate(
        PolicyProposal("tool_dispatch", ActionType.CALL_TOOLS.value, MappingProxyType({"tool_names": ("admin.secret_tool",), "tool_count": 1})),
        base_sv,
        base_constraints,
    )
    assert not r_perm.accepted and r_perm.reason_code == RejectionCategory.PERMISSION_VIOLATION.value
    assert r_perm.fallback_action and r_perm.fallback_action.action == ActionType.DENY.value
    assert r_perm.fallback_action.parameters.get("expose_data") is False

    # 3. Safety policy violation -> DENY
    unsafe = ConstraintSpec.from_runtime(
        service_id="hd2ai-public", owner_id="guest", authenticated=True, allowed_tools=["system.echo"],
        max_steps=4, max_tool_calls=2, time_budget_seconds=60.0, context_budget_tokens=2000,
        safety_blocked=True, safety_reason="blocked_policy", check_live_tool_policy=False,
    )
    r_safe = validator.validate(PolicyProposal("step_action", ActionType.CALL_MODEL.value), base_sv, unsafe)
    assert not r_safe.accepted and r_safe.reason_code == RejectionCategory.SAFETY_POLICY_VIOLATION.value
    assert r_safe.fallback_action and r_safe.fallback_action.action == ActionType.DENY.value

    # 4. Timeout exceeded -> TERMINATE
    sv_timeout = StateView(**{**base_sv.__dict__, "remaining_time_budget_seconds": 0.0})
    r_time = validator.validate(PolicyProposal("step_action", ActionType.CALL_MODEL.value), sv_timeout, base_constraints)
    assert not r_time.accepted and r_time.reason_code == RejectionCategory.TIMEOUT_EXCEEDED.value
    assert r_time.fallback_action and r_time.fallback_action.action == ActionType.TERMINATE.value

    # 5. Tool call budget exceeded -> SAFE_RESPOND
    sv_tbudget = StateView(**{**base_sv.__dict__, "tool_call_count": 2})
    r_tbudget = validator.validate(
        PolicyProposal("tool_dispatch", ActionType.CALL_TOOLS.value, MappingProxyType({"tool_names": ("system.echo",), "tool_count": 1})),
        sv_tbudget,
        base_constraints,
    )
    assert not r_tbudget.accepted and r_tbudget.reason_code == RejectionCategory.TOOL_CALL_BUDGET_EXCEEDED.value
    assert r_tbudget.fallback_action and r_tbudget.fallback_action.action == ActionType.SAFE_RESPOND.value

    # 6. Context budget insufficient -> COMPACT_CONTEXT
    sv_ctx = StateView(**{**base_sv.__dict__, "estimated_context_tokens": 5000})
    r_ctx = validator.validate(PolicyProposal("step_action", ActionType.CALL_MODEL.value), sv_ctx, base_constraints)
    assert not r_ctx.accepted and r_ctx.reason_code == RejectionCategory.CONTEXT_BUDGET_INSUFFICIENT.value
    assert r_ctx.fallback_action and r_ctx.fallback_action.action == ActionType.COMPACT_CONTEXT.value

    # 7. Invalid policy parameters -> DEFAULT_CONFIG
    r_param = validator.validate(
        PolicyProposal("step_action", ActionType.CALL_MODEL.value, MappingProxyType({"reasoning_effort": "invalid_xyz", "context_budget_tokens": 999999})),
        base_sv,
        base_constraints,
    )
    assert not r_param.accepted and r_param.reason_code == RejectionCategory.INVALID_POLICY_PARAMETERS.value
    assert r_param.fallback_action and r_param.fallback_action.action == ActionType.DEFAULT_CONFIG.value

    # 8. Unsupported action -> TERMINATE
    r_unsup = validator.validate(PolicyProposal("step_action", "EXEC_SHELL_ARBITRARY"), base_sv, base_constraints)
    assert not r_unsup.accepted and r_unsup.reason_code == RejectionCategory.UNSUPPORTED_ACTION.value
    assert r_unsup.fallback_action and r_unsup.fallback_action.action == ActionType.TERMINATE.value


# ---------------------------------------------------------------------------
# REG-POL-13: Fallback permission re-validation
# ---------------------------------------------------------------------------
def test_reg_pol_13_fallback_permission_revalidation():
    validator = ActionValidator()
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-public",
        owner_id="guest",
        authenticated=True,
        allowed_tools=["system.echo"],
        max_steps=4,
        max_tool_calls=2,
        time_budget_seconds=60.0,
        context_budget_tokens=2000,
        check_live_tool_policy=False,
    )
    sv = StateView(
        run_id="run-fb-1", service_id="hd2ai-public", step_count=0, tool_call_count=0,
        elapsed_seconds=0.0, remaining_time_budget_seconds=60.0, estimated_context_tokens=200,
        has_pending_tool_calls=False,
    )
    # A fallback attempting an unauthorized tool MUST be rejected and escalated to DENY
    bad_fb = FallbackAction(
        action=ActionType.CALL_TOOLS.value,
        parameters=MappingProxyType({"tool_names": ("forbidden.admin_tool",)}),
        reason_code="bad_fallback",
    )
    res = validator.validate_fallback(bad_fb, sv, constraints)
    assert not res.accepted
    assert res.reason_code == RejectionCategory.PERMISSION_VIOLATION.value
    assert res.fallback_action and res.fallback_action.action == ActionType.DENY.value


# ---------------------------------------------------------------------------
# REG-POL-14: Prevent infinite retry loop
# ---------------------------------------------------------------------------
def test_reg_pol_14_no_infinite_retry_loop():
    validator = ActionValidator()
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="u1", authenticated=True, allowed_tools=["system.echo"],
        max_steps=5, max_tool_calls=2, time_budget_seconds=60.0, context_budget_tokens=2000,
        check_live_tool_policy=False,
    )
    prop = PolicyProposal("step_action", ActionType.CALL_MODEL.value, MappingProxyType({"reasoning_effort": "bad"}))
    sv_already_rejected = StateView(
        run_id="run-loop-1", service_id="hd2ai-admin", step_count=1, tool_call_count=0,
        elapsed_seconds=1.0, remaining_time_budget_seconds=59.0, estimated_context_tokens=100,
        has_pending_tool_calls=False, fallback_count=1, rejected_signatures=frozenset({prop.signature()}),
    )
    res = validator.validate(prop, sv_already_rejected, constraints)
    assert not res.accepted
    assert res.reason_code == RejectionCategory.RETRY_LOOP_PREVENTED.value
    assert res.fallback_action and res.fallback_action.action == ActionType.TERMINATE.value


# ---------------------------------------------------------------------------
# REG-POL-15, REG-POL-16, REG-POL-17: Append-only integrity, Crash recovery, UNKNOWN outcome
# ---------------------------------------------------------------------------
def test_reg_pol_15_16_17_append_only_crash_and_unknown_outcome(tmp_path):
    db_file = tmp_path / "traj.db"
    clock = MockClock(start_epoch=1795600000.0)
    store = TrajectoryEventStore(db_file)
    tlogger = TrajectoryLogger(store, clock=clock)

    dec = tlogger.log_decision(
        trajectory_id="traj-1", run_id="run-1", decision_id="dec-1", step_index=1,
        decision_type="tool_dispatch", state_features={"step_count": 0},
        allowed_actions=["CALL_TOOLS", "SAFE_RESPOND"], constraints={"max_tool_calls": 1},
        chosen_action="CALL_TOOLS", proposed_parameters={"tool_names": ["system.echo"]},
        behavior_policy_version="agpolicy-r1-v1", config_digest="abcd1234",
        action_probability=None,
        validator_result={"accepted": False, "reason_code": "tool_call_budget_exceeded", "violated_constraints": ["max_tool_calls"]},
        event_id="evt-unique-1",
    )
    # REG-POL-15: Duplicate event_id rejected
    with pytest.raises(DuplicateEventError):
        tlogger.log_decision(
            trajectory_id="traj-1", run_id="run-1", decision_id="dec-1", step_index=1,
            decision_type="tool_dispatch", state_features={}, allowed_actions=[], constraints={},
            chosen_action="CALL_TOOLS", proposed_parameters={}, behavior_policy_version="v1",
            config_digest="abcd1234", action_probability=None, validator_result={},
            event_id="evt-unique-1",
        )

    # REG-POL-15: Engine-level UPDATE and DELETE forbidden
    conn = sqlite3.connect(str(db_file))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE trajectory_events SET event_type = 'HACKED' WHERE event_id = ?", (dec.event_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM trajectory_events WHERE event_id = ?", (dec.event_id,))
    finally:
        conn.close()

    # Log ActionEvent (fallback) WITHOUT OutcomeEvent -> simulates Crash before completion
    tlogger.log_action(
        trajectory_id="traj-1", run_id="run-1", decision_id="dec-1", action_id="act-1",
        executed_action="SAFE_RESPOND", executed_parameters={"disable_tools": True},
        fallback_applied=True,
    )

    # REG-POL-16: Reopen store cleanly after crash and recover prior events
    reopened = TrajectoryEventStore(db_file)
    reconstructed = reopened.reconstruct_trajectory(trajectory_id="traj-1")
    assert len(reconstructed["steps"]) == 1
    step = reconstructed["steps"][0]
    # Fallback is NOT recorded as the policy's originally chosen action
    assert step["chosen_action"] == "CALL_TOOLS"
    assert step["action_probability"] is None
    assert step["executions"][0]["action"]["executed_action"] == "SAFE_RESPOND"
    assert step["executions"][0]["action"]["fallback_applied"] is True

    # REG-POL-17: Missing OutcomeEvent -> UNKNOWN with no fabricated reward
    assert step["executions"][0]["outcome"]["outcome"] == "UNKNOWN"
    assert step["executions"][0]["outcome"]["reward_signals"] == {}
    assert reconstructed["integrity"]["missing_outcomes"] == 1
    assert reconstructed["terminal_status"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# REG-POL-18: Delayed Reward & Feedback linkage via API
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reg_pol_18_delayed_feedback_linkage(tmp_path, monkeypatch):
    db_file = tmp_path / "feedback.db"
    store = TrajectoryEventStore(db_file)
    tlogger = TrajectoryLogger(store, clock=MockClock())
    tlogger.log_decision(
        trajectory_id="traj-fb-1", run_id="run-fbapi001", decision_id="dec-fb-1", step_index=1,
        decision_type="step_action", state_features={}, allowed_actions=["CALL_MODEL"],
        constraints={}, chosen_action="CALL_MODEL", proposed_parameters={},
        behavior_policy_version="agpolicy-r1-v1", config_digest="1122",
        action_probability=None, validator_result={"accepted": True},
        owner_id="default_user", service_id="hd2ai-admin", tenant_id="default_tenant",
    )
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/runs/run-fbapi001/feedback",
            json={"feedback_type": "thumbs_up", "reward_signal": 1.0, "decision_id": "dec-fb-1"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["trajectory_id"] == "traj-fb-1"
        assert body["reward_signal"] == 1.0

        r2 = await client.get("/api/v1/agent/runs/run-fbapi001/trajectory", headers={"X-HD2Lab-Role": "admin"})
        assert r2.status_code == 200
        traj = r2.json()
        assert len(traj["feedback"]) == 1
        assert traj["feedback"][0]["feedback_type"] == "thumbs_up"
        assert len(traj["steps"][0]["feedback"]) == 1


# ---------------------------------------------------------------------------
# REG-POL-19: MockClock-based Deterministic Replay Reproducibility
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reg_pol_19_deterministic_mock_clock_replay(tmp_path):
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin",
        owner_id="replay-user",
        authenticated=True,
        allowed_tools=["system.echo"],
        max_steps=6,
        max_tool_calls=4,
        time_budget_seconds=10.0,
        context_budget_tokens=4000,
        check_live_tool_policy=False,
    )
    recorded = [
        RecordedGatewayStep(
            content=None,
            tool_calls=[ToolCall(id="call_echo_1", function=FunctionCall(name="system.echo", arguments='{"message": "ping"}'))],
            finish_reason="tool_calls",
            advance_seconds=1.5,
        ),
        RecordedGatewayStep(
            content="에코 결과는 ping 입니다.",
            tool_calls=None,
            finish_reason="stop",
            advance_seconds=2.0,
        ),
    ]

    snap_a = await run_deterministic_replay(
        message="ping 에코해줘",
        recorded_steps=recorded,
        constraints=constraints,
        db_path=str(tmp_path / "replay_a.db"),
        run_id="run-replay-a",
    )
    snap_b = await run_deterministic_replay(
        message="ping 에코해줘",
        recorded_steps=recorded,
        constraints=constraints,
        db_path=str(tmp_path / "replay_b.db"),
        run_id="run-replay-b",
    )
    assert snap_a == snap_b
    assert snap_a["tool_selection"] == ["system.echo"]
    assert snap_a["terminal_state"] == "completed"
    assert snap_a["budget_consumption"]["remaining_time_budget_seconds"] == pytest.approx(6.5, abs=0.01)

    # Also verify MockClock strictly enforces remaining_time_budget_seconds timeout
    timeout_steps = [
        RecordedGatewayStep(
            content=None,
            tool_calls=[ToolCall(id="call_echo_2", function=FunctionCall(name="system.echo", arguments='{"message": "slow"}'))],
            finish_reason="tool_calls",
            advance_seconds=12.0,  # exceeds 10.0s time budget on MockClock
        ),
    ]
    snap_timeout = await run_deterministic_replay(
        message="slow",
        recorded_steps=timeout_steps,
        constraints=constraints,
        db_path=str(tmp_path / "replay_timeout.db"),
        run_id="run-replay-timeout",
    )
    assert snap_timeout["terminal_state"] == "timeout"
    assert snap_timeout["budget_consumption"]["remaining_time_budget_seconds"] == 0.0
    assert snap_timeout["fallback_results"][0]["reason_code"] == "timeout_exceeded"


# ---------------------------------------------------------------------------
# REG-POL-20: Telemetry sensitive data & raw user text redaction
# ---------------------------------------------------------------------------
def test_reg_pol_20_telemetry_redacts_secrets_and_raw_user_text(tmp_path):
    db_file = tmp_path / "sanitize.db"
    store = TrajectoryEventStore(db_file)
    tlogger = TrajectoryLogger(store, clock=MockClock())

    secret_token = "sk-live-SUPERSECRET9876543210"
    raw_user_prompt = "내 주민번호와 비밀 메모를 그대로 저장하지 마세요"

    tlogger.log_decision(
        trajectory_id="traj-sec-1", run_id="run-sec-1", decision_id="dec-sec-1", step_index=1,
        decision_type="tool_dispatch",
        state_features={"step_count": 1, "user_input": raw_user_prompt, "api_key": secret_token},
        allowed_actions=["CALL_TOOLS"], constraints={"service_id": "hd2ai-admin"},
        chosen_action="CALL_TOOLS",
        proposed_parameters={"tool_names": ["system.echo"], "message": raw_user_prompt, "authorization": f"Bearer {secret_token}"},
        behavior_policy_version="agpolicy-r1-v1", config_digest="abcd",
        action_probability=None, validator_result={"accepted": True},
    )

    raw_rows = store.list_events(trajectory_id="traj-sec-1")
    serialized = json.dumps(raw_rows, ensure_ascii=False)
    assert secret_token not in serialized
    assert raw_user_prompt not in serialized
    assert raw_rows[0]["proposed_parameters"]["tool_names"] == ["system.echo"]
    assert raw_rows[0]["proposed_parameters"]["message"]["redacted"] is True


# ---------------------------------------------------------------------------
# Feature Flag verification (default legacy, opt-in langgraph_r1)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_feature_flag_default_legacy_and_r1_opt_in(tmp_path, monkeypatch):
    assert settings.RUNTIME_ENGINE == "legacy"

    db_file = tmp_path / "flag_r1.db"
    store = TrajectoryEventStore(db_file)
    tlogger = TrajectoryLogger(store, clock=MockClock())
    monkeypatch.setattr(settings, "RUNTIME_ENGINE", "langgraph_r1")
    monkeypatch.setattr("app.tracing.store.get_trajectory_logger", lambda: tlogger)

    req = AgentRunRequest(message="오늘 날짜 알려줘", stream=True)
    session = await runner.create_run(req)
    await runner.start_run(session)

    events = []
    async for sse in runner.stream_events(session.state.run_id):
        events.append(sse)
        if "event: run.completed" in sse or "event: run.failed" in sse:
            break

    assert any("event: run.started" in e for e in events)
    assert any("event: run.completed" in e and '"runtime_engine": "langgraph_r1"' in e for e in events)
    reconstructed = store.reconstruct_trajectory(run_id=session.state.run_id)
    assert reconstructed["integrity"]["event_count"] >= 3
    assert reconstructed["integrity"]["missing_outcomes"] == 0
