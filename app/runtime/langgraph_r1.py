"""Phase R1 Amendment 02: LangGraph StateGraph Runtime with Execution Boundary & Trajectory Integrity."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Literal, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app.core.clock import Clock, system_clock
from app.gateway.client import GatewayStreamError
from app.runtime.context import context_manager
from app.runtime.graph import model_node, tool_node
from app.runtime.policy.ag_policy import AGPolicy, DefaultAGPolicy, PolicyProposal, StateView
from app.runtime.policy.constraints import ActionType, ConstraintSpec
from app.runtime.policy.validator import ActionValidator, FallbackAction, ValidationResult
from app.runtime.state import AgentState
from app.schemas.gateway import GatewayMessage
from app.tools.adapters import EscalationGate, PostCallAdapter
from app.tracing.store import TrajectoryLogger


class R1GraphState(TypedDict, total=False):
    # Internal mutable runtime objects (never exposed to AGPolicy)
    agent_state: AgentState
    constraints: ConstraintSpec
    trajectory_id: str
    started_monotonic: float
    step_index: int
    context_compacted: bool
    fallback_count: int
    fallback_applied_any: bool
    last_fallback_reason: Optional[str]
    rejected_signatures: List[str]
    last_tool_meta: Optional[Dict[str, Any]]
    attempted_tool_sigs: List[str]
    escalations_used: int
    # Current step handoff between propose -> validate -> execute
    proposal: Optional[PolicyProposal]
    validation: Optional[ValidationResult]
    effective_action: str
    effective_parameters: Dict[str, Any]
    fallback_applied: bool
    decision_id: str
    terminal_status: Optional[str]
    # Deterministic replay telemetry
    node_trace: List[str]
    decision_records: List[Dict[str, Any]]
    fallback_records: List[Dict[str, Any]]


def _estimate_tokens(messages: List[GatewayMessage]) -> int:
    total_chars = 0
    for m in messages:
        if m.content:
            total_chars += len(m.content)
        if m.tool_calls:
            for tc in m.tool_calls:
                total_chars += len(tc.function.name) + len(tc.function.arguments or "")
    return max(1, total_chars // 4)


def _build_state_view(gstate: R1GraphState, clock: Clock) -> StateView:
    st: AgentState = gstate["agent_state"]
    constraints: ConstraintSpec = gstate["constraints"]
    elapsed = max(0.0, clock.monotonic() - gstate["started_monotonic"])
    remaining = max(0.0, constraints.time_budget_seconds - elapsed)
    pending_names = tuple(tc.function.name for tc in (st.pending_tool_calls or []))
    return StateView(
        run_id=st.run_id,
        service_id=st.service_id,
        step_count=st.step_count,
        tool_call_count=st.tool_call_count,
        elapsed_seconds=elapsed,
        remaining_time_budget_seconds=remaining,
        estimated_context_tokens=_estimate_tokens(st.messages),
        has_pending_tool_calls=bool(st.pending_tool_calls),
        pending_tool_names=pending_names,
        pending_tool_count=len(pending_names),
        has_final_answer=bool(st.final_answer is not None and not st.pending_tool_calls),
        context_compacted=bool(gstate.get("context_compacted", False)),
        fallback_count=int(gstate.get("fallback_count", 0)),
        reasoning_effort=st.reasoning_effort or constraints.default_reasoning_effort,
        rejected_signatures=frozenset(gstate.get("rejected_signatures", [])),
        last_tool_quality=(gstate.get("last_tool_meta") or {}).get("quality"),
    )


class LangGraphR1Runtime:
    """Executes AgentState under strict FIX-R1-01/02/03/04 boundaries using LangGraph StateGraph."""

    def __init__(
        self,
        *,
        policy: Optional[AGPolicy] = None,
        validator: Optional[ActionValidator] = None,
        trajectory_logger: Optional[TrajectoryLogger] = None,
        clock: Clock = system_clock,
    ):
        self.policy: AGPolicy = policy or DefaultAGPolicy()
        self.validator: ActionValidator = validator or ActionValidator()
        self.trajectory_logger: Optional[TrajectoryLogger] = trajectory_logger
        self.clock: Clock = clock

    async def execute(
        self,
        state: AgentState,
        constraints: ConstraintSpec,
        *,
        emit: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        trajectory_id: Optional[str] = None,
    ) -> R1GraphState:
        tid = trajectory_id or f"traj-{state.run_id}"
        started_mono = self.clock.monotonic()

        async def _emit(event_name: str, data: Dict[str, Any]) -> None:
            if emit is not None:
                res = emit(event_name, data)
                if asyncio.iscoroutine(res):
                    await res

        async def propose_node(gstate: R1GraphState) -> Dict[str, Any]:
            trace = list(gstate.get("node_trace", []))
            trace.append("propose")
            sv = _build_state_view(gstate, self.clock)
            # AGPolicy receives ONLY immutable StateView + ConstraintSpec (REG-POL-11)
            proposal = self.policy.propose(sv, gstate["constraints"])
            return {
                "node_trace": trace,
                "proposal": proposal,
                "step_index": int(gstate.get("step_index", 0)) + 1,
            }

        async def validate_node(gstate: R1GraphState) -> Dict[str, Any]:
            trace = list(gstate.get("node_trace", []))
            trace.append("validate")
            st: AgentState = gstate["agent_state"]
            constraints_spec: ConstraintSpec = gstate["constraints"]
            proposal: PolicyProposal = gstate["proposal"]  # type: ignore[assignment]
            sv = _build_state_view(gstate, self.clock)

            vres = self.validator.validate(proposal, sv, constraints_spec)
            rejected_sigs = list(gstate.get("rejected_signatures", []))
            fallback_applied = False
            effective_action = proposal.chosen_action
            effective_params: Dict[str, Any] = dict(proposal.proposed_parameters)
            last_fb_reason = gstate.get("last_fallback_reason")

            if not vres.accepted:
                rejected_sigs.append(proposal.signature())
                fb: FallbackAction = vres.fallback_action or FallbackAction(
                    action=ActionType.TERMINATE.value,
                    reason_code=vres.reason_code or "validation_rejected",
                )
                # FIX-R1-02 / REG-POL-13: Every fallback action MUST also pass ActionValidator
                fb_vres = self.validator.validate_fallback(fb, sv, constraints_spec)
                if not fb_vres.accepted:
                    fb = fb_vres.fallback_action or FallbackAction(
                        action=ActionType.TERMINATE.value,
                        reason_code=fb_vres.reason_code or "fallback_rejected",
                    )
                fallback_applied = True
                effective_action = fb.action
                effective_params = dict(fb.parameters)
                last_fb_reason = fb.reason_code or vres.reason_code

            decision_id = f"{st.run_id}-dec-{gstate['step_index']}"
            dec_record = {
                "decision_id": decision_id,
                "step_index": gstate["step_index"],
                "chosen_action": proposal.chosen_action,
                "proposed_parameters": dict(proposal.proposed_parameters),
                "action_probability": proposal.action_probability,
                "validator_result": vres.to_dict(),
                "effective_action": effective_action,
                "fallback_applied": fallback_applied,
            }
            decision_records = list(gstate.get("decision_records", []))
            decision_records.append(dec_record)

            # FIX-R1-03: Log DecisionEvent AFTER validation and BEFORE execution
            if self.trajectory_logger is not None:
                self.trajectory_logger.log_decision(
                    trajectory_id=gstate["trajectory_id"],
                    run_id=st.run_id,
                    decision_id=decision_id,
                    step_index=gstate["step_index"],
                    decision_type=proposal.decision_type,
                    state_features=sv.to_features_dict(),
                    allowed_actions=sorted(constraints_spec.allowed_actions),
                    constraints=constraints_spec.to_summary_dict(),
                    chosen_action=proposal.chosen_action,
                    proposed_parameters=proposal.proposed_parameters,
                    behavior_policy_version=proposal.behavior_policy_version,
                    config_digest=constraints_spec.digest(),
                    action_probability=proposal.action_probability,
                    validator_result=vres.to_dict(),
                )

            return {
                "node_trace": trace,
                "validation": vres,
                "effective_action": effective_action,
                "effective_parameters": effective_params,
                "fallback_applied": fallback_applied,
                "fallback_applied_any": bool(gstate.get("fallback_applied_any", False) or fallback_applied),
                "last_fallback_reason": last_fb_reason,
                "rejected_signatures": rejected_sigs,
                "decision_id": decision_id,
                "decision_records": decision_records,
            }

        async def execute_node(gstate: R1GraphState) -> Dict[str, Any]:
            trace = list(gstate.get("node_trace", []))
            trace.append("execute")
            st: AgentState = gstate["agent_state"]
            constraints_spec: ConstraintSpec = gstate["constraints"]
            action = gstate["effective_action"]
            params = dict(gstate.get("effective_parameters", {}))
            fallback_applied = bool(gstate.get("fallback_applied", False))
            decision_id = gstate["decision_id"]
            action_id = f"{decision_id}-act"
            act_start = self.clock.monotonic()

            fallback_count = int(gstate.get("fallback_count", 0))
            if fallback_applied:
                fallback_count += 1

            # FIX-R1-03: Log ActionEvent at execution start
            if self.trajectory_logger is not None:
                self.trajectory_logger.log_action(
                    trajectory_id=gstate["trajectory_id"],
                    run_id=st.run_id,
                    decision_id=decision_id,
                    action_id=action_id,
                    executed_action=action,
                    executed_parameters=params,
                    fallback_applied=fallback_applied,
                )

            outcome = "success"
            terminal_status: Optional[str] = None
            context_compacted = bool(gstate.get("context_compacted", False))
            last_tool_meta = gstate.get("last_tool_meta")
            attempted_sigs = list(gstate.get("attempted_tool_sigs", []))
            escalations_used = int(gstate.get("escalations_used", 0))
            fallback_records = list(gstate.get("fallback_records", []))

            try:
                if action in (ActionType.CALL_MODEL.value, ActionType.DEFAULT_CONFIG.value, ActionType.SAFE_RESPOND.value):
                    if action == ActionType.DEFAULT_CONFIG.value:
                        st.reasoning_effort = params.get("reasoning_effort", constraints_spec.default_reasoning_effort)
                    if action == ActionType.SAFE_RESPOND.value:
                        # Disable further tool calls safely without exposing denied data
                        st.pending_tool_calls = []
                        st.tool_call_count = st.max_tool_calls

                    await _emit("step.started", {"step": st.step_count + 1})
                    await _emit(
                        "model.started",
                        {"step": st.step_count + 1, "reasoning_effort": st.reasoning_effort or "default"},
                    )

                    async def on_delta(chunk: str):
                        await _emit("answer.delta", {"delta": chunk, "step": st.step_count})

                    async def on_reasoning_delta(chunk: str):
                        await _emit("reasoning.delta", {"delta": chunk, "step": st.step_count})

                    res = await model_node(st, on_delta=on_delta, on_reasoning_delta=on_reasoning_delta)
                    st.messages = res["messages"]
                    st.step_count = res["step_count"]
                    st.pending_tool_calls = res.get("pending_tool_calls", [])
                    if res.get("model_label"):
                        st.model_label = res["model_label"]
                    if "final_answer" in res:
                        st.final_answer = res["final_answer"]

                    await _emit(
                        "model.completed",
                        {
                            "step": st.step_count,
                            "has_tool_calls": bool(st.pending_tool_calls),
                            "reasoning_effort": st.reasoning_effort or "default",
                        },
                    )

                    if not st.pending_tool_calls and action != ActionType.SAFE_RESPOND.value:
                        should_esc, esc_prompt = EscalationGate.evaluate(
                            last_tool_meta,
                            set(attempted_sigs),
                            set(constraints_spec.allowed_tools),
                            escalations_used,
                            budget=1,
                        )
                        if should_esc and esc_prompt and st.step_count + 1 < constraints_spec.max_steps:
                            escalations_used += 1
                            last_tool_meta = None
                            st.final_answer = None
                            st.messages.append(GatewayMessage(role="user", content=esc_prompt))
                            await _emit("step.completed", {"step": st.step_count})
                        else:
                            await _emit("step.completed", {"step": st.step_count})
                    else:
                        await _emit("step.completed", {"step": st.step_count})

                    if action == ActionType.SAFE_RESPOND.value:
                        st.status = "completed"
                        terminal_status = "completed"

                elif action == ActionType.CALL_TOOLS.value:
                    for tc in st.pending_tool_calls:
                        await _emit(
                            "tool.started",
                            {
                                "step": st.step_count,
                                "tool_name": tc.function.name,
                                "tool_call_id": tc.id,
                            },
                        )
                    t_res = await tool_node(st)
                    st.messages = t_res["messages"]
                    st.tool_call_count = t_res["tool_call_count"]
                    st.traces = t_res.get("traces", st.traces)
                    st.pending_tool_calls = []
                    recent_count = len(params.get("tool_names", [])) or len(st.traces)
                    for tr in st.traces[-recent_count:]:
                        await _emit(
                            "tool.completed",
                            {
                                "step": st.step_count,
                                "tool_name": tr.tool_name,
                                "tool_call_id": tr.tool_call_id,
                                "is_error": tr.status != "success",
                                "duration_ms": tr.duration_ms,
                            },
                        )
                    # Track tool quality metadata for EscalationGate
                    for msg in reversed(st.messages):
                        if msg.role == "tool":
                            last_tool_meta = PostCallAdapter.inspect(
                                msg.name or "", msg.content or "", is_error=False
                            )
                            break

                elif action == ActionType.COMPACT_CONTEXT.value:
                    target_budget = int(params.get("target_budget_tokens", constraints_spec.context_budget_tokens))
                    st.messages, _ = context_manager.compact_with_report(
                        st.messages, budget_tokens=target_budget, route=st.service_id
                    )
                    context_compacted = True

                elif action == ActionType.DENY.value:
                    outcome = "denied"
                    terminal_status = "denied"
                    st.status = "failed"
                    st.error = {
                        "code": gstate.get("last_fallback_reason") or "permission_denied",
                        "message": "요청이 보안 및 권한 정책에 의해 차단되었습니다.",
                    }
                    st.final_answer = None
                    st.pending_tool_calls = []

                elif action == ActionType.TERMINATE.value:
                    tstat = params.get("terminal_status") or ("completed" if st.final_answer is not None else "failed")
                    terminal_status = tstat
                    if tstat == "completed":
                        st.status = "completed"
                        outcome = "success"
                    elif tstat == "timeout":
                        st.status = "failed"
                        st.error = {"code": "run_timeout", "message": "실행 제한 시간을 초과했습니다."}
                        outcome = "timeout"
                    else:
                        st.status = "failed"
                        st.error = {
                            "code": gstate.get("last_fallback_reason") or "terminated",
                            "message": "실행 제약 조건에 의해 중단되었습니다.",
                        }
                        outcome = "failed"
                    st.pending_tool_calls = []

            except GatewayStreamError as gse:
                outcome = "failed"
                terminal_status = "failed"
                st.status = "failed"
                st.error = {"code": f"gateway_{gse.code}", "message": gse.message}
            except Exception as exc:
                outcome = "failed"
                terminal_status = "failed"
                st.status = "failed"
                st.error = {"code": "internal_error", "message": str(exc)}

            if fallback_applied:
                fallback_records.append(
                    {
                        "decision_id": decision_id,
                        "action": action,
                        "reason_code": gstate.get("last_fallback_reason"),
                        "outcome": outcome,
                        "terminal_status": terminal_status,
                    }
                )

            latency = max(0.0, self.clock.monotonic() - act_start)
            next_sv = _build_state_view(
                {
                    **gstate,
                    "context_compacted": context_compacted,
                    "fallback_count": fallback_count,
                    "last_tool_meta": last_tool_meta,
                },
                self.clock,
            )

            # FIX-R1-03: Log OutcomeEvent on completion/failure/termination
            if self.trajectory_logger is not None:
                self.trajectory_logger.log_outcome(
                    trajectory_id=gstate["trajectory_id"],
                    run_id=st.run_id,
                    decision_id=decision_id,
                    action_id=action_id,
                    outcome=outcome,
                    next_state_features=next_sv.to_features_dict(),
                    execution_latency=latency,
                    terminal_status=terminal_status,
                )

            return {
                "node_trace": trace,
                "context_compacted": context_compacted,
                "fallback_count": fallback_count,
                "last_tool_meta": last_tool_meta,
                "attempted_tool_sigs": attempted_sigs,
                "escalations_used": escalations_used,
                "fallback_records": fallback_records,
                "terminal_status": terminal_status,
            }

        def should_continue(gstate: R1GraphState) -> Literal["propose", "end"]:
            if gstate.get("terminal_status") is not None:
                return "end"
            st: AgentState = gstate["agent_state"]
            if st.status != "running":
                return "end"
            return "propose"

        workflow = StateGraph(R1GraphState)
        workflow.add_node("propose", propose_node)
        workflow.add_node("validate", validate_node)
        workflow.add_node("execute", execute_node)
        workflow.set_entry_point("propose")
        workflow.add_edge("propose", "validate")
        workflow.add_edge("validate", "execute")
        workflow.add_conditional_edges("execute", should_continue, {"propose": "propose", "end": END})
        compiled = workflow.compile()

        init: R1GraphState = {
            "agent_state": state,
            "constraints": constraints,
            "trajectory_id": tid,
            "started_monotonic": started_mono,
            "step_index": 0,
            "context_compacted": False,
            "fallback_count": 0,
            "fallback_applied_any": False,
            "last_fallback_reason": None,
            "rejected_signatures": [],
            "last_tool_meta": None,
            "attempted_tool_sigs": [],
            "escalations_used": 0,
            "node_trace": [],
            "decision_records": [],
            "fallback_records": [],
            "terminal_status": None,
        }
        return await compiled.ainvoke(init, config={"recursion_limit": max(25, constraints.max_steps * 4 + 10)})
