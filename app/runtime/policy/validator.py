"""FIX-R1-02 & R1-FIX-01: ActionValidator, ValidatedAction Contract & Deterministic Safe Fallback."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.runtime.policy.ag_policy import PolicyProposal, StateView
from app.runtime.policy.constraints import (
    ActionType,
    ConstraintSpec,
    VALID_REASONING_EFFORTS,
)

# Private module-scoped minting token: only ActionValidator can construct ValidatedAction (R1-FIX-01.13).
_VALIDATED_ACTION_MINT_TOKEN = object()


class RejectionCategory(str, Enum):
    AUTH_FAILURE = "auth_failure"
    PERMISSION_VIOLATION = "permission_violation"
    SAFETY_POLICY_VIOLATION = "safety_policy_violation"
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    TOOL_CALL_BUDGET_EXCEEDED = "tool_call_budget_exceeded"
    STEP_BUDGET_EXCEEDED = "step_budget_exceeded"
    CONTEXT_BUDGET_INSUFFICIENT = "context_budget_insufficient"
    INVALID_POLICY_PARAMETERS = "invalid_policy_parameters"
    UNSUPPORTED_ACTION = "unsupported_action"
    RETRY_LOOP_PREVENTED = "retry_loop_prevented"


@dataclass(frozen=True)
class ValidatedToolCall:
    """Immutable snapshot of a single validated ToolCall (R1-FIX-01.3 / 01.8)."""

    tool_call_id: str
    tool_name: str
    arguments_json: str
    parsed_arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ValidatedAction:
    """Immutable execution contract minted exclusively by ActionValidator (R1-FIX-01)."""

    action: str
    parameters: Mapping[str, Any]
    validated_tool_calls: Tuple[ValidatedToolCall, ...]
    actual_call_count: int
    scope_owner_id: str
    scope_service_id: str
    budget_reservation: int
    deadline_monotonic: float
    fallback_applied: bool = False
    reason_code: Optional[str] = None
    _mint_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint_token is not _VALIDATED_ACTION_MINT_TOKEN:
            raise PermissionError("ValidatedAction can only be created by ActionValidator")


@dataclass(frozen=True)
class FallbackAction:
    action: str
    parameters: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    reason_code: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "parameters": dict(self.parameters),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Structured validation outcome mandated by FIX-R1-02 & R1-FIX-01."""

    accepted: bool
    reason_code: Optional[str] = None
    violated_constraints: Tuple[str, ...] = ()
    fallback_action: Optional[FallbackAction] = None
    validated_action: Optional[ValidatedAction] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "violated_constraints": list(self.violated_constraints),
            "fallback_action": self.fallback_action.to_dict() if self.fallback_action else None,
        }


