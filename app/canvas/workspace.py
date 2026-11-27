"""Workspace/RAG gateway (shared contract consumer).

Canvas must not implement workspace storage, indexing or authorization. It consumes the frozen
R2-W contract (``docs/r2/WORKSPACE_CONTRACT.md``) through the provider operations only:
``check_workspace_access``, ``get_workspace``, ``register_artifact_reference``,
``list_artifact_references`` and — since **Amendment 01** (folder-based workspace,
``r2-w/v1+a01``) — ``write_file`` for "export to workspace".

Contract revisions
------------------
* ``WORKSPACE_CONTRACT_VERSION = "r2-w/v1"`` — the frozen v1 surface Canvas has always consumed.
  Nothing in this module weakens it: every v1 field keeps its meaning and a v1-only provider
  (``mode = "workspace"``, only the §2 operations) still works for the v1 code paths.
* ``WORKSPACE_CONTRACT_REVISION = "r2-w/v1+a01"`` — the additive revision Canvas now understands.
  Only the *additive* members are read, so a v1 consumer that never looks at them is unaffected.

Permission model (WO-C17 / C-FIX-01)
------------------------------------
The R2-W contract answers with one of four levels: ``read`` / ``write`` / ``admin`` / ``none``.
Canvas distinguishes two required levels and never conflates them:

* ``read`` — listing, reading content, revisions, diffs, previews, exports, proposals inspection.
  Satisfied by ``read``, ``write`` or ``admin``.
* ``write`` — creating an artifact *into* a workspace, linking an artifact to a workspace, renaming,
  deleting, patching, restoring, creating/applying/rejecting proposals, and writing an export into
  a workspace folder. Satisfied by ``write`` or ``admin`` only. A ``read`` grant can never create
  or mutate workspace-linked state.

Amendment 01 state (``r2-w/v1+a01``)
------------------------------------
The same access answer now also carries where the root lives and whether it is usable:

* ``lifecycle_state`` — ``open`` | ``closed`` | ``deleted``. ``closed`` keeps every row but refuses
  reads, writes and exports with ``409 workspace_closed`` until it is reopened; a detached
  (``deleted``) workspace answers ``404 workspace_not_found``. Detaching an artifact link
  (``DELETE /links/workspace``) deliberately stays outside this check so a C-FIX-02 owner can
  always leave a closed/revoked workspace.
* ``read_only`` — a root opened read-only refuses every write with ``403 read_only_workspace``.
* ``storage_provider`` — ``server_managed`` | ``server_mounted_folder`` | ``local_browser``.
  For ``local_browser`` the server has **no** path access: ``server_side_path_access`` is false,
  no real path is ever reported, and a write is answered with a ``client_write`` directive for the
  browser to perform through its file handle.

Three providers exist:

* ``RuntimeWorkspaceProvider`` — binds ``app/workspace/contracts.py::WorkspaceProvider`` as soon as
  the Workspace branch is merged. It is imported lazily so this branch builds and tests without it.
* ``LocalDevWorkspaceProvider`` — in-memory development provider. It is **not** an authorization
  layer: every response carries ``provider_mode: "dev"`` and ``enforced: false``, and the provider
  refuses to be constructed in production. It now also simulates the Amendment 01 lifecycle
  (``set_lifecycle_state``, ``set_read_only``, ``set_storage_provider``) and the common write
  contract so the closed / read-only / ``local_browser`` paths can be verified without the engine.
* ``UnavailableWorkspaceProvider`` — fail-closed default; raises ``workspace_unavailable``.
"""
from __future__ import annotations

import base64
import hashlib
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.canvas.errors import CanvasError, CanvasErrorCode

#: Frozen v1 contract surface Canvas has always consumed (unchanged by Amendment 01).
WORKSPACE_CONTRACT_VERSION = "r2-w/v1"
#: Additive revision Canvas understands on top of v1.
WORKSPACE_CONTRACT_AMENDMENT = "A01"
WORKSPACE_CONTRACT_REVISION = "r2-w/v1+a01"

"""Permission ladder, lowest first. A higher level satisfies every lower requirement."""
PERMISSION_LADDER = ("none", "read", "write", "admin")
READ_LEVELS = ("read", "write", "admin")
WRITE_LEVELS = ("write", "admin")
LEVEL_RANK = {level: rank for rank, level in enumerate(PERMISSION_LADDER)}


# --------------------------------------------------------------------------- Amendment 01 types


class WorkspaceStorageProvider(str, Enum):
    """Where the workspace root physically lives (contract §A1.1)."""

    SERVER_MANAGED = "server_managed"
    SERVER_MOUNTED_FOLDER = "server_mounted_folder"
    LOCAL_BROWSER = "local_browser"


class WorkspaceLifecycleState(str, Enum):
    """Connection lifecycle of the workspace root (contract §A1.2)."""

    OPEN = "open"
    CLOSED = "closed"
    DELETED = "deleted"


class ContentOrigin(str, Enum):
    """How a workspace file's bytes are reached (contract §A1.1)."""

    BLOB = "blob"
    EXTERNAL = "external"


STORAGE_PROVIDERS = tuple(provider.value for provider in WorkspaceStorageProvider)
LIFECYCLE_STATES = tuple(state.value for state in WorkspaceLifecycleState)


class WorkspaceRootInfo(BaseModel):
    """Root binding of a workspace (contract §A1.1 / §A1.7), Canvas-side mirror."""

    model_config = ConfigDict(extra="forbid")

    storage_provider: WorkspaceStorageProvider = WorkspaceStorageProvider.SERVER_MANAGED
    root_id: Optional[str] = None
    display_path: str = ""
    allowed_root_id: Optional[str] = None
    read_only: bool = False
    zero_copy: bool = False
    server_side_path_access: bool = True
    client_handle_id: Optional[str] = None
    opened_at: Optional[str] = None
    status: str = "available"


