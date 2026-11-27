"""R2-W Workspace Shared Contract — machine-readable types (contract v1).

This module is the Python half of `docs/r2/WORKSPACE_CONTRACT.md`. It intentionally depends
only on pydantic so that the Canvas engine can import it without the AGCore runtime.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

WORKSPACE_CONTRACT_VERSION = "r2-w/v1"

# Amendment 01 (folder-based workspace) is additive on top of the frozen v1 surface.
# Existing v1 operations, fields and error codes keep their meaning; every Amendment 01 type
# below is optional for a v1 consumer.
WORKSPACE_CONTRACT_AMENDMENT = "A01"
WORKSPACE_CONTRACT_REVISION = "r2-w/v1+a01"

ID_PATTERN = r"^(ws|wf|wc|wj|wref)_[a-z0-9]{4,32}$"
_ROOT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,47}$"


class WorkspaceStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class WorkspaceLifecycleState(str, Enum):
    """Connection lifecycle of the workspace root (Amendment 01 §A1.2).

    `closed` keeps every row (metadata, revisions, chunks) so `reopen` is lossless; a closed
    workspace refuses reads, writes and indexing until it is reopened.
    """

    OPEN = "open"
    CLOSED = "closed"
    DELETED = "deleted"


class WorkspaceStorageProvider(str, Enum):
    """Where the workspace root physically lives (Amendment 01 §A1.1).

    * `server_managed`        — a folder the server created and owns. Without a folder root this
                                is the original v1 upload workspace (content-addressed blobs).
    * `server_mounted_folder` — an existing user folder on the server, mounted zero-copy.
    * `local_browser`         — a folder the user picked in the browser (File System Access API).
                                The server never receives a server-side path for it.
    """

    SERVER_MANAGED = "server_managed"
    SERVER_MOUNTED_FOLDER = "server_mounted_folder"
    LOCAL_BROWSER = "local_browser"


class ContentOrigin(str, Enum):
    """How a file's bytes are reached.

    `blob`     — a copy under the server storage root (v1 behavior).
    `external` — zero-copy: the bytes live in the workspace root itself and are read in place.
    """

    BLOB = "blob"
    EXTERNAL = "external"


class RootStatus(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    NOT_ALLOWED = "not_allowed"
    METADATA_OVERLAP = "metadata_overlap"


class AccessLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
    NONE = "none"


class FileKind(str, Enum):
    FILE = "file"
    FOLDER = "folder"


class IndexState(str, Enum):
    PENDING = "PENDING"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"
    STALE = "STALE"
    CANCELLED = "CANCELLED"


class SearchMode(str, Enum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    HYBRID = "hybrid"


class RevisionStatus(str, Enum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class ContextStatus(str, Enum):
    OK = "ok"
    INSUFFICIENT_DOCUMENTS = "insufficient_documents"
    EMPTY_WORKSPACE = "empty_workspace"


class WorkspaceErrorCode(str, Enum):
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    # Amendment 01 additions (additive; v1 consumers can ignore them)
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
    FILE_NOT_FOUND = "file_not_found"
    FILE_EXISTS = "file_exists"
    REVISION_CONFLICT = "revision_conflict"
    QUOTA_EXCEEDED = "quota_exceeded"
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    INVALID_PATH = "invalid_path"
    INVALID_REQUEST = "invalid_request"
    INDEX_NOT_READY = "index_not_ready"
    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    FORBIDDEN = "forbidden"


ERROR_STATUS: Dict[WorkspaceErrorCode, int] = {
    WorkspaceErrorCode.WORKSPACE_NOT_FOUND: 404,
    WorkspaceErrorCode.WORKSPACE_CLOSED: 409,
    WorkspaceErrorCode.READ_ONLY_WORKSPACE: 403,
    WorkspaceErrorCode.PATH_OUTSIDE_ALLOWED_ROOT: 403,
    WorkspaceErrorCode.SYMLINK_ESCAPE: 400,
    WorkspaceErrorCode.ROOT_UNAVAILABLE: 409,
    WorkspaceErrorCode.PROVIDER_MISMATCH: 400,
    WorkspaceErrorCode.CONTENT_CONFLICT: 409,
    WorkspaceErrorCode.SOURCE_DELETE_NOT_PERMITTED: 403,
    WorkspaceErrorCode.METADATA_OVERLAP: 400,
    WorkspaceErrorCode.WATCH_UNAVAILABLE: 409,
    WorkspaceErrorCode.FILE_NOT_FOUND: 404,
    WorkspaceErrorCode.FILE_EXISTS: 409,
    WorkspaceErrorCode.REVISION_CONFLICT: 409,
    WorkspaceErrorCode.QUOTA_EXCEEDED: 413,
    WorkspaceErrorCode.FILE_TOO_LARGE: 413,
    WorkspaceErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    WorkspaceErrorCode.INVALID_PATH: 400,
    WorkspaceErrorCode.INVALID_REQUEST: 400,
    WorkspaceErrorCode.INDEX_NOT_READY: 409,
    WorkspaceErrorCode.EMBEDDING_UNAVAILABLE: 503,
    WorkspaceErrorCode.FORBIDDEN: 403,
}


class WorkspaceError(BaseModel):
    """Structured error body carried in `HTTPException.detail`."""

    model_config = ConfigDict(extra="forbid")

    code: WorkspaceErrorCode
    message: str = ""
    retryable: bool = False
    context: Dict[str, Any] = Field(default_factory=dict)

    @property
    def http_status(self) -> int:
        return ERROR_STATUS.get(self.code, 400)


# --------------------------------------------------------------------------------------
# Identity payloads
# --------------------------------------------------------------------------------------


class WorkspaceRootInfo(BaseModel):
    """Root binding of a workspace (Amendment 01 §A1.1 / §A1.7)."""

    model_config = ConfigDict(extra="forbid")

    storage_provider: WorkspaceStorageProvider = WorkspaceStorageProvider.SERVER_MANAGED
    root_id: Optional[str] = None
    display_path: str = ""
    allowed_root_id: Optional[str] = None
    read_only: bool = False
    zero_copy: bool = False
    # False for `local_browser`: the server must never be assumed to reach the client path.
    server_side_path_access: bool = True
    client_handle_id: Optional[str] = None
    opened_at: Optional[str] = None
    status: RootStatus = RootStatus.AVAILABLE


class AllowedRootInfo(BaseModel):
    """One server-side root a user may open as a workspace (Amendment 01 §A1.9)."""

    model_config = ConfigDict(extra="forbid")

    root_id: str
    display_path: str
    storage_provider: WorkspaceStorageProvider = WorkspaceStorageProvider.SERVER_MOUNTED_FOLDER
    writable: bool = True
    scope: str = "owner"  # owner | tenant | global
    is_default: bool = False


class IndexStorageInfo(BaseModel):
    """Proof that metadata/index storage is outside the workspace root (§A1.10)."""

    model_config = ConfigDict(extra="forbid")

    separate_from_workspace_root: bool = True
    metadata_store: str = "sqlite"
    metadata_location_ref: str = "state://workspace.db"
    chunk_index_location_ref: str = "state://workspace.db#workspace_chunk"
    vector_index_location_ref: str = "state://workspace.db#workspace_chunk_vector"
    inside_workspace_root: bool = False


class WorkspaceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    tenant_id: str
    owner_id: str
    service_id: str
    name: str
    description: str = ""
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    revision: int = 0
    created_at: str
    updated_at: str
    # Amendment 01 (additive)
    lifecycle_state: WorkspaceLifecycleState = WorkspaceLifecycleState.OPEN
    root: WorkspaceRootInfo = Field(default_factory=WorkspaceRootInfo)
    closed_at: Optional[str] = None


class FileIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    workspace_id: str
    parent_id: Optional[str] = None
    kind: FileKind = FileKind.FILE
    name: str
    path: str = "/"
    mime_type: Optional[str] = None
    size_bytes: int = 0
    content_hash: Optional[str] = None
    revision: int = 0
    index_status: Optional[IndexState] = None
    created_at: str
    updated_at: str
    # Amendment 01 (additive): zero-copy files carry their real relative path in the root.
    storage_provider: WorkspaceStorageProvider = WorkspaceStorageProvider.SERVER_MANAGED
    content_origin: ContentOrigin = ContentOrigin.BLOB
    relative_path: Optional[str] = None
    synced_at: Optional[str] = None


class AccessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    level: AccessLevel
    reason: str = ""


class FileContent(BaseModel):
    """`read_file` result. `bytes` is a python `bytes` object, not JSON serializable."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    metadata: FileIdentity
    revision: int
    revision_status: RevisionStatus = RevisionStatus.CURRENT
    content_hash: str
    bytes: bytes
    text: Optional[str] = None


class ArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_ref_id: str
    workspace_id: str
    artifact_id: str
    kind: str = "artifact"
    file_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ArtifactReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    kind: str = "artifact"
    file_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------------------


class WorkspaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class WorkspaceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    expected_revision: Optional[int] = Field(default=None, ge=0)


class FolderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    parent_id: Optional[str] = None


class FileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    parent_id: Optional[str] = None
    expected_revision: Optional[int] = Field(default=None, ge=0)


class WorkspaceFileList(BaseModel):
    items: List[FileIdentity] = Field(default_factory=list)
    total: int = 0


class WorkspaceList(BaseModel):
    items: List[WorkspaceIdentity] = Field(default_factory=list)
    total: int = 0


# --------------------------------------------------------------------------------------
# Amendment 01 — folder-based workspace lifecycle / root / write contract
# --------------------------------------------------------------------------------------


class FolderOpenRequest(BaseModel):
    """`Open Folder`: mount an existing server folder as a workspace root (zero-copy)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    read_only: bool = False
    root_id: Optional[str] = Field(default=None, pattern=_ROOT_ID_PATTERN)


class NewWorkspaceRequest(BaseModel):
    """`New Workspace`: create a real folder under an allowed root and use it as the root."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    parent_path: Optional[str] = Field(default=None, max_length=4096)
    root_id: Optional[str] = Field(default=None, pattern=_ROOT_ID_PATTERN)


