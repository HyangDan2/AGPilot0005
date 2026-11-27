"""Canvas error contract.

Mirrors the R2-W workspace error envelope (``{"detail": {code, message, retryable, context}}``) so
the HD2AI frontend handles Canvas and Workspace failures with one code path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class CanvasErrorCode(str, Enum):
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    REVISION_NOT_FOUND = "revision_not_found"
    REVISION_CONFLICT = "revision_conflict"
    EXPECTED_TEXT_MISMATCH = "expected_text_mismatch"
    INVALID_SELECTION = "invalid_selection"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_ARTIFACT_TYPE = "unsupported_artifact_type"
    CONTENT_TOO_LARGE = "content_too_large"
    REVISION_LIMIT_REACHED = "revision_limit_reached"
    PROPOSAL_NOT_FOUND = "proposal_not_found"
    PROPOSAL_STALE = "proposal_stale"
    WORKSPACE_UNAVAILABLE = "workspace_unavailable"
    WORKSPACE_ACCESS_DENIED = "workspace_access_denied"
    EXPORT_FORMAT_UNSUPPORTED = "export_format_unsupported"
    AI_ADAPTER_UNAVAILABLE = "ai_adapter_unavailable"
    STORAGE_FAILURE = "storage_failure"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"


ERROR_STATUS: Dict[CanvasErrorCode, int] = {
    CanvasErrorCode.ARTIFACT_NOT_FOUND: 404,
    CanvasErrorCode.REVISION_NOT_FOUND: 404,
    CanvasErrorCode.REVISION_CONFLICT: 409,
    CanvasErrorCode.EXPECTED_TEXT_MISMATCH: 409,
    CanvasErrorCode.INVALID_SELECTION: 422,
    CanvasErrorCode.INVALID_REQUEST: 422,
    CanvasErrorCode.UNSUPPORTED_ARTIFACT_TYPE: 415,
    CanvasErrorCode.CONTENT_TOO_LARGE: 413,
    CanvasErrorCode.REVISION_LIMIT_REACHED: 409,
    CanvasErrorCode.PROPOSAL_NOT_FOUND: 404,
    CanvasErrorCode.PROPOSAL_STALE: 409,
    CanvasErrorCode.WORKSPACE_UNAVAILABLE: 503,
    CanvasErrorCode.WORKSPACE_ACCESS_DENIED: 403,
    CanvasErrorCode.EXPORT_FORMAT_UNSUPPORTED: 400,
    CanvasErrorCode.AI_ADAPTER_UNAVAILABLE: 503,
    CanvasErrorCode.STORAGE_FAILURE: 500,
    CanvasErrorCode.UNAUTHENTICATED: 401,
    CanvasErrorCode.FORBIDDEN: 403,
}


@dataclass
class CanvasError(Exception):
    """Domain failure carrying an explicit, non-silent reason."""

    code: CanvasErrorCode
    message: str = ""
    retryable: bool = False
    context: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code.value}: {self.message}" if self.message else self.code.value)

    @property
    def http_status(self) -> int:
        return ERROR_STATUS.get(self.code, 400)

    def to_detail(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "context": self.context,
        }