class ClientWriteDirective(BaseModel):
    """Returned when only the browser can perform the write (``local_browser``)."""

    model_config = ConfigDict(extra="forbid")

    required: bool = True
    client_handle_id: Optional[str] = None
    relative_path: str
    reason: str = "local_browser_no_server_path_access"


class WorkspaceWriteRequest(BaseModel):
    """Canvas-side mirror of ``WorkspaceWriteRequest`` (contract §A1.5).

    Used only when the real ``app/workspace`` package is not importable on this branch; when it is,
    :func:`build_write_request` builds the contract's own model instead so the provider validates
    it exactly as it would for the Coding Agent.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    content: Optional[str] = None
    content_base64: Optional[str] = None
    mime_type: Optional[str] = Field(default=None, max_length=200)
    expected_content_hash: Optional[str] = Field(default=None, max_length=80)
    create_parents: bool = True
    overwrite: bool = True


class WorkspaceWriteResult(BaseModel):
    """Canvas-side mirror of ``WorkspaceWriteResult`` (contract §A1.5)."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    path: str
    file: Optional[Dict[str, Any]] = None
    revision: int = 0
    content_hash: Optional[str] = None
    written_bytes: int = 0
    storage_provider: WorkspaceStorageProvider = WorkspaceStorageProvider.SERVER_MANAGED
    content_origin: ContentOrigin = ContentOrigin.BLOB
    client_write: Optional[ClientWriteDirective] = None


class WorkspaceAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    level: str = "none"  # none | read | write | admin
    reason: str = ""
    provider_mode: str = "dev"
    enforced: bool = False
    exists: bool = True
    provider_mode_detail: Optional[str] = None
    # Amendment 01 (additive): root binding and lifecycle of the workspace.
    lifecycle_state: str = WorkspaceLifecycleState.OPEN.value
    storage_provider: str = WorkspaceStorageProvider.SERVER_MANAGED.value
    read_only: bool = False
    server_side_path_access: bool = True
    client_handle_id: Optional[str] = None
    root_display_path: str = ""
    root_id: Optional[str] = None
    allowed_root_id: Optional[str] = None
    contract_revision: str = WORKSPACE_CONTRACT_REVISION

    @property
    def allowed(self) -> bool:
        """Read access (kept for callers written against the pre-WO-C17 contract)."""
        return self.can_read

    @property
    def can_read(self) -> bool:
        return self.level in READ_LEVELS

    @property
    def can_write(self) -> bool:
        return self.level in WRITE_LEVELS

    @property
    def is_open(self) -> bool:
        return self.lifecycle_state == WorkspaceLifecycleState.OPEN.value

    @property
    def is_local_browser(self) -> bool:
        return self.storage_provider == WorkspaceStorageProvider.LOCAL_BROWSER.value

    def satisfies(self, required: str) -> bool:
        required = normalise_required(required)
        held = LEVEL_RANK.get(self.level, 0)
        return held >= LEVEL_RANK[required]


def normalise_required(required: str) -> str:
    value = (required or "read").strip().lower()
    if value not in ("read", "write"):
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            "workspace requirement must be 'read' or 'write'",
            context={"required": required},
        )
    return value


# --------------------------------------------------------------------------- path gate (mirror)

_PATH_MAX_SEGMENT = 255
_PATH_MAX_TOTAL = 1024
_FORBIDDEN_SEGMENTS = {"", ".", ".."}


def validate_relative_path(path: str, *, allow_empty: bool = False) -> str:
    """Validate and normalize a workspace-relative path.

    This is a byte-for-byte mirror of ``app/workspace/contracts.py::validate_relative_path`` (the
    contract's single path gate). Canvas keeps its own copy because ``app/workspace/**`` belongs to
    the Workspace/RAG engineer and is not importable from this branch; the rules and the failure
    code (``invalid_path``, 400) are identical, so a path the server accepts is a path Canvas
    accepts and vice versa.
    """

    raw = path or ""
    if not isinstance(raw, str):
        raise CanvasError(CanvasErrorCode.INVALID_PATH, "path must be a string", context={"path": repr(path)})
    if raw.strip() == "":
        if allow_empty:
            return ""
        raise _invalid_path(path, "empty path")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise _invalid_path(path, "control characters rejected")
    if "\\" in raw:
        raise _invalid_path(path, "backslash rejected")
    if raw.startswith("/") or raw.startswith("~"):
        raise _invalid_path(path, "absolute path rejected")
    if len(raw) > _PATH_MAX_TOTAL:
        raise _invalid_path(path, "path too long")
    if len(raw) >= 2 and raw[1] == ":":
        raise _invalid_path(path, "drive letter rejected")
    parts = raw.split("/")
    for part in parts:
        if part in _FORBIDDEN_SEGMENTS:
            raise _invalid_path(path, f"invalid path segment: {part!r}")
        if len(part) > _PATH_MAX_SEGMENT:
            raise _invalid_path(path, "path segment too long")
    return "/".join(parts)


def _invalid_path(path: Any, reason: str) -> CanvasError:
    return CanvasError(
        CanvasErrorCode.INVALID_PATH,
        f"invalid workspace path: {reason}",
        context={"path": path if isinstance(path, str) else repr(path), "reason": reason},
    )


# --------------------------------------------------------------------------- state enforcement