class LocalBrowserEntry(BaseModel):
    """One file the browser read from the picked directory (never a server-side path)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    kind: FileKind = FileKind.FILE
    size_bytes: int = Field(default=0, ge=0)
    content_hash: Optional[str] = None
    text: Optional[str] = None
    mime_type: Optional[str] = None


class LocalBrowserWorkspaceRequest(BaseModel):
    """Browser-picked folder (`local_browser` provider, File System Access API)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    client_handle_id: str = Field(min_length=1, max_length=128)
    display_path: str = Field(default="", max_length=512)
    read_only: bool = True
    entries: List[LocalBrowserEntry] = Field(default_factory=list)


class WorkspaceCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=200)


class WorkspaceDisconnectRequest(BaseModel):
    """Connection release. Deleting the real folder is a separate, explicit option."""

    model_config = ConfigDict(extra="forbid")

    delete_source_files: bool = False
    confirm: bool = False


class WorkspaceWriteRequest(BaseModel):
    """Canvas / Coding Agent common write contract (Amendment 01 §A1.5).

    One request per output file; the same request works against every provider. Paths are
    validated (`validate_relative_path`) and confined to the workspace root.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    content: Optional[str] = None
    content_base64: Optional[str] = None
    mime_type: Optional[str] = Field(default=None, max_length=200)
    expected_content_hash: Optional[str] = Field(default=None, max_length=80)
    create_parents: bool = True
    overwrite: bool = True


class ClientWriteDirective(BaseModel):
    """Returned when only the client can perform the write (`local_browser`)."""

    model_config = ConfigDict(extra="forbid")

    required: bool = True
    client_handle_id: Optional[str] = None
    relative_path: str
    reason: str = "local_browser_no_server_path_access"


class WorkspaceWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    path: str
    file: Optional[FileIdentity] = None
    revision: int = 0
    content_hash: Optional[str] = None
    written_bytes: int = 0
    storage_provider: WorkspaceStorageProvider = WorkspaceStorageProvider.SERVER_MANAGED
    content_origin: ContentOrigin = ContentOrigin.BLOB
    client_write: Optional[ClientWriteDirective] = None


class WorkspaceSyncResult(BaseModel):
    """Result of one watcher pass / manual rescan (Amendment 01 §A1.4)."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    full: bool = False
    scanned: int = 0
    created: int = 0
    modified: int = 0
    moved: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""


class WorkspaceWatchStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    watching: bool = False
    storage_provider: WorkspaceStorageProvider = WorkspaceStorageProvider.SERVER_MANAGED
    interval_seconds: float = 0.0
    last_scan_at: Optional[str] = None
    last_change_at: Optional[str] = None
    changes_total: int = 0
    last_error: Optional[str] = None


# --------------------------------------------------------------------------------------
# Index lifecycle
# --------------------------------------------------------------------------------------


class IndexJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    workspace_id: str
    file_id: str
    file_revision: int
    state: IndexState
    attempts: int = 0
    max_attempts: int = 3
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class EmbedderStatus(BaseModel):
    provider: str
    model_id: str
    dimension: int
    available: bool
    degraded_reason: Optional[str] = None


class IndexSummary(BaseModel):
    workspace_id: str
    files_total: int = 0
    ready: int = 0
    indexing: int = 0
    pending: int = 0
    failed: int = 0
    stale: int = 0
    cancelled: int = 0
    chunks_total: int = 0
    embedder: EmbedderStatus
    retrieval_modes: List[SearchMode] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Retrieval / context / citations
# --------------------------------------------------------------------------------------


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    file_id: str
    file_revision: int
    file_name: str
    page_number: Optional[int] = None
    section: Optional[str] = None
    excerpt: str
    score: float = 0.0
    keyword_score: Optional[float] = None
    vector_score: Optional[float] = None
    matched_by: List[str] = Field(default_factory=list)


class ProviderStatus(BaseModel):
    keyword: str = "ok"
    embedding: str = "ok"


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    query: str
    mode_requested: SearchMode
    mode_effective: SearchMode
    degraded: bool = False
    degradation_reason: Optional[str] = None
    provider_status: ProviderStatus = Field(default_factory=ProviderStatus)
    total_matches: int = 0
    results: List[SearchResult] = Field(default_factory=list)


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=8, ge=1, le=50)
    token_budget: Optional[int] = Field(default=None, ge=64, le=131072)
    file_ids: Optional[List[str]] = None
    search_mode: SearchMode = SearchMode.HYBRID
    allow_degraded: bool = True


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_id: str
    workspace_id: str
    file_id: str
    file_revision: int
    file_name: str
    chunk_id: str
    page_number: Optional[int] = None
    section: Optional[str] = None
    source_excerpt: str = ""
    revision_status: RevisionStatus = RevisionStatus.CURRENT


class ContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    query: str
    status: ContextStatus
    context_text: str = ""
    used_tokens: int = 0
    budget_tokens: int = 0
    truncated: bool = False
    sources: List[Citation] = Field(default_factory=list)
    retrieved: List[SearchResult] = Field(default_factory=list)
    degraded: bool = False
    injection_suspected: int = 0


class CitationResolveResponse(BaseModel):
    available: bool
    citation: Optional[Citation] = None
    revision_status: RevisionStatus = RevisionStatus.CURRENT
    download_url: Optional[str] = None
    reason: Optional[str] = None


class CitationValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_ids: List[str] = Field(default_factory=list, max_length=200)


class CitationValidationResponse(BaseModel):
    valid: List[str] = Field(default_factory=list)
    unknown: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Shared provider interface
# --------------------------------------------------------------------------------------


@runtime_checkable
class ScopeView(Protocol):
    """Minimal structural view of AGCore's `ExecutionScope` used by the contract."""

    tenant_id: str
    owner_id: str
    service_id: str


@runtime_checkable
class WorkspaceProvider(Protocol):
    """Logical shared provider interface (contract §2)."""

    def get_workspace(self, workspace_id: str, scope: ScopeView) -> WorkspaceIdentity: ...

    def check_workspace_access(self, workspace_id: str, scope: ScopeView) -> AccessResult: ...

    def list_workspace_files(
        self,
        workspace_id: str,
        scope: ScopeView,
        parent_id: Optional[str] = None,
        recursive: bool = False,
    ) -> Sequence[FileIdentity]: ...

    def get_file_metadata(self, workspace_id: str, file_id: str, scope: ScopeView) -> FileIdentity: ...

    def read_file(
        self,
        workspace_id: str,
        file_id: str,
        scope: ScopeView,
        revision: Optional[int] = None,
    ) -> FileContent: ...

    def create_file(
        self,
        workspace_id: str,
        scope: ScopeView,
        name: str,
        content: bytes,
        parent_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
        overwrite: bool = False,
    ) -> FileIdentity: ...

    def register_artifact_reference(
        self, workspace_id: str, scope: ScopeView, ref: ArtifactReferenceInput
    ) -> ArtifactReference: ...

    def list_artifact_references(
        self, workspace_id: str, scope: ScopeView, kind: Optional[str] = None
    ) -> Sequence[ArtifactReference]: ...


