"""FIX-R1-04: Deterministic Replay Harness with MockClock and Recorded Fixtures."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.clock import MockClock
from app.runtime.langgraph_r1 import LangGraphR1Runtime
from app.runtime.policy.ag_policy import AGPolicy, DefaultAGPolicy
from app.runtime.policy.constraints import ConstraintSpec
from app.runtime.policy.validator import ActionValidator
from app.runtime.state import AgentState
from app.schemas.gateway import GatewayMessage, ToolCall
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


@dataclass(frozen=True)
class RecordedGatewayStep:
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    finish_reason: str = "stop"
    advance_seconds: float = 0.25


class RecordedGatewayClient:
    """Deterministic Gateway fixture that advances MockClock by fixed simulated durations."""

    def __init__(self, steps: List[RecordedGatewayStep], clock: MockClock):
        self._steps = list(steps)
        self._clock = clock
        self._idx = 0

    def remaining_unused(self) -> int:
        return max(0, len(self._steps) - self._idx)

    async def execute(
        self,
        *,
        messages: List[GatewayMessage],
        tools: Optional[Any] = None,
        tool_choice: str = "auto",
        stream: bool = False,
        reasoning_effort: Optional[str] = None,
        on_reasoning_delta: Optional[Callable] = None,
        on_content_delta: Optional[Callable] = None,
        **kwargs: Any,
    ) -> Tuple[Optional[str], Optional[List[ToolCall]], str]:
        if self._idx >= len(self._steps):
            raise AssertionError(
                f"RecordedGatewayClient fixture exhausted after {len(self._steps)} call(s)"
            )
        step = self._steps[self._idx]
        self._idx += 1
        self._clock.advance(step.advance_seconds)
        if step.content and on_content_delta:
            await on_content_delta(step.content)
        return step.content, step.tool_calls, step.finish_reason


async def run_deterministic_replay(
    *,
    message: str,
    recorded_steps: List[RecordedGatewayStep],
    constraints: ConstraintSpec,
    db_path: str,
    policy: Optional[AGPolicy] = None,
    seed: int = 42,
    start_epoch: float = 1795600000.0,
    run_id: str = "run-replay-0001",
) -> Dict[str, Any]:
    """Executes LangGraphR1Runtime in a strictly deterministic environment and returns normalized snapshot."""
    random.seed(seed)
    clock = MockClock(start_epoch=start_epoch, start_monotonic=1000.0)
    client = RecordedGatewayClient(recorded_steps, clock=clock)
    store = TrajectoryEventStore(db_path)
    tlogger = TrajectoryLogger(store, clock=clock)

    state = AgentState(
        run_id=run_id,
        session_id="sess-replay",
        service_id=constraints.service_id,
        owner_id=constraints.owner_id,
        messages=[GatewayMessage(role="user", content=message)],
        max_steps=constraints.max_steps,
        max_tool_calls=constraints.max_tool_calls,
        status="running",
        stream=False,
        reasoning_effort=constraints.default_reasoning_effort,
        custom_client=client,
    )

    runtime = LangGraphR1Runtime(
        policy=policy or DefaultAGPolicy(),
        validator=ActionValidator(),
        trajectory_logger=tlogger,
        clock=clock,
    )
    res = await runtime.execute(state, constraints, trajectory_id=f"traj-{run_id}")
    remaining_time = round(max(0.0, constraints.time_budget_seconds - (clock.monotonic() - 1000.0)), 4)

    # Normalize non-deterministic identifiers (timestamp, run_id, event_id, wall-clock latency)
    # while keeping remaining_time_budget_seconds strictly controlled by MockClock.
    return {
        "node_order": res.get("node_trace", []),
        "policy_decisions": [
            {
                "step_index": d["step_index"],
                "chosen_action": d["chosen_action"],
                "proposed_parameters": d["proposed_parameters"],
                "action_probability": d["action_probability"],
            }
            for d in res.get("decision_records", [])
        ],
        "tool_selection": [tr.tool_name for tr in state.traces],
        "tool_arguments": [tr.arguments for tr in state.traces],
        "validator_results": [d["validator_result"] for d in res.get("decision_records", [])],
        "budget_consumption": {
            "steps": state.step_count,
            "tool_calls": state.tool_call_count,
            "remaining_time_budget_seconds": remaining_time,
        },
        "citations": state.citations,
        "terminal_state": res.get("terminal_status") or state.status,
        "final_answer": state.final_answer,
        "fallback_results": [
            {
                "action": f["action"],
                "reason_code": f["reason_code"],
                "outcome": f["outcome"],
                "terminal_status": f["terminal_status"],
            }
            for f in res.get("fallback_records", [])
        ],
    }


async def run_legacy_replay(
    *,
    message: str,
    recorded_steps: List[RecordedGatewayStep],
    constraints: ConstraintSpec,
    seed: int = 42,
    start_epoch: float = 1795600000.0,
    run_id: str = "run-legacy-0001",
) -> Dict[str, Any]:
    """Executes the legacy model_node/tool_node loop with the exact same RecordedGatewayClient & MockClock."""
    from app.runtime.graph import model_node, tool_node

    random.seed(seed)
    clock = MockClock(start_epoch=start_epoch, start_monotonic=1000.0)
    client = RecordedGatewayClient(recorded_steps, clock=clock)
    state = AgentState(
        run_id=run_id,
        session_id="sess-replay",
        service_id=constraints.service_id,
        owner_id=constraints.owner_id,
        messages=[GatewayMessage(role="user", content=message)],
        max_steps=constraints.max_steps,
        max_tool_calls=constraints.max_tool_calls,
        status="running",
        stream=False,
        reasoning_effort=constraints.default_reasoning_effort,
        custom_client=client,
    )

    while state.step_count < state.max_steps and state.status == "running":
        m_res = await model_node(state)
        for k, v in m_res.items():
            setattr(state, k, v)
        if state.pending_tool_calls and state.tool_call_count < state.max_tool_calls:
            t_res = await tool_node(state, constraints=constraints)
            for k, v in t_res.items():
                setattr(state, k, v)
        else:
            break

    if state.status == "running" and state.final_answer is not None and not state.pending_tool_calls:
        state.status = "completed"

    remaining_time = round(max(0.0, constraints.time_budget_seconds - (clock.monotonic() - 1000.0)), 4)
    return {
        "tool_selection": [tr.tool_name for tr in state.traces],
        "tool_arguments": [tr.arguments for tr in state.traces],
        "budget_consumption": {
            "steps": state.step_count,
            "tool_calls": state.tool_call_count,
            "remaining_time_budget_seconds": remaining_time,
        },
        "citations": state.citations,
        "terminal_state": state.status,
        "final_answer": state.final_answer,
    }


async def run_agent_runner_replay(
    *,
    engine: str,
    message: str,
    recorded_steps: List[RecordedGatewayStep],
    db_path: str,
    monkeypatch: Any,
) -> Dict[str, Any]:
    """FIX-210: Full production AgentRunner replay under legacy or langgraph_r1 with identical fixtures."""
    from app.config import settings
    from app.runtime.runner import runner
    from app.schemas.agent import AgentRunRequest

    clock = MockClock()
    client = RecordedGatewayClient(recorded_steps, clock=clock)
    store = TrajectoryEventStore(db_path)
    tlogger = TrajectoryLogger(store, clock=clock)

    monkeypatch.setattr(settings, "RUNTIME_ENGINE", engine)
    monkeypatch.setattr("app.tracing.store.get_trajectory_logger", lambda: tlogger)

    req = AgentRunRequest(message=message, stream=True)
    session = await runner.create_run(req, owner_id="replay-user", service_id="hd2ai-admin", custom_client=client)
    await runner.start_run(session)

    sse_events: List[str] = []
    async for frame in runner.stream_events(session.state.run_id):
        sse_events.append(frame)
        if "event: run.completed" in frame or "event: run.failed" in frame or "event: run.cancelled" in frame:
            break
    if session.task:
        await session.task

    return {
        "engine": engine,
        "status": session.state.status,
        "final_answer": session.state.final_answer,
        "tool_selection": [tr.tool_name for tr in session.state.traces],
        "tool_arguments": [tr.arguments for tr in session.state.traces],
        "sse_event_count": len(sse_events),
        "unused_fixtures": client.remaining_unused(),
    }