def enforce_workspace_state(
    access: WorkspaceAccess,
    *,
    required: str = "read",
    operation: str = "",
) -> WorkspaceAccess:
    """Apply the Amendment 01 lifecycle / read-only rules to an access answer.

    Called by :func:`app.canvas.manager.assert_workspace_level` *after* the permission level is
    satisfied, so a caller without access learns nothing about the workspace's lifecycle.
    """

    required = normalise_required(required)
    state = access.lifecycle_state or WorkspaceLifecycleState.OPEN.value
    if state == WorkspaceLifecycleState.DELETED.value:
        raise CanvasError(
            CanvasErrorCode.WORKSPACE_NOT_FOUND,
            "workspace is detached (lifecycle deleted)",
            context={
                "workspace_id": access.workspace_id,
                "lifecycle_state": state,
                "operation": operation,
            },
        )
    if state == WorkspaceLifecycleState.CLOSED.value:
        raise CanvasError(
            CanvasErrorCode.WORKSPACE_CLOSED,
            "workspace is closed; reopen it before reading, writing or exporting",
            retryable=True,
            context={
                "workspace_id": access.workspace_id,
                "lifecycle_state": state,
                "required_level": required,
                "operation": operation,
                "recovery": "POST /api/v1/workspaces/{id}/reopen",
            },
        )
    if required == "write" and access.read_only:
        raise CanvasError(
            CanvasErrorCode.READ_ONLY_WORKSPACE,
            "workspace root was opened read-only",
            context={
                "workspace_id": access.workspace_id,
                "required_level": "write",
                "operation": operation,
                "storage_provider": access.storage_provider,
            },
        )
    return access


def apply_workspace_identity(access: WorkspaceAccess, identity: Any) -> WorkspaceAccess:
    """Fold ``WorkspaceIdentity`` (Amendment 01 root/lifecycle) into an access answer."""

    if identity is None:
        return access
    lifecycle = _field(identity, "lifecycle_state", WorkspaceLifecycleState.OPEN.value)
    lifecycle = str(getattr(lifecycle, "value", lifecycle) or WorkspaceLifecycleState.OPEN.value)
    root = _field(identity, "root", None)
    provider = _field(root, "storage_provider", access.storage_provider)
    provider = str(getattr(provider, "value", provider) or WorkspaceStorageProvider.SERVER_MANAGED.value)
    if provider not in STORAGE_PROVIDERS:
        provider = WorkspaceStorageProvider.SERVER_MANAGED.value
    read_only = bool(_field(root, "read_only", access.read_only))
    server_access = _field(root, "server_side_path_access", None)
    if server_access is None:
        server_access = provider != WorkspaceStorageProvider.LOCAL_BROWSER.value
    server_access = bool(server_access) and provider != WorkspaceStorageProvider.LOCAL_BROWSER.value
    display_path = str(_field(root, "display_path", "") or "")
    if not server_access:
        # A client-owned root has no server path; never echo one even if a provider sends it.
        display_path = ""
    return access.model_copy(
        update={
            "lifecycle_state": lifecycle,
            "storage_provider": provider,
            "read_only": read_only,
            "server_side_path_access": server_access,
            "client_handle_id": _field(root, "client_handle_id", access.client_handle_id),
            "root_display_path": display_path,
            "root_id": _field(root, "root_id", access.root_id),
            "allowed_root_id": _field(root, "allowed_root_id", access.allowed_root_id),
            "contract_revision": WORKSPACE_CONTRACT_REVISION,
        }
    )


# --------------------------------------------------------------------------- provider error bridge

#: Every R2-W contract error code → the Canvas code that carries the same meaning and status.
#: The eight Amendment 01 codes (§A1.10) map one-to-one; v1 codes that were never surfaced before
#: keep their contract status.
WORKSPACE_ERROR_CODES: Dict[str, CanvasErrorCode] = {
    "workspace_not_found": CanvasErrorCode.WORKSPACE_NOT_FOUND,
    "workspace_closed": CanvasErrorCode.WORKSPACE_CLOSED,
    "read_only_workspace": CanvasErrorCode.READ_ONLY_WORKSPACE,
    "path_outside_allowed_root": CanvasErrorCode.PATH_OUTSIDE_ALLOWED_ROOT,
    "symlink_escape": CanvasErrorCode.SYMLINK_ESCAPE,
    "root_unavailable": CanvasErrorCode.ROOT_UNAVAILABLE,
    "provider_mismatch": CanvasErrorCode.PROVIDER_MISMATCH,
    "content_conflict": CanvasErrorCode.CONTENT_CONFLICT,
    "source_delete_not_permitted": CanvasErrorCode.SOURCE_DELETE_NOT_PERMITTED,
    "metadata_overlap": CanvasErrorCode.METADATA_OVERLAP,
    "watch_unavailable": CanvasErrorCode.WATCH_UNAVAILABLE,
    "file_not_found": CanvasErrorCode.WORKSPACE_FILE_NOT_FOUND,
    "file_exists": CanvasErrorCode.WORKSPACE_FILE_EXISTS,
    "revision_conflict": CanvasErrorCode.REVISION_CONFLICT,
    "quota_exceeded": CanvasErrorCode.CONTENT_TOO_LARGE,
    "file_too_large": CanvasErrorCode.CONTENT_TOO_LARGE,
    "unsupported_media_type": CanvasErrorCode.UNSUPPORTED_ARTIFACT_TYPE,
    "invalid_path": CanvasErrorCode.INVALID_PATH,
    "invalid_request": CanvasErrorCode.INVALID_REQUEST,
    "index_not_ready": CanvasErrorCode.INDEX_NOT_READY,
    "embedding_unavailable": CanvasErrorCode.WORKSPACE_UNAVAILABLE,
    "forbidden": CanvasErrorCode.FORBIDDEN,
}


