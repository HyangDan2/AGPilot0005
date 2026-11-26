"""R1.2 RC Hotfix — Checkpoint 2 (P1) Fail-First Tests: CR-04 .. CR-09."""
from __future__ import annotations

import asyncio
import sqlite3
from types import MappingProxyType

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.clock import MockClock
from app.main import app
from app.runtime.event_bus import RunEventBus
from app.runtime.langgraph_r1 import LangGraphR1Runtime
from app.runtime.policy.ag_policy import DecisionType, PolicyProposal, StateView
from app.runtime.policy.constraints import ActionType, ConstraintSpec
from app.runtime.runner import AgentRunner
from app.runtime.state import AgentState
from app.schemas.agent import AgentRunRequest
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tracing.async_writer import AsyncTrajectoryWriter
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# CR-04: Policy-Approved Context Budget Passed End-to-End into LangGraph tool_node
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cr_04_approved_budget_propagates_to_tool_node_in_langgraph(tmp_path, monkeypatch):
    observed_budgets = []
    from app.runtime import graph as graph_mod

    orig_tool_node = graph_mod.tool_node

    async def spy_tool_node(*args, **kwargs):
        observed_budgets.append(kwargs.get("approved_context_budget"))
        return await orig_tool_node(*args, **kwargs)

    monkeypatch.setattr("app.runtime.langgraph_r1.tool_node", spy_tool_node)

    class BudgetPolicy:
        version = "budget-prop-v1"

        def propose(self, sv: StateView, c: ConstraintSpec) -> PolicyProposal:
            if sv.has_pending_tool_calls:
                return PolicyProposal(
                    DecisionType.TOOL_SELECTION.value,
                    ActionType.CALL_TOOLS.value,
                    MappingProxyType({"tool_call_ids": sv.pending_tool_call_ids}),
                    self.version,
                )
            if sv.has_final_answer:
                return PolicyProposal(DecisionType.TERMINATION.value, ActionType.TERMINATE.value, MappingProxyType({"terminal_status": "completed"}), self.version)
            return PolicyProposal(
                DecisionType.CONTEXT_ALLOCATION.value,
                ActionType.CALL_MODEL.value,
                MappingProxyType({"reasoning_effort": "low", "context_budget_tokens": 1500}),
                self.version,
            )

    class OneToolGW:
        def __init__(self):
            self.step = 0

        async def execute(self, **kwargs):
            self.step += 1
            if self.step == 1:
                return None, [ToolCall(id="tc_b1", function=FunctionCall(name="system.echo", arguments='{"message":"hi"}'))], "tool_calls"
            return "budget ok", None, "stop"

    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        allowed_tools=["system.echo"], max_steps=5, max_tool_calls=3,
        time_budget_seconds=30.0, context_budget_tokens=4000, check_live_tool_policy=False,
    )
    st = AgentState(
        run_id="run-cr04-1", service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        messages=[GatewayMessage(role="user", content="hi")],
        max_steps=5, max_tool_calls=3, status="running", custom_client=OneToolGW(),
    )
    rt = LangGraphR1Runtime(
        policy=BudgetPolicy(),
        trajectory_logger=TrajectoryLogger(TrajectoryEventStore(tmp_path / "cr04.db"), clock=MockClock()),
        clock=MockClock(),
    )
    await rt.execute(st, constraints)
    assert observed_budgets == [1500]


