"""FIX-R1-02: ActionValidator & Deterministic Safe Fallback."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.runtime.policy.ag_policy import PolicyProposal, StateView
from app.runtime.policy.constraints import (
    ActionType,
    ConstraintSpec,
    VALID_REASONING_EFFORTS,
)


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
    """Structured validation outcome mandated by FIX-R1-02."""

    accepted: bool
    reason_code: Optional[str] = None
    violated_constraints: Tuple[str, ...] = ()
    fallback_action: Optional[FallbackAction] = None

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
    - Deterministically maps each rejection category to a safe fallback.
    - Re-validates every fallback action (REG-POL-13) and prevents infinite retries (REG-POL-14).
    """

    def validate(
        self,
        proposal: PolicyProposal,
        state_view: StateView,
        constraints: ConstraintSpec,
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

        # 6. Action-specific checks
        if proposal.chosen_action == ActionType.CALL_TOOLS.value:
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

            # Permission check against ConstraintSpec (SSOT)
            denied: List[str] = []
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

            # Tool call budget check -> SAFE_RESPOND or TERMINATE
            requested_count = int(proposal.proposed_parameters.get("tool_count", len(tool_names)))
            if state_view.tool_call_count + requested_count > constraints.max_tool_calls:
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

        elif proposal.chosen_action == ActionType.CALL_MODEL.value:
            # Step budget check
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

            # Parameter validation -> DEFAULT_CONFIG or TERMINATE
            effort = proposal.proposed_parameters.get("reasoning_effort", constraints.default_reasoning_effort)
            budget = proposal.proposed_parameters.get("context_budget_tokens", constraints.context_budget_tokens)
            invalid_params: List[str] = []
            if effort not in VALID_REASONING_EFFORTS:
                invalid_params.append(f"invalid_reasoning_effort:{effort}")
            if not isinstance(budget, (int, float)) or budget <= 0 or budget > constraints.context_budget_tokens:
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

            # Context budget check -> verified COMPACT_CONTEXT or TERMINATE
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

        return ValidationResult(accepted=True)

    def validate_fallback(
        self,
        fallback: FallbackAction,
        state_view: StateView,
        constraints: ConstraintSpec,
    ) -> ValidationResult:
        """Re-validates a FallbackAction before execution (FIX-R1-02 / REG-POL-13 / REG-POL-14)."""
        # DENY and TERMINATE are always terminal safe exits
        if fallback.action in (ActionType.DENY.value, ActionType.TERMINATE.value):
            return ValidationResult(accepted=True)

        # Auth/safety/permission failures can NEVER degrade into SAFE_RESPOND or expose data
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

        # Prevent infinite fallback chains (REG-POL-14)
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

        # Verify fallback action is allowed by ConstraintSpec
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

        # Time budget check for non-terminal fallbacks
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

        # If fallback requests a tool call, re-check tool permissions via ConstraintSpec
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

        return ValidationResult(accepted=True)

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