def translate_workspace_error(
    exc: BaseException, *, workspace_id: str = "", operation: str = ""
) -> CanvasError:
    """Map a provider failure to the Canvas envelope without inventing a reason.

    A known contract code keeps its own code/status; an unknown failure stays ``workspace_unavailable``
    (503, retryable) and names the provider exception class, so a real outage is never reported as a
    client error and a contract error is never flattened into "unavailable".
    """

    if isinstance(exc, CanvasError):
        return exc
    code, detail = _provider_error(exc)
    mapped = WORKSPACE_ERROR_CODES.get(code) if code else None
    if mapped is None:
        return CanvasError(
            CanvasErrorCode.WORKSPACE_UNAVAILABLE,
            "workspace provider call failed",
            retryable=True,
            context={
                "workspace_id": workspace_id,
                "operation": operation,
                "reason": exc.__class__.__name__,
                "provider_code": code,
            },
        )
    context: Dict[str, Any] = dict(detail.get("context") or {}) if isinstance(detail, dict) else {}
    context.update({"workspace_id": workspace_id, "operation": operation, "provider_code": code})
    message = ""
    if isinstance(detail, dict):
        message = str(detail.get("message") or "")
    return CanvasError(
        mapped,
        message or f"workspace provider answered {code}",
        retryable=bool(detail.get("retryable")) if isinstance(detail, dict) else False,
        context=context,
    )


def _provider_error(exc: BaseException) -> Tuple[Optional[str], Any]:
    """Extract ``(workspace_error_code, detail)`` from a provider exception."""

    detail = getattr(exc, "detail", None)
    if detail is None and isinstance(exc, dict):
        detail = exc
    candidate = None
    if isinstance(detail, dict):
        candidate = detail.get("code")
    elif isinstance(detail, str):
        candidate = detail
    if candidate is None:
        candidate = getattr(exc, "code", None)
    candidate = getattr(candidate, "value", candidate)
    if isinstance(candidate, str) and candidate in WORKSPACE_ERROR_CODES:
        return candidate, detail
    return None, detail


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from a pydantic model, an object or a dict (provider-agnostic)."""

    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


# --------------------------------------------------------------------------- write contract (§A1.5)


def build_write_request(
    *,
    path: str,
    content: bytes,
    mime_type: Optional[str] = None,
    expected_content_hash: Optional[str] = None,
    create_parents: bool = True,
    overwrite: bool = True,
) -> Any:
    """Build the ``WorkspaceWriteRequest`` the provider expects.

    The real contract model is used when ``app/workspace`` is importable (the merged engine), so the
    provider validates exactly what the Coding Agent would send. Otherwise the local mirror is used;
    both carry the same fields and a provider only reads attributes.
    """

    payload: Dict[str, Any] = {
        "path": path,
        "mime_type": mime_type,
        "expected_content_hash": expected_content_hash,
        "create_parents": create_parents,
        "overwrite": overwrite,
    }
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and "\x00" not in text:
        payload["content"] = text
    else:
        payload["content_base64"] = base64.b64encode(content).decode("ascii")
    try:  # pragma: no cover - only when the Workspace branch is merged
        from app.workspace.contracts import WorkspaceWriteRequest as ContractWriteRequest

        return ContractWriteRequest(**payload)
    except Exception:
        return WorkspaceWriteRequest(**payload)


def _request_field(request: Any, name: str, default: Any = None) -> Any:
    return _field(request, name, default)


def request_content_bytes(request: Any) -> bytes:
    """Return the bytes a write request carries (``content`` or ``content_base64``)."""

    text = _request_field(request, "content", None)
    if text is not None:
        return str(text).encode("utf-8")
    encoded = _request_field(request, "content_base64", None)
    if encoded:
        try:
            return base64.b64decode(str(encoded), validate=True)
        except Exception as exc:  # pragma: no cover - defensive
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST, "content_base64 is not valid base64"
            ) from exc
    return b""


def write_workspace_file(
    gateway: "WorkspaceGateway",
    scope: Any,
    access: WorkspaceAccess,
    *,
    path: str,
    content: bytes,
    mime_type: Optional[str] = None,
    expected_content_hash: Optional[str] = None,
    create_parents: bool = True,
    overwrite: bool = True,
) -> Dict[str, Any]:
    """Write one file into a workspace through the shared Amendment 01 write contract.

    Canvas never touches a workspace path itself: the path is validated with the contract gate and
    the provider performs the write (real file, atomic replace, index enqueue) or answers with a
    ``client_write`` directive for a browser-owned root.
    """

    relative = validate_relative_path(path)
    request = build_write_request(
        path=relative,
        content=content,
        mime_type=mime_type,
        expected_content_hash=expected_content_hash,
        create_parents=create_parents,
        overwrite=overwrite,
    )
    writer = getattr(gateway, "write_file", None)
    if not callable(writer):
        raise CanvasError(
            CanvasErrorCode.PROVIDER_MISMATCH,
            "the workspace provider does not implement the Amendment 01 file write contract",
            context={
                "workspace_id": access.workspace_id,
                "storage_provider": access.storage_provider,
                "required_operation": "write_file",
                "contract_revision": WORKSPACE_CONTRACT_REVISION,
            },
        )
    try:
        result = writer(access.workspace_id, scope, request)
    except CanvasError:
        raise
    except TypeError as exc:
        raise CanvasError(
            CanvasErrorCode.PROVIDER_MISMATCH,
            "workspace provider write_file is not compatible with the Amendment 01 write contract",
            context={
                "workspace_id": access.workspace_id,
                "reason": str(exc),
                "contract_revision": WORKSPACE_CONTRACT_REVISION,
            },
        ) from exc
    except Exception as exc:
        raise translate_workspace_error(
            exc, workspace_id=access.workspace_id, operation="write_file"
        ) from exc
    return normalise_write_result(result, access=access, path=relative)


_WRITE_RESULT_FIELDS = (
    "workspace_id",
    "path",
    "file",
    "revision",
    "content_hash",
    "written_bytes",
    "storage_provider",
    "content_origin",
    "client_write",
)


def normalise_write_result(
    result: Any, *, access: Optional[WorkspaceAccess] = None, path: Optional[str] = None
) -> Dict[str, Any]:
    """Return the write result as a JSON-safe dict with the contract's fields always present."""

    payload = _dump(result)
    if not isinstance(payload, dict):
        # A provider may answer with a plain object instead of the contract model; read the
        # documented fields rather than refusing a successful write.
        payload = {name: _field(result, name, None) for name in _WRITE_RESULT_FIELDS}
    if not isinstance(payload, dict):  # pragma: no cover - defensive
        raise CanvasError(
            CanvasErrorCode.PROVIDER_MISMATCH,
            "workspace provider write_file returned an unexpected payload",
            context={"type": type(result).__name__},
        )
    workspace_id = payload.get("workspace_id") or (access.workspace_id if access else "")
    storage_provider = payload.get("storage_provider") or (
        access.storage_provider if access else WorkspaceStorageProvider.SERVER_MANAGED.value
    )
    storage_provider = str(getattr(storage_provider, "value", storage_provider))
    origin = payload.get("content_origin")
    if origin is None:
        origin = (
            ContentOrigin.BLOB.value
            if storage_provider == WorkspaceStorageProvider.SERVER_MANAGED.value
            else ContentOrigin.EXTERNAL.value
        )
    origin = str(getattr(origin, "value", origin))
    file_payload = _dump(payload.get("file"))
    client_write = _dump(payload.get("client_write"))
    written = payload.get("written_bytes")
    if written is None and isinstance(file_payload, dict):
        written = file_payload.get("size_bytes", 0)
    normalised = {
        "workspace_id": workspace_id,
        "path": payload.get("path") or path or "",
        "file": file_payload,
        "revision": int(payload.get("revision") or 0),
        "content_hash": payload.get("content_hash"),
        "written_bytes": int(written or 0),
        "storage_provider": storage_provider,
        "content_origin": origin,
        "client_write": client_write,
    }
    return normalised


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