# ---------------------------------------------------------------------------
# CR-05: Real Corrupted SQLite Dedup Index Detected via PRAGMA (No Manual Flag Flip!)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cr_05_real_corrupted_dedup_index_detected_without_flag_override(tmp_path, monkeypatch):
    db_file = tmp_path / "corrupt_idx.db"
    # Bootstrap valid schema first, then replace index with a WRONG-COLUMN unique index of the SAME NAME!
    TrajectoryEventStore(db_file)
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("DROP INDEX uq_trajectory_feedback_dedup;")
        conn.execute("CREATE UNIQUE INDEX uq_trajectory_feedback_dedup ON trajectory_events(event_id);")
        conn.commit()
    finally:
        conn.close()

    # Re-open store — MUST automatically detect wrong target column via PRAGMA index_info!
    reopened = TrajectoryEventStore(db_file)
    assert reopened.feedback_dedup_integrity_ok is False

    tlogger = TrajectoryLogger(reopened, clock=MockClock())
    tlogger.log_decision(
        trajectory_id="traj-cr05", run_id="run-cr05-1", decision_id="dec-1", step_index=1,
        decision_type="step_action", state_features={}, allowed_actions=["CALL_MODEL"],
        constraints={}, chosen_action="CALL_MODEL", proposed_parameters={},
        behavior_policy_version="agpolicy-r1-v1", config_digest="1111",
        action_probability=None, validator_result={"accepted": True},
        owner_id="alice", service_id="hd2ai-admin",
    )
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/runs/run-cr05-1/feedback",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"},
            json={"feedback_type": "thumbs_up"},
        )
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# CR-06: Queue Overflow Gap Detection (Never Silently Skip to Terminal Event)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cr_06_slow_subscriber_overflow_reports_gap_not_normal_terminal():
    bus = RunEventBus(run_id="run-cr06-1", max_buffer_size=32, subscriber_queue_size=2)
    gen = bus.stream()
    # Trigger subscription by publishing initial event & reading 1 frame
    bus.publish("run.started", "running", {})
    first = await gen.__anext__()
    assert "run.started" in first

    # Overflow subscriber queue (capacity=2) without reading
    for i in range(6):
        bus.publish("answer.delta", "running", {"delta": f"d{i}"})
    bus.publish_terminal("run.completed", "completed", {"answer": "done"})

    rest = [f async for f in gen]
    # Must NOT deliver run.completed after an overflow gap as if continuous!
    assert not any("event: run.completed" in f for f in rest)


# ---------------------------------------------------------------------------
# CR-07: Automatic Eviction Without Lookup & Active Run Admission Control
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cr_07_auto_eviction_on_create_and_active_run_admission_control():
    local_runner = AgentRunner(max_finished_sessions=2, max_active_runs=2)

    # 1) Active Run Admission Control: 3rd concurrent active run rejected
    s1 = await local_runner.create_run(AgentRunRequest(message="1", stream=False), owner_id="alice", service_id="hd2ai-admin")
    s2 = await local_runner.create_run(AgentRunRequest(message="2", stream=False), owner_id="alice", service_id="hd2ai-admin")
    with pytest.raises(RuntimeError, match="active_run_limit_exceeded"):
        await local_runner.create_run(AgentRunRequest(message="3", stream=False), owner_id="alice", service_id="hd2ai-admin")

    # 2) Mark finished -> creating new runs auto-evicts oldest finished beyond max_finished_sessions=2 (without any lookup!)
    s1.state.status = "completed"
    s1.finished_at = 100.0
    s2.state.status = "completed"
    s2.finished_at = 101.0
    s3 = await local_runner.create_run(AgentRunRequest(message="3", stream=False), owner_id="alice", service_id="hd2ai-admin")
    s3.state.status = "completed"
    s3.finished_at = 102.0
    await local_runner.create_run(AgentRunRequest(message="4", stream=False), owner_id="alice", service_id="hd2ai-admin")
    # s1 must have been auto-evicted into tombstones
    assert s1.state.run_id not in local_runner.sessions


# ---------------------------------------------------------------------------
# CR-08: Dirty Marker Preserved on Real SQLite Failure & Cleared Only After Recovery Commit
# ---------------------------------------------------------------------------
def test_cr_08_dirty_marker_preserved_on_db_failure_and_cleared_after_recovery(tmp_path):
    store = TrajectoryEventStore(tmp_path / "cr08.db")
    orig_mark = store.mark_incomplete
    fail_flag = {"should_fail": True}

    def flaky_mark(tid: str, reason: str, recorded_at=None):
        if fail_flag["should_fail"]:
            raise sqlite3.OperationalError("disk I/O error")
        return orig_mark(tid, reason, recorded_at=recorded_at)

    store.mark_incomplete = flaky_mark  # type: ignore[method-assign]
    writer = AsyncTrajectoryWriter(store, queue_capacity=4)
    writer.mark_dirty("traj-flaky-1", "queue_overflow")
    writer.flush(timeout_seconds=1.5)

    # Commit FAILED -> dirty marker MUST remain in writer.dirty_trajectories and NOT be in DB yet!
    assert "traj-flaky-1" in writer.dirty_trajectories
    assert "traj-flaky-1" not in store.list_incomplete_trajectories()

    # Recover DB -> next flush MUST commit and clear dirty marker!
    fail_flag["should_fail"] = False
    writer.flush(timeout_seconds=2.0)
    assert "traj-flaky-1" not in writer.dirty_trajectories
    assert "traj-flaky-1" in store.list_incomplete_trajectories()
    writer.shutdown(timeout_seconds=2.0)


