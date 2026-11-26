"""FIX-R1-01: ConstraintSpec — Single Source of Truth for permissions, budgets, and limits."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterable, Optional, Tuple

from app.tools.policy import tool_policy


class ActionType(str, Enum):
    CALL_MODEL = "CALL_MODEL"
    CALL_TOOLS = "CALL_TOOLS"
    COMPACT_CONTEXT = "COMPACT_CONTEXT"
    SAFE_RESPOND = "SAFE_RESPOND"
    DEFAULT_CONFIG = "DEFAULT_CONFIG"
    DENY = "DENY"
    TERMINATE = "TERMINATE"


DEFAULT_ALLOWED_ACTIONS: FrozenSet[str] = frozenset(a.value for a in ActionType)
VALID_REASONING_EFFORTS: FrozenSet[str] = frozenset({"off", "low", "medium", "high", "default"})


@dataclass(frozen=True)
class ConstraintSpec:
    """Immutable execution boundary specification shared by Runtime, Validator, and Dispatcher.

    ConstraintSpec is the single source of truth for permissions, step/tool/context/time budgets.
    ActionValidator and ToolDispatcher MUST reference this spec rather than duplicating rules.
    """

    service_id: str
    owner_id: str
    authenticated: bool = True
    allowed_tools: FrozenSet[str] = field(default_factory=frozenset)
    allowed_actions: FrozenSet[str] = field(default_factory=lambda: DEFAULT_ALLOWED_ACTIONS)
    max_steps: int = 20
    max_tool_calls: int = 16
    time_budget_seconds: float = 180.0
    context_budget_tokens: int = 12000
    max_fallback_depth: int = 1
    safety_blocked: bool = False
    safety_reason: Optional[str] = None
    default_reasoning_effort: str = "default"
    check_live_tool_policy: bool = True

    def check_tool_permission(self, tool_name: str) -> Tuple[bool, Optional[str]]:
        """Authoritative tool permission check referencing ConstraintSpec + live ToolPolicyStore."""
        if not self.authenticated:
            return False, "auth_required"
        if self.safety_blocked:
            return False, self.safety_reason or "safety_policy_violation"
        if tool_name not in self.allowed_tools:
            return False, f"tool_not_in_constraint_spec:{tool_name}"
        if self.check_live_tool_policy:
            allowed, deny_reason = tool_policy.is_tool_allowed(tool_name, self.service_id)
            if not allowed:
                return False, deny_reason or f"tool_policy_denied:{tool_name}"
        return True, None

    def to_summary_dict(self) -> Dict[str, Any]:
        """Sanitized, deterministic constraint summary for Trajectory DecisionEvent."""
        return {
            "service_id": self.service_id,
            "authenticated": self.authenticated,
            "allowed_tools": sorted(self.allowed_tools),
            "allowed_actions": sorted(self.allowed_actions),
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "time_budget_seconds": self.time_budget_seconds,
            "context_budget_tokens": self.context_budget_tokens,
            "max_fallback_depth": self.max_fallback_depth,
            "default_reasoning_effort": self.default_reasoning_effort,
        }

    def digest(self) -> str:
        payload = json.dumps(self.to_summary_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_runtime(
        cls,
        *,
        service_id: str,
        owner_id: str,
        allowed_tools: Iterable[str],
        max_steps: int,
        max_tool_calls: int,
        time_budget_seconds: float,
        context_budget_tokens: int,
        authenticated: bool = True,
        safety_blocked: bool = False,
        safety_reason: Optional[str] = None,
        default_reasoning_effort: str = "default",
        check_live_tool_policy: bool = True,
    ) -> "ConstraintSpec":
        effort = default_reasoning_effort if default_reasoning_effort in VALID_REASONING_EFFORTS else "default"
        return cls(
            service_id=service_id,
            owner_id=owner_id,
            authenticated=authenticated,
            allowed_tools=frozenset(allowed_tools),
            max_steps=max(1, int(max_steps)),
            max_tool_calls=max(0, int(max_tool_calls)),
            time_budget_seconds=max(0.0, float(time_budget_seconds)),
            context_budget_tokens=max(256, int(context_budget_tokens)),
            safety_blocked=safety_blocked,
            safety_reason=safety_reason,
            default_reasoning_effort=effort,
            check_live_tool_policy=check_live_tool_policy,
        )