class ActionValidator:
    """Validates AGPolicy proposals and Fallback actions against ConstraintSpec (SSOT).

    - Never duplicates constraint rules: delegates permissions/budgets to ConstraintSpec.
    - Binds exact ToolCall IDs/names/arguments/count into an immutable ValidatedAction (R1-FIX-01).
    - Deterministically maps each rejection category to a safe fallback.
    - Re-validates every fallback action (REG-POL-13) and prevents infinite retries (REG-POL-14).
    """

    @staticmethod
    def _mint(
        *,
        action: str,
        parameters: Mapping[str, Any],
        constraints: ConstraintSpec,
        validated_tool_calls: Tuple[ValidatedToolCall, ...] = (),
        budget_reservation: int = 0,
        deadline_monotonic: float = 0.0,
        fallback_applied: bool = False,
        reason_code: Optional[str] = None,
    ) -> ValidatedAction:
        return ValidatedAction(
            action=action,
            parameters=MappingProxyType(dict(parameters)),
            validated_tool_calls=validated_tool_calls,
            actual_call_count=len(validated_tool_calls),
            scope_owner_id=constraints.owner_id,
            scope_service_id=constraints.service_id,
            budget_reservation=budget_reservation,
            deadline_monotonic=deadline_monotonic,
            fallback_applied=fallback_applied,
            reason_code=reason_code,
            _mint_token=_VALIDATED_ACTION_MINT_TOKEN,
        )

    def validate(
        self,
        proposal: PolicyProposal,
        state_view: StateView,
        constraints: ConstraintSpec,
        *,
        pending_tool_calls: Optional[Sequence[Any]] = None,
        deadline_monotonic: float = 0.0,
    ) -> ValidationResult:
        # 1. Authentication check -> DENY / TERMINATE
        if not constraints.authenticated:
            return self._reject(
                RejectionCategory.AUTH_FAILURE,
                ["authenticated"],
                FallbackAction(
                    action=ActionType.DENY.value,
                    parameters=MappingProxyType({"terminal_status": "denied", "expose_data": False}),
                    reason_code=RejectionCategory.AUTH_FAILURE.value,
                ),
            )

        # 2. Safety policy check -> DENY / TERMINATE
        if constraints.safety_blocked:
            return self._reject(
                RejectionCategory.SAFETY_POLICY_VIOLATION,
                [constraints.safety_reason or "safety_policy"],
                FallbackAction(
                    action=ActionType.DENY.value,
                    parameters=MappingProxyType({"terminal_status": "denied", "expose_data": False}),
                    reason_code=RejectionCategory.SAFETY_POLICY_VIOLATION.value,
                ),
            )

        # 3. Unsupported action -> TERMINATE
        if proposal.chosen_action not in constraints.allowed_actions:
            return self._reject(
                RejectionCategory.UNSUPPORTED_ACTION,
                [f"allowed_actions:{proposal.chosen_action}"],
                FallbackAction(
                    action=ActionType.TERMINATE.value,
                    parameters=MappingProxyType({"terminal_status": "failed"}),
                    reason_code=RejectionCategory.UNSUPPORTED_ACTION.value,
                ),
            )

        # 4. Prevent repeated identical rejected proposals (REG-POL-14)
        if proposal.signature() in state_view.rejected_signatures:
            return self._reject(
                RejectionCategory.RETRY_LOOP_PREVENTED,
                ["no_infinite_retry"],
                FallbackAction(
                    action=ActionType.TERMINATE.value,
                    parameters=MappingProxyType({"terminal_status": "failed"}),
                    reason_code=RejectionCategory.RETRY_LOOP_PREVENTED.value,
                ),
            )

        # 5. Execution time budget -> TERMINATE
        if state_view.remaining_time_budget_seconds <= 0.0 and proposal.chosen_action not in (
            ActionType.TERMINATE.value,
            ActionType.DENY.value,
        ):
            return self._reject(
                RejectionCategory.TIMEOUT_EXCEEDED,
                ["time_budget_seconds"],
                FallbackAction(
                    action=ActionType.TERMINATE.value,
                    parameters=MappingProxyType({"terminal_status": "timeout"}),
                    reason_code=RejectionCategory.TIMEOUT_EXCEEDED.value,
                ),
            )

        # 6. Action-specific checks + ValidatedAction binding
        validated_calls: Tuple[ValidatedToolCall, ...] = ()
        budget_reservation = 0

        if proposal.chosen_action == ActionType.CALL_TOOLS.value:
            if pending_tool_calls is not None:
                # R1-FIX-01: Bind exact ToolCall objects by ID, never trusting AGPolicy self-reported count
                pending_by_id: Dict[str, Any] = {}
                for tc in pending_tool_calls:
                    if tc.id in pending_by_id:
                        return self._reject(
                            RejectionCategory.INVALID_POLICY_PARAMETERS,
                            [f"duplicate_pending_tool_call_id:{tc.id}"],
                            FallbackAction(
                                action=ActionType.TERMINATE.value,
                                parameters=MappingProxyType({"terminal_status": "failed"}),
                                reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                            ),
                        )
                    pending_by_id[tc.id] = tc

                requested_ids_raw = proposal.proposed_parameters.get("tool_call_ids")
                if requested_ids_raw is not None:
                    requested_ids = tuple(requested_ids_raw)
                else:
                    requested_ids = tuple(tc.id for tc in pending_tool_calls)

                if not requested_ids:
                    return self._reject(
                        RejectionCategory.INVALID_POLICY_PARAMETERS,
                        ["tool_call_ids_required"],
                        FallbackAction(
                            action=ActionType.TERMINATE.value,
                            parameters=MappingProxyType({"terminal_status": "failed"}),
                            reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                        ),
                    )

                # Reject duplicate IDs or non-existent IDs (R1-FIX-01.7)
                if len(set(requested_ids)) != len(requested_ids):
                    return self._reject(
                        RejectionCategory.INVALID_POLICY_PARAMETERS,
                        ["duplicate_tool_call_id"],
                        FallbackAction(
                            action=ActionType.TERMINATE.value,
                            parameters=MappingProxyType({"terminal_status": "failed"}),
                            reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                        ),
                    )

                selected_tcs = []
                for cid in requested_ids:
                    if cid not in pending_by_id:
                        return self._reject(
                            RejectionCategory.INVALID_POLICY_PARAMETERS,
                            [f"unknown_tool_call_id:{cid}"],
                            FallbackAction(
                                action=ActionType.TERMINATE.value,
                                parameters=MappingProxyType({"terminal_status": "failed"}),
                                reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                            ),
                        )
                    selected_tcs.append(pending_by_id[cid])

                # Check optional proposed tool_names match actual pending calls (R1-FIX-01.11)
                proposed_names = proposal.proposed_parameters.get("tool_names")
                actual_names = tuple(tc.function.name for tc in selected_tcs)
                if proposed_names is not None and tuple(proposed_names) != actual_names:
                    return self._reject(
                        RejectionCategory.INVALID_POLICY_PARAMETERS,
                        ["tool_name_mismatch"],
                        FallbackAction(
                            action=ActionType.TERMINATE.value,
                            parameters=MappingProxyType({"terminal_status": "failed"}),
                            reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                        ),
                    )

                # Permission check against ConstraintSpec (SSOT)
                denied: List[str] = []
                for tname in actual_names:
                    allowed, deny_reason = constraints.check_tool_permission(tname)
                    if not allowed:
                        denied.append(deny_reason or f"permission_denied:{tname}")
                if denied:
                    return self._reject(
                        RejectionCategory.PERMISSION_VIOLATION,
                        denied,
                        FallbackAction(
                            action=ActionType.DENY.value,
                            parameters=MappingProxyType({"terminal_status": "denied", "expose_data": False}),
                            reason_code=RejectionCategory.PERMISSION_VIOLATION.value,
                        ),
                    )

                # Actual call count computed strictly from validated targets (R1-FIX-01.6)
                actual_count = len(selected_tcs)
                if state_view.tool_call_count + actual_count > constraints.max_tool_calls:
                    fb_action = (
                        ActionType.SAFE_RESPOND.value
                        if state_view.fallback_count < constraints.max_fallback_depth
                        else ActionType.TERMINATE.value
                    )
                    return self._reject(
                        RejectionCategory.TOOL_CALL_BUDGET_EXCEEDED,
                        ["max_tool_calls"],
                        FallbackAction(
                            action=fb_action,
                            parameters=MappingProxyType(
                                {
                                    "disable_tools": True,
                                    "terminal_status": "completed" if fb_action == ActionType.SAFE_RESPOND.value else "failed",
                                }
                            ),
                            reason_code=RejectionCategory.TOOL_CALL_BUDGET_EXCEEDED.value,
                        ),
                    )

                # Freeze validated tool calls & arguments (R1-FIX-01.8)
                frozen_list: List[ValidatedToolCall] = []
                for tc in selected_tcs:
                    raw_args = tc.function.arguments
                    if isinstance(raw_args, str):
                        try:
                            parsed = json.loads(raw_args) if raw_args.strip() else {}
                        except Exception:
                            return self._reject(
                                RejectionCategory.INVALID_POLICY_PARAMETERS,
                                [f"invalid_tool_arguments_json:{tc.id}"],
                                FallbackAction(
                                    action=ActionType.TERMINATE.value,
                                    parameters=MappingProxyType({"terminal_status": "failed"}),
                                    reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                                ),
                            )
                        args_json = raw_args
                    elif isinstance(raw_args, dict):
                        parsed = dict(raw_args)
                        args_json = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
                    else:
                        return self._reject(
                            RejectionCategory.INVALID_POLICY_PARAMETERS,
                            [f"invalid_tool_arguments_type:{tc.id}"],
                            FallbackAction(
                                action=ActionType.TERMINATE.value,
                                parameters=MappingProxyType({"terminal_status": "failed"}),
                                reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                            ),
                        )
                    if not isinstance(parsed, dict):
                        return self._reject(
                            RejectionCategory.INVALID_POLICY_PARAMETERS,
                            [f"tool_arguments_must_be_object:{tc.id}"],
                            FallbackAction(
                                action=ActionType.TERMINATE.value,
                                parameters=MappingProxyType({"terminal_status": "failed"}),
                                reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                            ),
                        )
                    frozen_list.append(
                        ValidatedToolCall(
                            tool_call_id=tc.id,
                            tool_name=tc.function.name,
                            arguments_json=args_json,
                            parsed_arguments=MappingProxyType(parsed),
                        )
                    )
                validated_calls = tuple(frozen_list)
                budget_reservation = actual_count
            else:
                # Unit-test path without pending_tool_calls objects
                tool_names = tuple(proposal.proposed_parameters.get("tool_names", ()))
                if not tool_names:
                    return self._reject(
                        RejectionCategory.INVALID_POLICY_PARAMETERS,
                        ["tool_names_required"],
                        FallbackAction(
                            action=ActionType.TERMINATE.value,
                            parameters=MappingProxyType({"terminal_status": "failed"}),
                            reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                        ),
                    )
                denied = []
                for tname in tool_names:
                    allowed, deny_reason = constraints.check_tool_permission(tname)
                    if not allowed:
                        denied.append(deny_reason or f"permission_denied:{tname}")
                if denied:
                    return self._reject(
                        RejectionCategory.PERMISSION_VIOLATION,
                        denied,
                        FallbackAction(
                            action=ActionType.DENY.value,
                            parameters=MappingProxyType({"terminal_status": "denied", "expose_data": False}),
                            reason_code=RejectionCategory.PERMISSION_VIOLATION.value,
                        ),
                    )
                actual_count = max(len(tool_names), int(proposal.proposed_parameters.get("tool_count", len(tool_names))))
                if state_view.tool_call_count + actual_count > constraints.max_tool_calls:
                    fb_action = (
                        ActionType.SAFE_RESPOND.value
                        if state_view.fallback_count < constraints.max_fallback_depth
                        else ActionType.TERMINATE.value
                    )
                    return self._reject(
                        RejectionCategory.TOOL_CALL_BUDGET_EXCEEDED,
                        ["max_tool_calls"],
                        FallbackAction(
                            action=fb_action,
                            parameters=MappingProxyType(
                                {
                                    "disable_tools": True,
                                    "terminal_status": "completed" if fb_action == ActionType.SAFE_RESPOND.value else "failed",
                                }
                            ),
                            reason_code=RejectionCategory.TOOL_CALL_BUDGET_EXCEEDED.value,
                        ),
                    )
                budget_reservation = actual_count

        elif proposal.chosen_action == ActionType.CALL_MODEL.value:
            if state_view.step_count >= constraints.max_steps:
                return self._reject(
                    RejectionCategory.STEP_BUDGET_EXCEEDED,
                    ["max_steps"],
                    FallbackAction(
                        action=ActionType.TERMINATE.value,
                        parameters=MappingProxyType({"terminal_status": "failed"}),
                        reason_code=RejectionCategory.STEP_BUDGET_EXCEEDED.value,
                    ),
                )

            effort = proposal.proposed_parameters.get("reasoning_effort", constraints.default_reasoning_effort)
            budget = proposal.proposed_parameters.get("context_budget_tokens", constraints.context_budget_tokens)
            invalid_params: List[str] = []
            if effort not in VALID_REASONING_EFFORTS:
                invalid_params.append(f"invalid_reasoning_effort:{effort}")
            if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0 or budget > constraints.context_budget_tokens:
                invalid_params.append("invalid_context_budget_tokens")
            if invalid_params:
                fb_action = (
                    ActionType.DEFAULT_CONFIG.value
                    if state_view.fallback_count < constraints.max_fallback_depth
                    else ActionType.TERMINATE.value
                )
                return self._reject(
                    RejectionCategory.INVALID_POLICY_PARAMETERS,
                    invalid_params,
                    FallbackAction(
                        action=fb_action,
                        parameters=MappingProxyType(
                            {
                                "reasoning_effort": constraints.default_reasoning_effort,
                                "context_budget_tokens": constraints.context_budget_tokens,
                            }
                        ),
                        reason_code=RejectionCategory.INVALID_POLICY_PARAMETERS.value,
                    ),
                )

            if state_view.estimated_context_tokens > constraints.context_budget_tokens:
                if not state_view.context_compacted and state_view.fallback_count < constraints.max_fallback_depth:
                    return self._reject(
                        RejectionCategory.CONTEXT_BUDGET_INSUFFICIENT,
                        ["context_budget_tokens"],
                        FallbackAction(
                            action=ActionType.COMPACT_CONTEXT.value,
                            parameters=MappingProxyType({"target_budget_tokens": constraints.context_budget_tokens}),
                            reason_code=RejectionCategory.CONTEXT_BUDGET_INSUFFICIENT.value,
                        ),
                    )
                return self._reject(
                    RejectionCategory.CONTEXT_BUDGET_INSUFFICIENT,
                    ["context_budget_tokens"],
                    FallbackAction(
                        action=ActionType.TERMINATE.value,
                        parameters=MappingProxyType({"terminal_status": "failed"}),
                        reason_code=RejectionCategory.CONTEXT_BUDGET_INSUFFICIENT.value,
                    ),
                )

        va = self._mint(
            action=proposal.chosen_action,
            parameters=proposal.proposed_parameters,
            constraints=constraints,
            validated_tool_calls=validated_calls,
            budget_reservation=budget_reservation,
            deadline_monotonic=deadline_monotonic,
            fallback_applied=False,
        )
        return ValidationResult(accepted=True, validated_action=va)

    def validate_fallback(
        self,
        fallback: FallbackAction,
        state_view: StateView,
        constraints: ConstraintSpec,
        *,
        deadline_monotonic: float = 0.0,
    ) -> ValidationResult:
        """Re-validates a FallbackAction before execution and mints a ValidatedAction (REG-POL-13 / R1-FIX-07)."""
        if fallback.action in (ActionType.DENY.value, ActionType.TERMINATE.value):
            va = self._mint(
                action=fallback.action,
                parameters=fallback.parameters,
                constraints=constraints,
                deadline_monotonic=deadline_monotonic,
                fallback_applied=True,
                reason_code=fallback.reason_code,
            )
            return ValidationResult(accepted=True, validated_action=va)

        if not constraints.authenticated:
            return self._reject(
                RejectionCategory.AUTH_FAILURE,
                ["authenticated"],
                FallbackAction(
                    action=ActionType.DENY.value,
                    parameters=MappingProxyType({"terminal_status": "denied", "expose_data": False}),
                    reason_code=RejectionCategory.AUTH_FAILURE.value,
                ),
            )
        if constraints.safety_blocked:
            return self._reject(
                RejectionCategory.SAFETY_POLICY_VIOLATION,
                [constraints.safety_reason or "safety_policy"],
                FallbackAction(
                    action=ActionType.DENY.value,
                    parameters=MappingProxyType({"terminal_status": "denied", "expose_data": False}),
                    reason_code=RejectionCategory.SAFETY_POLICY_VIOLATION.value,
                ),
            )

        if state_view.fallback_count >= constraints.max_fallback_depth:
            return self._reject(
                RejectionCategory.RETRY_LOOP_PREVENTED,
                ["max_fallback_depth"],
                FallbackAction(
                    action=ActionType.TERMINATE.value,
                    parameters=MappingProxyType({"terminal_status": "failed"}),
                    reason_code=RejectionCategory.RETRY_LOOP_PREVENTED.value,
                ),
            )

        if fallback.action not in constraints.allowed_actions:
            return self._reject(
                RejectionCategory.UNSUPPORTED_ACTION,
                [f"allowed_actions:{fallback.action}"],
                FallbackAction(
                    action=ActionType.TERMINATE.value,
                    parameters=MappingProxyType({"terminal_status": "failed"}),
                    reason_code=RejectionCategory.UNSUPPORTED_ACTION.value,
                ),
            )

        if state_view.remaining_time_budget_seconds <= 0.0:
            return self._reject(
                RejectionCategory.TIMEOUT_EXCEEDED,
                ["time_budget_seconds"],
                FallbackAction(
                    action=ActionType.TERMINATE.value,
                    parameters=MappingProxyType({"terminal_status": "timeout"}),
                    reason_code=RejectionCategory.TIMEOUT_EXCEEDED.value,
                ),
            )

        # Non-terminal fallbacks that invoke model MUST also satisfy step & context budget (R1-FIX-07.2)
        if fallback.action in (ActionType.SAFE_RESPOND.value, ActionType.DEFAULT_CONFIG.value):
            if state_view.step_count >= constraints.max_steps:
                return self._reject(
                    RejectionCategory.STEP_BUDGET_EXCEEDED,
                    ["max_steps"],
                    FallbackAction(
                        action=ActionType.TERMINATE.value,
                        parameters=MappingProxyType({"terminal_status": "failed"}),
                        reason_code=RejectionCategory.STEP_BUDGET_EXCEEDED.value,
                    ),
                )
            if state_view.estimated_context_tokens > constraints.context_budget_tokens:
                return self._reject(
                    RejectionCategory.CONTEXT_BUDGET_INSUFFICIENT,
                    ["context_budget_tokens"],
                    FallbackAction(
                        action=ActionType.TERMINATE.value,
                        parameters=MappingProxyType({"terminal_status": "failed"}),
                        reason_code=RejectionCategory.CONTEXT_BUDGET_INSUFFICIENT.value,
                    ),
                )

        if fallback.action == ActionType.CALL_TOOLS.value:
            for tname in fallback.parameters.get("tool_names", ()):
                allowed, deny_reason = constraints.check_tool_permission(tname)
                if not allowed:
                    return self._reject(
                        RejectionCategory.PERMISSION_VIOLATION,
                        [deny_reason or f"permission_denied:{tname}"],
                        FallbackAction(
                            action=ActionType.DENY.value,
                            parameters=MappingProxyType({"terminal_status": "denied", "expose_data": False}),
                            reason_code=RejectionCategory.PERMISSION_VIOLATION.value,
                        ),
                    )

        va = self._mint(
            action=fallback.action,
            parameters=fallback.parameters,
            constraints=constraints,
            deadline_monotonic=deadline_monotonic,
            fallback_applied=True,
            reason_code=fallback.reason_code,
        )
        return ValidationResult(accepted=True, validated_action=va)

    @staticmethod
    def _reject(
        category: RejectionCategory,
        violated: List[str],
        fallback: FallbackAction,
    ) -> ValidationResult:
        return ValidationResult(
            accepted=False,
            reason_code=category.value,
            violated_constraints=tuple(violated),
            fallback_action=fallback,
        )
