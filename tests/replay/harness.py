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
        step = self._steps[min(self._idx, len(self._steps) - 1)]
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
