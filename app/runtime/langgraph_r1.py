"""Phase R1.1: LangGraph StateGraph Runtime with ValidatedAction, HardDeadline & TerminalStateFence."""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Literal, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app.config import settings
from app.core.clock import Clock, system_clock
from app.gateway.client import GatewayStreamError
from app.runtime.context import context_manager
from app.runtime.fence import HardDeadline, TerminalStateFence
from app.runtime.graph import model_node, tool_node
from app.runtime.policy.ag_policy import AGPolicy, DefaultAGPolicy, PolicyProposal, StateView
from app.runtime.policy.constraints import ActionType, ConstraintSpec
from app.runtime.policy.validator import ActionValidator, FallbackAction, ValidatedAction, ValidationResult
from app.runtime.state import AgentState
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tools.adapters import EscalationGate, PostCallAdapter
from app.tracing.store import TrajectoryLogger


class R1GraphState(TypedDict, total=False):
    agent_state: AgentState
    constraints: ConstraintSpec
    trajectory_id: str
    deadline: HardDeadline
    fence: TerminalStateFence
    step_index: int
    context_compacted: bool
    fallback_count: int
    fallback_applied_any: bool
    last_fallback_reason: Optional[str]
    rejected_signatures: List[str]
    last_tool_meta: Optional[Dict[str, Any]]
    attempted_tool_sigs: List[str]
    escalations_used: int
    proposal: Optional[PolicyProposal]
    validation: Optional[ValidationResult]
    validated_action: Optional[ValidatedAction]
    decision_id: str
    terminal_status: Optional[str]
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


def _build_state_view(gstate: R1GraphState) -> StateView:
    st: AgentState = gstate["agent_state"]
    constraints: ConstraintSpec = gstate["constraints"]
    deadline: HardDeadline = gstate["deadline"]
    pending_names = tuple(tc.function.name for tc in (st.pending_tool_calls or []))
    pending_ids = tuple(tc.id for tc in (st.pending_tool_calls or []))
    return StateView(
        run_id=st.run_id,
        service_id=st.service_id or constraints.service_id,
        step_count=st.step_count,
        tool_call_count=st.tool_call_count,
        elapsed_seconds=deadline.elapsed_seconds(),
        remaining_time_budget_seconds=deadline.remaining_seconds(),
        estimated_context_tokens=_estimate_tokens(st.messages),
        has_pending_tool_calls=bool(st.pending_tool_calls),
        pending_tool_names=pending_names,
        pending_tool_call_ids=pending_ids,
        pending_tool_count=len(pending_names),
        has_final_answer=bool(st.final_answer is not None and not st.pending_tool_calls),
        context_compacted=bool(gstate.get("context_compacted", False)),
        fallback_count=int(gstate.get("fallback_count", 0)),
        reasoning_effort=st.reasoning_effort or constraints.default_reasoning_effort,
        rejected_signatures=frozenset(gstate.get("rejected_signatures", [])),
        last_tool_quality=(gstate.get("last_tool_meta") or {}).get("quality"),
    )


