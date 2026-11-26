"""FIX-R1-01: AGPolicy — Read-only decision proposer without state mutation or execution authority."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, Mapping, Optional, Protocol, Tuple

from app.runtime.policy.constraints import ActionType, ConstraintSpec


class DecisionType(str, Enum):
    """R1-FIX-06: Typed Policy Decision Contract."""

    NEXT_NODE = "NEXT_NODE"
    TOOL_SELECTION = "TOOL_SELECTION"
    CONTEXT_ALLOCATION = "CONTEXT_ALLOCATION"
    MODEL_PREFERENCE = "MODEL_PREFERENCE"
    REASONING_CONFIG = "REASONING_CONFIG"
    # Reserved contracts for follow-up phases (NOT active in Phase R1.1)
    RETRIEVAL_CONFIG = "RETRIEVAL_CONFIG"
    VERIFICATION = "VERIFICATION"
    REPLANNING = "REPLANNING"
    TERMINATION = "TERMINATION"


RESERVED_DECISION_TYPES: FrozenSet[str] = frozenset(
    {
        DecisionType.RETRIEVAL_CONFIG.value,
        DecisionType.VERIFICATION.value,
        DecisionType.REPLANNING.value,
    }
)


@dataclass(frozen=True)
class StateView:
    """Immutable read-only projection of runtime state passed to AGPolicy and ActionValidator.

    Structurally prevents AGPolicy from mutating AgentState or invoking tools/clients directly (REG-POL-11).
    """

    run_id: str
    service_id: str
    step_count: int
    tool_call_count: int
    elapsed_seconds: float
    remaining_time_budget_seconds: float
    estimated_context_tokens: int
    has_pending_tool_calls: bool
    pending_tool_names: Tuple[str, ...] = ()
    pending_tool_call_ids: Tuple[str, ...] = ()
    pending_tool_count: int = 0
    has_final_answer: bool = False
    context_compacted: bool = False
    fallback_count: int = 0
    reasoning_effort: str = "default"
    rejected_signatures: FrozenSet[str] = field(default_factory=frozenset)
    last_tool_quality: Optional[str] = None
    extra_features: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def to_features_dict(self) -> Dict[str, Any]:
        """Privacy-safe numeric/categorical features for DecisionEvent/OutcomeEvent (REG-POL-20)."""
        return {
            "service_id": self.service_id,
            "step_count": self.step_count,
            "tool_call_count": self.tool_call_count,
            "remaining_time_budget_seconds": round(self.remaining_time_budget_seconds, 4),
            "estimated_context_tokens": self.estimated_context_tokens,
            "has_pending_tool_calls": self.has_pending_tool_calls,
            "pending_tool_count": self.pending_tool_count,
            "has_final_answer": self.has_final_answer,
            "context_compacted": self.context_compacted,
            "fallback_count": self.fallback_count,
            "reasoning_effort": self.reasoning_effort,
            "last_tool_quality": self.last_tool_quality,
        }


@dataclass(frozen=True)
class PolicyProposal:
    """Proposal returned by AGPolicy. Treated strictly as a proposal until ActionValidator approves it."""

    decision_type: str  # "step_action" | "tool_dispatch" | "recovery"
    chosen_action: str  # ActionType value (kept as str so unsupported actions can be caught by Validator)
    proposed_parameters: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    behavior_policy_version: str = "agpolicy-r1-v1"
    action_probability: Optional[float] = None  # None when deterministic/unknown (FIX-R1-03)

    def signature(self) -> str:
        tool_names = tuple(self.proposed_parameters.get("tool_names", ()))
        return f"{self.decision_type}:{self.chosen_action}:{','.join(tool_names)}"


class AGPolicy(Protocol):
    version: str

    def propose(self, state_view: StateView, constraints: ConstraintSpec) -> PolicyProposal:
        """Return the next proposed action based strictly on read-only StateView and ConstraintSpec."""
        ...


class DefaultAGPolicy:
    """Deterministic baseline AGPolicy for Phase R1 parity with legacy AgentRunner."""

    version: str = "agpolicy-r1-v1"

    def propose(self, state_view: StateView, constraints: ConstraintSpec) -> PolicyProposal:
        if state_view.has_pending_tool_calls:
            return PolicyProposal(
                decision_type="tool_dispatch",
                chosen_action=ActionType.CALL_TOOLS.value,
                proposed_parameters=MappingProxyType(
                    {
                        "tool_call_ids": state_view.pending_tool_call_ids,
                        "tool_names": state_view.pending_tool_names,
                        "tool_count": state_view.pending_tool_count,
                    }
                ),
                behavior_policy_version=self.version,
                action_probability=None,
            )

        if state_view.has_final_answer:
            return PolicyProposal(
                decision_type="step_action",
                chosen_action=ActionType.TERMINATE.value,
                proposed_parameters=MappingProxyType({"terminal_status": "completed"}),
                behavior_policy_version=self.version,
                action_probability=None,
            )

        return PolicyProposal(
            decision_type="step_action",
            chosen_action=ActionType.CALL_MODEL.value,
            proposed_parameters=MappingProxyType(
                {
                    "reasoning_effort": state_view.reasoning_effort or constraints.default_reasoning_effort,
                    "context_budget_tokens": constraints.context_budget_tokens,
                }
            ),
            behavior_policy_version=self.version,
            action_probability=None,
        )