# --------------------------------------------------------------------------- provider protocols


class WorkspaceGateway(Protocol):
    mode: str
    enforced: bool

    def check_access(self, workspace_id: str, scope: Any) -> WorkspaceAccess: ...

    def register_reference(
        self, workspace_id: str, scope: Any, *, artifact_id: str, kind: str = "artifact",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]: ...


class WorkspaceWriteGateway(Protocol):
    """Amendment 01 surface Canvas uses for "export to workspace" (contract §A1.5)."""

    def write_file(self, workspace_id: str, scope: Any, request: Any) -> Any: ...


class UnavailableWorkspaceProvider:
    """Fail-closed provider used until the Workspace engine is available."""

    mode = "unavailable"
    enforced = True

    def __init__(self, reason: str = "workspace provider is not configured") -> None:
        self.reason = reason

    def check_access(self, workspace_id: str, scope: Any) -> WorkspaceAccess:
        raise CanvasError(
            CanvasErrorCode.WORKSPACE_UNAVAILABLE,
            self.reason,
            retryable=True,
            context={"workspace_id": workspace_id},
        )

    def register_reference(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise CanvasError(
            CanvasErrorCode.WORKSPACE_UNAVAILABLE, self.reason, retryable=True
        )

    def write_file(self, workspace_id: str, scope: Any, request: Any) -> Any:
        raise CanvasError(
            CanvasErrorCode.WORKSPACE_UNAVAILABLE,
            self.reason,
            retryable=True,
            context={"workspace_id": workspace_id, "operation": "write_file"},
        )


class LocalDevWorkspaceProvider:
    """Isolated development provider (never production, never an authorization claim).

    Amendment 01: every workspace record carries a lifecycle state, a storage provider and a
    read-only flag, and ``write_file`` implements the shared write contract in memory — including
    the ``client_write`` answer for a browser-owned root — so Canvas' closed / read-only /
    ``local_browser`` paths are exercised by tests without the storage engine.
    """

    mode = "dev"
    enforced = False
    DEV_WORKSPACE_ID = "ws_dev00000001"

    def __init__(self, production: bool = False, extra_workspaces: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        if production:
            raise RuntimeError(
                "LocalDevWorkspaceProvider must never run in production; "
                "configure AGCORE_CANVAS_WORKSPACE_PROVIDER=workspace"
            )
        self._workspaces: Dict[str, Dict[str, Any]] = {
            self.DEV_WORKSPACE_ID: self._record("Canvas Dev Workspace"),
        }
        for workspace_id, payload in (extra_workspaces or {}).items():
            self.add_workspace(
                workspace_id,
                name=str(payload.get("name") or workspace_id),
                level=str(payload.get("level") or "admin"),
                lifecycle_state=str(payload.get("lifecycle_state") or WorkspaceLifecycleState.OPEN.value),
                storage_provider=str(payload.get("storage_provider") or WorkspaceStorageProvider.SERVER_MANAGED.value),
                read_only=bool(payload.get("read_only") or False),
                client_handle_id=payload.get("client_handle_id"),
                display_path=str(payload.get("display_path") or ""),
            )
        self._references: Dict[str, list] = {}

    # -- record helpers -----------------------------------------------------------

    @staticmethod
    def _record(name: str, level: str = "admin") -> Dict[str, Any]:
        return {
            "name": name or "workspace",
            "level": level,
            "lifecycle_state": WorkspaceLifecycleState.OPEN.value,
            "storage_provider": WorkspaceStorageProvider.SERVER_MANAGED.value,
            "read_only": False,
            "client_handle_id": None,
            "display_path": "",
            "root_id": None,
            "allowed_root_id": None,
            "files": {},
        }

    def _ensure(self, workspace_id: str) -> Dict[str, Any]:
        return self._workspaces.setdefault(workspace_id, self._record(workspace_id))

    @staticmethod
    def _check_level(level: str) -> str:
        if level not in LEVEL_RANK:
            raise ValueError(f"unknown workspace level '{level}'")
        return level

    # -- setup (tests / dev preview only) -----------------------------------------

    def add_workspace(
        self,
        workspace_id: str,
        *,
        name: str = "",
        level: str = "admin",
        lifecycle_state: str = WorkspaceLifecycleState.OPEN.value,
        storage_provider: str = WorkspaceStorageProvider.SERVER_MANAGED.value,
        read_only: bool = False,
        client_handle_id: Optional[str] = None,
        display_path: str = "",
    ) -> None:
        self._check_level(level)
        if lifecycle_state not in LIFECYCLE_STATES:
            raise ValueError(f"unknown workspace lifecycle state '{lifecycle_state}'")
        if storage_provider not in STORAGE_PROVIDERS:
            raise ValueError(f"unknown workspace storage provider '{storage_provider}'")
        record = self._record(name or workspace_id, level)
        record.update(
            {
                "lifecycle_state": lifecycle_state,
                "storage_provider": storage_provider,
                "read_only": read_only,
                "client_handle_id": client_handle_id,
                "display_path": display_path,
            }
        )
        self._workspaces[workspace_id] = record

    def set_level(self, workspace_id: str, level: str) -> None:
        """Simulate a permission change (grant downgrade / revocation) in the dev preview."""
        self._check_level(level)
        self._ensure(workspace_id)["level"] = level

    def set_lifecycle_state(
        self,
        workspace_id: str,
        state: str,
        *,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Amendment 01 §A1.2: ``open`` | ``closed`` | ``deleted`` (rows kept, unlike the engine)."""

        if state not in LIFECYCLE_STATES:
            raise ValueError(f"unknown workspace lifecycle state '{state}'")
        record = self._ensure(workspace_id)
        record["lifecycle_state"] = state
        record["closed_reason"] = reason if state == WorkspaceLifecycleState.CLOSED.value else ""
        return dict(record)

    def set_read_only(self, workspace_id: str, read_only: bool = True) -> None:
        """Amendment 01 §A1.7: a root opened read-only refuses every write."""

        self._ensure(workspace_id)["read_only"] = bool(read_only)

    def set_storage_provider(
        self,
        workspace_id: str,
        storage_provider: str,
        *,
        client_handle_id: Optional[str] = None,
        display_path: Optional[str] = None,
    ) -> None:
        """Amendment 01 §A1.1: ``server_managed`` | ``server_mounted_folder`` | ``local_browser``."""

        if storage_provider not in STORAGE_PROVIDERS:
            raise ValueError(f"unknown workspace storage provider '{storage_provider}'")
        record = self._ensure(workspace_id)
        record["storage_provider"] = storage_provider
        record["client_handle_id"] = (
            client_handle_id
            if storage_provider == WorkspaceStorageProvider.LOCAL_BROWSER.value
            else None
        )
        if display_path is not None:
            record["display_path"] = display_path

    def seed_file(self, workspace_id: str, path: str, content: str = "") -> Dict[str, Any]:
        """Put a file in the dev root so a write can be tested against an existing hash."""

        relative = validate_relative_path(path)
        record = self._ensure(workspace_id)
        raw = content.encode("utf-8")
        entry = {
            "content": raw,
            "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "revision": 1,
            "mime_type": "text/plain",
        }
        record["files"][relative] = entry
        return {"path": relative, **{key: value for key, value in entry.items() if key != "content"}}

    def list_files(self, workspace_id: str) -> Dict[str, Dict[str, Any]]:
        """Files the dev root holds (a ``local_browser`` root never holds any)."""

        record = self._workspaces.get(workspace_id)
        if record is None:
            return {}
        return {
            path: {
                "content": entry["content"].decode("utf-8", "replace"),
                "content_hash": entry["content_hash"],
                "revision": entry["revision"],
            }
            for path, entry in record["files"].items()
        }

    # -- contract surface ---------------------------------------------------------

    def _access(self, workspace_id: str, record: Dict[str, Any]) -> WorkspaceAccess:
        storage_provider = record["storage_provider"]
        server_access = storage_provider != WorkspaceStorageProvider.LOCAL_BROWSER.value
        return WorkspaceAccess(
            workspace_id=workspace_id,
            level=str(record.get("level") or "admin"),
            reason="dev_adapter_unenforced",
            provider_mode=self.mode,
            enforced=False,
            exists=True,
            provider_mode_detail="Development adapter: access level is not an authorization decision.",
            lifecycle_state=str(record.get("lifecycle_state") or WorkspaceLifecycleState.OPEN.value),
            storage_provider=storage_provider,
            read_only=bool(record.get("read_only")),
            server_side_path_access=server_access,
            client_handle_id=record.get("client_handle_id"),
            root_display_path=str(record.get("display_path") or "") if server_access else "",
            root_id=record.get("root_id"),
            allowed_root_id=record.get("allowed_root_id"),
        )

    def check_access(self, workspace_id: str, scope: Any) -> WorkspaceAccess:
        record = self._workspaces.get(workspace_id)
        if record is None:
            return WorkspaceAccess(
                workspace_id=workspace_id,
                level="none",
                reason="dev_adapter_unknown_workspace",
                provider_mode=self.mode,
                enforced=False,
                exists=False,
            )
        return self._access(workspace_id, record)

    def register_reference(
        self, workspace_id: str, scope: Any, *, artifact_id: str, kind: str = "artifact",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        access = self.check_access(workspace_id, scope)
        if not access.can_write:
            raise CanvasError(
                CanvasErrorCode.WORKSPACE_ACCESS_DENIED,
                "workspace write access denied",
                context={
                    "workspace_id": workspace_id,
                    "level": access.level,
                    "required_level": "write",
                    "provider_mode": self.mode,
                },
            )
        reference = {
            "artifact_ref_id": f"arf_dev{uuid_suffix()}",
            "workspace_id": workspace_id,
            "artifact_id": artifact_id,
            "kind": kind,
            "metadata": dict(metadata or {}),
            "provider_mode": self.mode,
            "enforced": False,
        }
        self._references.setdefault(workspace_id, []).append(reference)
        return reference

    def list_references(self, workspace_id: str) -> list:
        return list(self._references.get(workspace_id, []))

    def write_file(self, workspace_id: str, scope: Any, request: Any) -> WorkspaceWriteResult:
        """In-memory implementation of the shared write contract (contract §A1.5)."""

        access = self.check_access(workspace_id, scope)
        if not access.exists or not access.can_write:
            raise CanvasError(
                CanvasErrorCode.WORKSPACE_ACCESS_DENIED,
                "workspace write access denied",
                context={
                    "workspace_id": workspace_id,
                    "level": access.level,
                    "required_level": "write",
                    "provider_mode": self.mode,
                },
            )
        enforce_workspace_state(access, required="write", operation="dev.write_file")
        relative = validate_relative_path(str(_request_field(request, "path", "")))
        record = self._workspaces[workspace_id]
        files: Dict[str, Dict[str, Any]] = record["files"]
        existing = files.get(relative)
        overwrite = bool(_request_field(request, "overwrite", True))
        if existing is not None and not overwrite:
            raise CanvasError(
                CanvasErrorCode.WORKSPACE_FILE_EXISTS,
                "a file already exists at this path and overwrite is false",
                context={"workspace_id": workspace_id, "path": relative},
            )
        expected = _request_field(request, "expected_content_hash", None)
        current_hash = existing["content_hash"] if existing else None
        if expected is not None and expected != current_hash:
            raise CanvasError(
                CanvasErrorCode.CONTENT_CONFLICT,
                "expected_content_hash no longer matches the workspace file",
                context={
                    "workspace_id": workspace_id,
                    "path": relative,
                    "expected_content_hash": expected,
                    "current_content_hash": current_hash,
                },
            )
        if access.storage_provider == WorkspaceStorageProvider.LOCAL_BROWSER.value:
            # The server has no path access: hand the write back to the browser handle.
            return WorkspaceWriteResult(
                workspace_id=workspace_id,
                path=relative,
                storage_provider=WorkspaceStorageProvider.LOCAL_BROWSER,
                content_origin=ContentOrigin.EXTERNAL,
                written_bytes=0,
                client_write=ClientWriteDirective(
                    client_handle_id=access.client_handle_id,
                    relative_path=relative,
                ),
            )

        payload = request_content_bytes(request)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        revision = int(existing["revision"]) + 1 if existing else 1
        mime_type = _request_field(request, "mime_type", None) or "text/markdown"
        files[relative] = {
            "content": payload,
            "content_hash": digest,
            "revision": revision,
            "mime_type": mime_type,
        }
        origin = (
            ContentOrigin.BLOB
            if access.storage_provider == WorkspaceStorageProvider.SERVER_MANAGED.value
            else ContentOrigin.EXTERNAL
        )
        return WorkspaceWriteResult(
            workspace_id=workspace_id,
            path=relative,
            file={
                "file_id": f"wf_dev{revision:04d}{len(files):04d}",
                "workspace_id": workspace_id,
                "name": relative.rsplit("/", 1)[-1],
                "path": "/" + relative,
                "relative_path": relative,
                "kind": "file",
                "mime_type": mime_type,
                "size_bytes": len(payload),
                "content_hash": digest,
                "revision": revision,
                "storage_provider": access.storage_provider,
                "content_origin": origin.value,
            },
            revision=revision,
            content_hash=digest,
            written_bytes=len(payload),
            storage_provider=WorkspaceStorageProvider(access.storage_provider),
            content_origin=origin,
        )


def uuid_suffix() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


class RuntimeWorkspaceProvider:
    """Binds the frozen R2-W provider implementation (``app/workspace/contracts.py``)."""

    mode = "workspace"
    enforced = True

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    @classmethod
    def try_build(cls) -> Optional["RuntimeWorkspaceProvider"]:
        """Return a bound provider when the Workspace branch is present, else ``None``."""
        try:  # pragma: no cover - depends on the other engineer's branch being merged
            from app.workspace.contracts import WorkspaceProvider  # noqa: F401
        except Exception:
            return None
        factory = getattr(_runtime_provider_factory, "build", None)
        if factory is None:  # pragma: no cover
            return None
        provider = factory()
        return cls(provider) if provider is not None else None

    def check_access(self, workspace_id: str, scope: Any) -> WorkspaceAccess:
        try:
            result = self._provider.check_workspace_access(workspace_id, scope)
        except Exception as exc:
            raise translate_workspace_error(
                exc, workspace_id=workspace_id, operation="check_workspace_access"
            ) from exc
        level = getattr(result, "level", "none")
        level = getattr(level, "value", level)
        level = str(level)
        access = WorkspaceAccess(
            workspace_id=getattr(result, "workspace_id", workspace_id),
            level=level,
            reason=str(getattr(result, "reason", "") or ""),
            provider_mode=self.mode,
            enforced=True,
            exists=level != "none",
        )
        if access.can_read:
            access = self._with_identity(access, workspace_id, scope)
        return access

    def _with_identity(self, access: WorkspaceAccess, workspace_id: str, scope: Any) -> WorkspaceAccess:
        """Amendment 01: fold lifecycle/root into the access answer when the provider offers it.

        A v1-only provider has no ``get_workspace``; Canvas then keeps the v1 behaviour exactly
        (an open, server-managed root) instead of failing the call.
        """

        getter = getattr(self._provider, "get_workspace", None)
        if not callable(getter):
            return access
        try:
            identity = getter(workspace_id, scope)
        except Exception:  # pragma: no cover - identity is enrichment, not authorization
            return access
        return apply_workspace_identity(access, identity)

    def register_reference(
        self, workspace_id: str, scope: Any, *, artifact_id: str, kind: str = "artifact",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            reference = self._provider.register_artifact_reference(
                workspace_id, scope,
                {"artifact_id": artifact_id, "kind": kind, "metadata": dict(metadata or {})},
            )
        except Exception as exc:
            raise CanvasError(
                CanvasErrorCode.WORKSPACE_UNAVAILABLE,
                "workspace artifact reference registration failed",
                retryable=True,
                context={"reason": exc.__class__.__name__},
            ) from exc
        if hasattr(reference, "model_dump"):
            payload = reference.model_dump()
        elif isinstance(reference, dict):
            payload = dict(reference)
        else:  # pragma: no cover
            payload = {"artifact_id": artifact_id}
        payload.setdefault("provider_mode", self.mode)
        payload.setdefault("enforced", True)
        return payload

    def write_file(self, workspace_id: str, scope: Any, request: Any) -> Any:
        """Amendment 01 §A1.5 — delegate the write to the workspace engine.

        Canvas builds the content and the path; the engine owns the file system, the atomic replace,
        the revision and the index job. A provider that only implements the v1 operations is refused
        with ``provider_mismatch`` (400) instead of being asked to guess.
        """

        writer = getattr(self._provider, "write_file", None)
        if not callable(writer):
            raise CanvasError(
                CanvasErrorCode.PROVIDER_MISMATCH,
                "the bound workspace provider does not implement the Amendment 01 file write contract",
                context={
                    "workspace_id": workspace_id,
                    "required_operation": "write_file",
                    "contract_revision": WORKSPACE_CONTRACT_REVISION,
                },
            )
        try:
            return writer(workspace_id, scope, request)
        except Exception as exc:
            raise translate_workspace_error(
                exc, workspace_id=workspace_id, operation="write_file"
            ) from exc


class _RuntimeProviderFactory:
    """Indirection so the Workspace owner can register its provider without Canvas importing it."""

    build = None


_runtime_provider_factory = _RuntimeProviderFactory()


def register_runtime_provider_factory(factory) -> None:
    """Integration hook: call with ``lambda: WorkspaceService(...)`` once the branch is merged."""
    _runtime_provider_factory.build = factory


def build_workspace_gateway(settings) -> WorkspaceGateway:
    mode = (settings.workspace_provider or "unavailable").lower()
    if mode == "workspace":
        provider = RuntimeWorkspaceProvider.try_build()
        if provider is not None:
            return provider
        return UnavailableWorkspaceProvider(
            "workspace provider requested but app.workspace is not available on this branch"
        )
    if mode == "dev":
        if settings.production or not settings.dev_preview or not settings.dev_adapters_enabled:
            return UnavailableWorkspaceProvider(
                "development workspace adapter is disabled (set AGCORE_CANVAS_DEV_PREVIEW=true)"
            )
        return LocalDevWorkspaceProvider(production=settings.production)
    return UnavailableWorkspaceProvider()


def default_dev_provider(data_dir: Optional[Path] = None) -> LocalDevWorkspaceProvider:
    """Convenience for tests/tools that need the dev provider explicitly."""
    return LocalDevWorkspaceProvider(production=False)


def workspace_contract_report() -> Dict[str, Any]:
    """Contract surface Canvas consumes, reported by ``/capabilities`` and the handover."""

    return {
        "version": WORKSPACE_CONTRACT_VERSION,
        "amendment": WORKSPACE_CONTRACT_AMENDMENT,
        "revision": WORKSPACE_CONTRACT_REVISION,
        "v1_operations": [
            "check_workspace_access",
            "get_workspace",
            "register_artifact_reference",
            "list_artifact_references",
        ],
        "a01_operations": ["write_file"],
        "lifecycle_states": list(LIFECYCLE_STATES),
        "storage_providers": list(STORAGE_PROVIDERS),
        "error_codes": {
            contract_code: canvas_code.value
            for contract_code, canvas_code in WORKSPACE_ERROR_CODES.items()
        },
        "path_gate": "validate_relative_path (invalid_path 400)",
        "export_to_workspace": "POST /api/v1/canvas/artifacts/{artifact_id}/export-to-workspace",
    }