class LangGraphR1Runtime:
    """Executes AgentState under strict R1.1 ValidatedAction, HardDeadline, and TerminalStateFence."""

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
        deadline: Optional[HardDeadline] = None,
        fence: Optional[TerminalStateFence] = None,
    ) -> R1GraphState:
        tid = trajectory_id or f"traj-{state.run_id}"
        run_deadline = deadline or HardDeadline(constraints.time_budget_seconds, clock=self.clock)
        run_fence = fence or TerminalStateFence()

        async def _emit(event_name: str, data: Dict[str, Any]) -> None:
            # R1-FIX-04 B.5: Block non-terminal user events once TerminalStateFence is closed
            if run_fence.is_terminal:
                run_fence.record_late_result()
                return
            if emit is not None:
                res = emit(event_name, data)
                if asyncio.iscoroutine(res):
                    await res

        async def propose_node(gstate: R1GraphState) -> Dict[str, Any]:
            trace = list(gstate.get("node_trace", []))
            trace.append("propose")
            sv = _build_state_view(gstate)
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
            sv = _build_state_view(gstate)

            vres = self.validator.validate(
                proposal,
                sv,
                constraints_spec,
                pending_tool_calls=st.pending_tool_calls if proposal.chosen_action == ActionType.CALL_TOOLS.value else None,
                deadline_monotonic=run_deadline.deadline_monotonic,
            )
            rejected_sigs = list(gstate.get("rejected_signatures", []))
            last_fb_reason = gstate.get("last_fallback_reason")
            va: Optional[ValidatedAction] = vres.validated_action

            if not vres.accepted or va is None:
                rejected_sigs.append(proposal.signature())
                fb: FallbackAction = vres.fallback_action or FallbackAction(
                    action=ActionType.TERMINATE.value,
                    reason_code=vres.reason_code or "validation_rejected",
                )
                fb_vres = self.validator.validate_fallback(
                    fb,
                    sv,
                    constraints_spec,
                    deadline_monotonic=run_deadline.deadline_monotonic,
                )
                if not fb_vres.accepted or fb_vres.validated_action is None:
                    term_fb = fb_vres.fallback_action or FallbackAction(
                        action=ActionType.TERMINATE.value,
                        reason_code=fb_vres.reason_code or "fallback_rejected",
                    )
                    term_res = self.validator.validate_fallback(
                        term_fb, sv, constraints_spec, deadline_monotonic=run_deadline.deadline_monotonic
                    )
                    va = term_res.validated_action
                else:
                    va = fb_vres.validated_action
                last_fb_reason = (va.reason_code if va else None) or fb.reason_code or vres.reason_code

            assert va is not None, "ActionValidator must always produce a ValidatedAction"

            decision_id = f"{st.run_id}-dec-{gstate['step_index']}"
            dec_record = {
                "decision_id": decision_id,
                "step_index": gstate["step_index"],
                "chosen_action": proposal.chosen_action,
                "proposed_parameters": dict(proposal.proposed_parameters),
                "action_probability": proposal.action_probability,
                "validator_result": vres.to_dict(),
                "effective_action": va.action,
                "fallback_applied": va.fallback_applied,
            }
            decision_records = list(gstate.get("decision_records", []))
            decision_records.append(dec_record)

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
                    owner_id=constraints_spec.owner_id,
                    service_id=constraints_spec.service_id,
                )

            return {
                "node_trace": trace,
                "validation": vres,
                "validated_action": va,
                "fallback_applied_any": bool(gstate.get("fallback_applied_any", False) or va.fallback_applied),
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
            va: ValidatedAction = gstate["validated_action"]  # type: ignore[assignment]
            action = va.action
            params = dict(va.parameters)
            fallback_applied = va.fallback_applied
            decision_id = gstate["decision_id"]
            action_id = f"{decision_id}-act"
            act_start = self.clock.monotonic()

            fallback_count = int(gstate.get("fallback_count", 0))
            if fallback_applied:
                fallback_count += 1

            # Block any new execution if TerminalStateFence is already closed (R1-FIX-04 B.3)
            if run_fence.is_terminal:
                run_fence.record_late_result()
                return {"node_trace": trace, "terminal_status": run_fence.terminal_state}

            if self.trajectory_logger is not None:
                self.trajectory_logger.log_action(
                    trajectory_id=gstate["trajectory_id"],
                    run_id=st.run_id,
                    decision_id=decision_id,
                    action_id=action_id,
                    executed_action=action,
                    executed_parameters=params,
                    fallback_applied=fallback_applied,
                    owner_id=constraints_spec.owner_id,
                    service_id=constraints_spec.service_id,
                )

            outcome = "success"
            terminal_status: Optional[str] = None
            collected_token_usage: Dict[str, int] = {}
            context_compacted = bool(gstate.get("context_compacted", False))
            last_tool_meta = gstate.get("last_tool_meta")
            attempted_sigs = list(gstate.get("attempted_tool_sigs", []))
            escalations_used = int(gstate.get("escalations_used", 0))
            fallback_records = list(gstate.get("fallback_records", []))

            try:
                if action in (ActionType.CALL_MODEL.value, ActionType.DEFAULT_CONFIG.value, ActionType.SAFE_RESPOND.value):
                    if run_deadline.is_expired():
                        raise TimeoutError("run_deadline_exceeded")

                    effective_effort = (
                        params.get("reasoning_effort", constraints_spec.default_reasoning_effort)
                        if action in (ActionType.DEFAULT_CONFIG.value, ActionType.CALL_MODEL.value)
                        else st.reasoning_effort
                    )
                    effective_ctx_budget = params.get("context_budget_tokens")
                    is_safe_respond = action == ActionType.SAFE_RESPOND.value
                    if is_safe_respond:
                        st.pending_tool_calls = []

                    await _emit("step.started", {"step": st.step_count + 1})
                    await _emit(
                        "model.started",
                        {"step": st.step_count + 1, "reasoning_effort": effective_effort or "default"},
                    )

                    is_pseudo_tool = False
                    async def on_delta(chunk: str):
                        nonlocal is_pseudo_tool
                        if "<tool_call" in chunk or "<function" in chunk:
                            is_pseudo_tool = True
                        if not is_pseudo_tool:
                            await _emit("answer.delta", {"delta": chunk, "step": st.step_count})

                    async def on_reasoning_delta(chunk: str):
                        await _emit("reasoning.delta", {"delta": chunk, "step": st.step_count})

                    async with asyncio.timeout(run_deadline.remaining_seconds()):
                        res = await model_node(
                            st,
                            on_delta=on_delta,
                            on_reasoning_delta=on_reasoning_delta,
                            context_budget_override=effective_ctx_budget,
                            reasoning_effort_override=effective_effort,
                            disable_tools=is_safe_respond,
                        )

                    # FIX-207: Prefer per-invocation ModelReply usage from model_node; fall back to custom_client.last_usage only if unreported
                    raw_usage = res.get("usage") or (
                        getattr(st.custom_client, "last_usage", None) if st.custom_client is not None else None
                    )
                    if isinstance(raw_usage, dict):
                        for k in ("input_tokens", "output_tokens", "total_tokens"):
                            v = raw_usage.get(k)
                            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                                collected_token_usage[k] = v

                    if run_fence.is_terminal:
                        run_fence.record_late_result()
                        return {"node_trace": trace, "terminal_status": run_fence.terminal_state}

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

                    if action == ActionType.SAFE_RESPOND.value:
                        if (st.final_answer or "").strip():
                            if run_fence.try_finalize("completed"):
                                st.status = "completed"
                                terminal_status = "completed"
                        else:
                            if run_fence.try_finalize("failed"):
                                st.status = "failed"
                                st.error = {"code": "empty_safe_response", "message": "안전 응답 생성 결과가 비어 있습니다."}
                                terminal_status = "failed"
                                outcome = "failed"

                elif action == ActionType.CALL_TOOLS.value:
                    if run_deadline.is_expired():
                        raise TimeoutError("run_deadline_exceeded")

                    # R1-FIX-01: Execute ONLY the exact validated ToolCall list from ValidatedAction!
                    validated_tc_objs = [
                        ToolCall(
                            id=vtc.tool_call_id,
                            function=FunctionCall(name=vtc.tool_name, arguments=vtc.arguments_json),
                        )
                        for vtc in va.validated_tool_calls
                    ]
                    if not validated_tc_objs:
                        raise RuntimeError("validated_action_missing_tool_calls")

                    for tc in validated_tc_objs:
                        await _emit(
                            "tool.started",
                            {
                                "step": st.step_count,
                                "tool_name": tc.function.name,
                                "tool_call_id": tc.id,
                            },
                        )

                    tool_timeout = run_deadline.cap_timeout(settings.TOOL_TIMEOUT_SECONDS)
                    async with asyncio.timeout(run_deadline.remaining_seconds()):
                        t_res = await tool_node(
                            st,
                            validated_calls=validated_tc_objs,
                            constraints=constraints_spec,
                            timeout_override=tool_timeout,
                        )

                    if run_fence.is_terminal:
                        run_fence.record_late_result()
                        return {"node_trace": trace, "terminal_status": run_fence.terminal_state}

                    st.messages = t_res["messages"]
                    st.tool_call_count = t_res["tool_call_count"]
                    st.traces = t_res.get("traces", st.traces)
                    st.citations = t_res.get("citations", st.citations)
                    st.pending_tool_calls = []

                    recent_traces = st.traces[-len(validated_tc_objs):]
                    any_tool_err = False
                    any_tool_unknown = False
                    for tr in recent_traces:
                        # FIX-205: ToolExecutionTrace.status SSoT is "completed" | "failed" | "running"
                        is_err = tr.status == "failed"
                        if is_err:
                            any_tool_err = True
                        elif tr.status != "completed":
                            any_tool_unknown = True
                        await _emit(
                            "tool.completed",
                            {
                                "step": st.step_count,
                                "tool_name": tr.tool_name,
                                "tool_call_id": tr.tool_call_id,
                                "is_error": is_err,
                                "status": tr.status,
                                "duration_ms": tr.duration_ms,
                            },
                        )
                    if any_tool_err:
                        outcome = "failed"
                    elif any_tool_unknown:
                        outcome = "UNKNOWN"
                    else:
                        outcome = "success"
                    for msg in reversed(st.messages):
                        if msg.role == "tool":
                            last_tool_meta = PostCallAdapter.inspect(
                                msg.name or "", msg.content or "", is_error=any_tool_err
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
                    if run_fence.try_finalize("failed"):
                        st.status = "failed"
                        st.error = {
                            "code": gstate.get("last_fallback_reason") or "permission_denied",
                            "message": "요청이 보안 및 권한 정책에 의해 차단되었습니다.",
                        }
                        st.final_answer = None
                        st.pending_tool_calls = []

                elif action == ActionType.TERMINATE.value:
                    tstat = params.get("terminal_status") or ("completed" if (st.final_answer or "").strip() else "failed")
                    if tstat == "completed" and (st.pending_tool_calls or not (st.final_answer or "").strip()):
                        tstat = "failed"
                    terminal_status = tstat
                    if tstat == "completed":
                        if run_fence.try_finalize("completed"):
                            st.status = "completed"
                            outcome = "success"
                    elif tstat == "timeout":
                        if run_fence.try_finalize("failed"):
                            st.status = "failed"
                            st.error = {"code": "run_timeout", "message": "실행 제한 시간을 초과했습니다."}
                            outcome = "timeout"
                    else:
                        if run_fence.try_finalize("failed"):
                            st.status = "failed"
                            st.error = {
                                "code": gstate.get("last_fallback_reason") or "terminated",
                                "message": "실행 제약 조건에 의해 중단되었습니다.",
                            }
                            outcome = "failed"
                    st.pending_tool_calls = []

            except TimeoutError:
                outcome = "timeout"
                terminal_status = "timeout"
                if run_fence.try_finalize("failed"):
                    st.status = "failed"
                    st.error = {"code": "run_timeout", "message": "실행 제한 시간을 초과했습니다."}
                    st.pending_tool_calls = []
            except GatewayStreamError as gse:
                outcome = "failed"
                terminal_status = "failed"
                if run_fence.try_finalize("failed"):
                    st.status = "failed"
                    st.error = {"code": f"gateway_{gse.code}", "message": gse.message}
            except Exception as exc:
                outcome = "failed"
                terminal_status = "failed"
                if run_fence.try_finalize("failed"):
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
                }
            )

            if self.trajectory_logger is not None:
                self.trajectory_logger.log_outcome(
                    trajectory_id=gstate["trajectory_id"],
                    run_id=st.run_id,
                    decision_id=decision_id,
                    action_id=action_id,
                    outcome=outcome,
                    next_state_features=next_sv.to_features_dict(),
                    execution_latency=latency,
                    token_usage=collected_token_usage,
                    terminal_status=terminal_status,
                    owner_id=constraints_spec.owner_id,
                    service_id=constraints_spec.service_id,
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
            if gstate.get("terminal_status") is not None or run_fence.is_terminal:
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
            "deadline": run_deadline,
            "fence": run_fence,
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