def provider_operations() -> Tuple[str, ...]:
    """Names frozen in the contract, used by verification tests."""

    return (
        "get_workspace",
        "check_workspace_access",
        "list_workspace_files",
        "get_file_metadata",
        "read_file",
        "create_file",
        "register_artifact_reference",
        "list_artifact_references",
    )


@runtime_checkable
class WorkspaceFolderProvider(Protocol):
    """Amendment 01 folder lifecycle + common write/scan surface (§A1.2, §A1.4, §A1.5)."""

    def open_folder(self, scope: ScopeView, path: str, **options: Any) -> WorkspaceIdentity: ...

    def new_workspace(self, scope: ScopeView, name: str, **options: Any) -> WorkspaceIdentity: ...

    def close_workspace(self, workspace_id: str, scope: ScopeView, reason: str = "") -> WorkspaceIdentity: ...

    def reopen_workspace(self, workspace_id: str, scope: ScopeView) -> WorkspaceIdentity: ...

    def disconnect_workspace(
        self, workspace_id: str, scope: ScopeView, delete_source_files: bool = False
    ) -> Dict[str, Any]: ...

    def write_file(
        self, workspace_id: str, scope: ScopeView, request: WorkspaceWriteRequest
    ) -> WorkspaceWriteResult: ...

    def sync_workspace(self, workspace_id: str, scope: ScopeView, full: bool = False) -> WorkspaceSyncResult: ...


def provider_operations_a01() -> Tuple[str, ...]:
    """Operations added by Amendment 01 (kept apart from the frozen v1 list)."""

    return (
        "open_folder",
        "new_workspace",
        "close_workspace",
        "reopen_workspace",
        "disconnect_workspace",
        "write_file",
        "sync_workspace",
    )


# --------------------------------------------------------------------------------------
# Relative path validation (shared by server, mock and clients)
# --------------------------------------------------------------------------------------

_PATH_MAX_SEGMENT = 255
_PATH_MAX_TOTAL = 1024
_FORBIDDEN_SEGMENTS = {"", ".", ".."}


class InvalidRelativePath(ValueError):
    """Raised by `validate_relative_path`; mapped to `invalid_path` by the API."""


def validate_relative_path(path: str, *, allow_empty: bool = False) -> str:
    """Normalize and validate a workspace-relative path.

    Rejects absolute paths, Windows drive/UNC prefixes, backslashes, control characters, NUL,
    `.`/`..` segments and over-long segments. Returns the normalized POSIX form without a
    leading or trailing slash. This is the single path gate shared by Canvas, the Coding Agent
    and the workspace API.
    """

    raw = path or ""
    if not isinstance(raw, str):
        raise InvalidRelativePath("path must be a string")
    if raw.strip() == "":
        if allow_empty:
            return ""
        raise InvalidRelativePath("empty path")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise InvalidRelativePath("control characters rejected")
    if "\\" in raw:
        raise InvalidRelativePath("backslash rejected")
    if raw.startswith("/") or raw.startswith("~"):
        raise InvalidRelativePath("absolute path rejected")
    if len(raw) > _PATH_MAX_TOTAL:
        raise InvalidRelativePath("path too long")
    if len(raw) >= 2 and raw[1] == ":":
        raise InvalidRelativePath("drive letter rejected")
    parts = [part for part in raw.split("/")]
    out: List[str] = []
    for part in parts:
        if part in _FORBIDDEN_SEGMENTS:
            raise InvalidRelativePath(f"invalid path segment: {part!r}")
        if len(part) > _PATH_MAX_SEGMENT:
            raise InvalidRelativePath("path segment too long")
        out.append(part)
    return "/".join(out)
