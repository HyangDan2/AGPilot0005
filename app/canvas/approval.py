"""User approval boundary (WO-C17 / C-FIX-06).

An AI tool call is **not** a user approval. Applying an edit proposal writes a new artifact
revision in the user's name, so Canvas requires an approval object that only the verified
interactive path can build:

* ``POST /api/v1/canvas/edit-proposals/{id}/apply`` builds one from the verified ``ExecutionScope``
  (an interactive user principal) plus an explicit ``user_approved: true`` field, which is part of
  the request body, not of any tool schema.
* ``canvas.apply_edit`` (the model-facing tool) never receives an approval. It therefore returns
  ``403 user_approval_required`` and the artifact is untouched. The tool is also excluded from the
  AI-exposed registration set until the R1.3 integration defines its approval channel.
* Management/automation principals (``role`` of ``service``, ``tool``, ``system``, ``guest``, …)
  are refused even when they hold an owner scope, so a shared service credential cannot approve on
  the user's behalf.
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.canvas.errors import CanvasError, CanvasErrorCode

"""Roles that can be an interactive approval (a human sitting in front of the UI)."""
USER_ROLES = frozenset({"user", "owner", "admin", "admiral"})
"""Machine principals: they hold a scope but can never stand in for the user's approval."""
MANAGEMENT_ROLES = frozenset({"service", "tool", "ai", "assistant", "automation", "system", "guest"})


@dataclass(frozen=True)
class UserApproval:
    """Proof that an interactive user approved one specific proposal."""

    approval_id: str
    approved_by: str
    proposal_id: str
    source: str = "user_action"

    def model_dump(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "approved_by": self.approved_by,
            "proposal_id": self.proposal_id,
            "source": self.source,
        }


# Set only by the verified user-facing endpoint for the duration of one apply call.
current_user_approval: ContextVar[Optional[UserApproval]] = ContextVar(
    "canvas_user_approval", default=None
)


def build_user_approval(
    scope: Any,
    *,
    proposal_id: str,
    user_approved: bool,
    approval_reference: Optional[str] = None,
) -> UserApproval:
    """Build the approval from a verified scope; refuse anything that is not an explicit user act."""
    if scope is None or not getattr(scope, "authenticated", False):
        raise CanvasError(
            CanvasErrorCode.UNAUTHENTICATED, "no verified execution scope for the approval"
        )
    role = str(getattr(scope, "role", "") or "").strip().lower()
    if role not in USER_ROLES:
        raise CanvasError(
            CanvasErrorCode.USER_APPROVAL_REQUIRED,
            "applying an edit proposal requires an interactive user approval",
            context={
                "role": role or "unknown",
                "required_roles": sorted(USER_ROLES),
                "tool_call_is_approval": False,
            },
        )
    if user_approved is not True:
        raise CanvasError(
            CanvasErrorCode.USER_APPROVAL_REQUIRED,
            "the request must carry the user's explicit approval (user_approved: true)",
            context={
                "proposal_id": proposal_id,
                "user_approved": bool(user_approved),
                "tool_call_is_approval": False,
            },
        )
    reference = (approval_reference or "").strip() or f"apr_{uuid.uuid4().hex[:12]}"
    return UserApproval(
        approval_id=reference[:120],
        approved_by=str(getattr(scope, "owner_id", "") or ""),
        proposal_id=proposal_id,
        source="user_action",
    )


def require_user_approval(approval: Optional[UserApproval], *, proposal_id: str) -> UserApproval:
    """Every apply path (HTTP and internal) goes through this guard."""
    if approval is None:
        raise CanvasError(
            CanvasErrorCode.USER_APPROVAL_REQUIRED,
            "applying an edit proposal requires an explicit user approval",
            context={"proposal_id": proposal_id, "tool_call_is_approval": False},
        )
    if approval.proposal_id != proposal_id:
        raise CanvasError(
            CanvasErrorCode.USER_APPROVAL_REQUIRED,
            "user approval does not cover this proposal",
            context={"proposal_id": proposal_id, "approved_proposal_id": approval.proposal_id},
        )
    if approval.source != "user_action":
        raise CanvasError(
            CanvasErrorCode.USER_APPROVAL_REQUIRED,
            "only an interactive user action can approve a proposal",
            context={"proposal_id": proposal_id, "source": approval.source},
        )
    return approval


def approval_policy() -> Dict[str, Any]:
    return {
        "apply_requires_user_approval": True,
        "tool_call_is_approval": False,
        "accepted_roles": sorted(USER_ROLES),
        "refused_roles": sorted(MANAGEMENT_ROLES),
        "ai_apply_tool_exposed": False,
        "note": "R1.3 runtime integration must supply its own verified user approval channel.",
    }
