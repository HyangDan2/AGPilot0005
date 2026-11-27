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
    # WO-C17 additions
    PROPOSAL_ALREADY_APPLIED = "proposal_already_applied"
    PROPOSAL_REJECTED = "proposal_rejected"
    USER_APPROVAL_REQUIRED = "user_approval_required"
    CANVAS_DISABLED = "canvas_disabled"
    # R2-W Amendment 01 (folder-based workspace, `r2-w/v1+a01`) — the eight codes added by
    # §A1.10 plus the v1 codes Canvas now surfaces unchanged through the write/export path.
    WORKSPACE_CLOSED = "workspace_closed"
    READ_ONLY_WORKSPACE = "read_only_workspace"
    PATH_OUTSIDE_ALLOWED_ROOT = "path_outside_allowed_root"
    SYMLINK_ESCAPE = "symlink_escape"
    ROOT_UNAVAILABLE = "root_unavailable"
    PROVIDER_MISMATCH = "provider_mismatch"
    CONTENT_CONFLICT = "content_conflict"
    SOURCE_DELETE_NOT_PERMITTED = "source_delete_not_permitted"
    METADATA_OVERLAP = "metadata_overlap"
    WATCH_UNAVAILABLE = "watch_unavailable"
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    WORKSPACE_FILE_NOT_FOUND = "file_not_found"
    WORKSPACE_FILE_EXISTS = "file_exists"
    CONVERSATION_WORKSPACE_CONFLICT = "conversation_workspace_conflict"
    INVALID_PATH = "invalid_path"
    INDEX_NOT_READY = "index_not_ready"


ERROR_STATUS: Dict[CanvasErrorCode, int] = {
    CanvasErrorCode.CONVERSATION_WORKSPACE_CONFLICT: 409,
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
    # WO-C17 additions
    CanvasErrorCode.PROPOSAL_ALREADY_APPLIED: 409,
    CanvasErrorCode.PROPOSAL_REJECTED: 409,
    CanvasErrorCode.USER_APPROVAL_REQUIRED: 403,
    CanvasErrorCode.CANVAS_DISABLED: 503,
    # R2-W Amendment 01 (§A1.10 status mapping is authoritative)
    CanvasErrorCode.WORKSPACE_CLOSED: 409,
    CanvasErrorCode.READ_ONLY_WORKSPACE: 403,
    CanvasErrorCode.PATH_OUTSIDE_ALLOWED_ROOT: 403,
    CanvasErrorCode.SYMLINK_ESCAPE: 400,
    CanvasErrorCode.ROOT_UNAVAILABLE: 409,
    CanvasErrorCode.PROVIDER_MISMATCH: 400,
    CanvasErrorCode.CONTENT_CONFLICT: 409,
    CanvasErrorCode.SOURCE_DELETE_NOT_PERMITTED: 403,
    CanvasErrorCode.METADATA_OVERLAP: 400,
    CanvasErrorCode.WATCH_UNAVAILABLE: 409,
    CanvasErrorCode.WORKSPACE_NOT_FOUND: 404,
    CanvasErrorCode.WORKSPACE_FILE_NOT_FOUND: 404,
    CanvasErrorCode.WORKSPACE_FILE_EXISTS: 409,
    CanvasErrorCode.INVALID_PATH: 400,
    CanvasErrorCode.INDEX_NOT_READY: 409,
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
