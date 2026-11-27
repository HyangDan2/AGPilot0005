"""R2-W Workspace Shared Contract — machine-readable types (contract v1).

This module is the Python half of `docs/r2/WORKSPACE_CONTRACT.md`. It intentionally depends
only on pydantic so that the Canvas engine can import it without the AGCore runtime.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

WORKSPACE_CONTRACT_VERSION = "r2-w/v1"

ID_PATTERN = r"^(ws|wf|wc|wj|wref)_[a-z0-9]{4,32}$"


class WorkspaceStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


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


class CitationResolveResponse(BaseModel):
    available: bool
    citation: Optional[Citation] = None
    revision_status: RevisionStatus = RevisionStatus.CURRENT
    download_url: Optional[str] = None


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