# ---------------------------------------------------------------------------
# CR-09: Trajectory Completeness Proof (Orphan/Missing Bundle/Missing Completion Metadata Excluded)
# ---------------------------------------------------------------------------
def test_cr_09_trajectory_completeness_proof_and_strict_export(tmp_path):
    store = TrajectoryEventStore(tmp_path / "cr09.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())

    # 1) Trajectory with Decision+Action+Outcome(terminal) BUT missing Completion Metadata (expected_event_count)
    tlogger.log_decision(
        trajectory_id="traj-no-meta", run_id="run-nm-1", decision_id="dec-1", step_index=1,
        decision_type="step_action", state_features={}, allowed_actions=["CALL_MODEL"],
        constraints={}, chosen_action="CALL_MODEL", proposed_parameters={},
        behavior_policy_version="v1", config_digest="1111", action_probability=None,
        validator_result={"accepted": True}, owner_id="alice", service_id="hd2ai-admin",
    )
    tlogger.log_action(
        trajectory_id="traj-no-meta", run_id="run-nm-1", decision_id="dec-1", action_id="act-1",
        executed_action="CALL_MODEL", executed_parameters={}, fallback_applied=False,
        owner_id="alice", service_id="hd2ai-admin",
    )
    tlogger.log_outcome(
        trajectory_id="traj-no-meta", run_id="run-nm-1", decision_id="dec-1", action_id="act-1",
        outcome="success", next_state_features={}, terminal_status="completed",
        owner_id="alice", service_id="hd2ai-admin",
    )
    rec_no_meta = store.reconstruct_trajectory(trajectory_id="traj-no-meta")
    assert rec_no_meta["eligible_for_training"] is False

    # 2) Trajectory with expected_event_count=6 when only 3 events stored (whole bundle dropped!) -> excluded!
    tlogger.log_decision(
        trajectory_id="traj-dropped-bundle", run_id="run-db-1", decision_id="dec-b1", step_index=1,
        decision_type="step_action", state_features={}, allowed_actions=["CALL_MODEL"],
        constraints={}, chosen_action="CALL_MODEL", proposed_parameters={},
        behavior_policy_version="v1", config_digest="1111", action_probability=None,
        validator_result={"accepted": True}, owner_id="alice", service_id="hd2ai-admin",
    )
    tlogger.log_action(
        trajectory_id="traj-dropped-bundle", run_id="run-db-1", decision_id="dec-b1", action_id="act-b1",
        executed_action="CALL_MODEL", executed_parameters={}, fallback_applied=False,
        owner_id="alice", service_id="hd2ai-admin",
    )
    tlogger.log_outcome(
        trajectory_id="traj-dropped-bundle", run_id="run-db-1", decision_id="dec-b1", action_id="act-b1",
        outcome="success", next_state_features={"expected_event_count": 6}, terminal_status="completed",
        owner_id="alice", service_id="hd2ai-admin",
    )
    rec_dropped = store.reconstruct_trajectory(trajectory_id="traj-dropped-bundle")
    assert rec_dropped["eligible_for_training"] is False

    # 3) Complete valid trajectory with matching expected_event_count=3 -> eligible!
    tlogger.log_decision(
        trajectory_id="traj-valid", run_id="run-v-1", decision_id="dec-v1", step_index=1,
        decision_type="step_action", state_features={}, allowed_actions=["CALL_MODEL"],
        constraints={}, chosen_action="CALL_MODEL", proposed_parameters={},
        behavior_policy_version="v1", config_digest="1111", action_probability=None,
        validator_result={"accepted": True}, owner_id="alice", service_id="hd2ai-admin",
    )
    tlogger.log_action(
        trajectory_id="traj-valid", run_id="run-v-1", decision_id="dec-v1", action_id="act-v1",
        executed_action="CALL_MODEL", executed_parameters={}, fallback_applied=False,
        owner_id="alice", service_id="hd2ai-admin",
    )
    tlogger.log_outcome(
        trajectory_id="traj-valid", run_id="run-v-1", decision_id="dec-v1", action_id="act-v1",
        outcome="success", next_state_features={"expected_event_count": 3}, terminal_status="completed",
        owner_id="alice", service_id="hd2ai-admin",
    )
    rec_valid = store.reconstruct_trajectory(trajectory_id="traj-valid")
    assert rec_valid["eligible_for_training"] is True
    assert store.export_training_jsonl(tmp_path / "train.jsonl", dirty_trajectories=set(), dirty_overflow=False) == 1
