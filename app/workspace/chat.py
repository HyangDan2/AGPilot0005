"""Workspace scope for a chat request (WO-R2-W Amendment 02 §1, "Workspace Context").

A chat request may name one workspace. When it does, the runtime receives a bounded
`WorkspaceChatContext`:

    workspace_id, workspace_root, storage_provider, execution_scope,
    granted_capabilities, file_metadata, retrieval_availability

The context carries **metadata only**. No file body is ever inserted here; the model has to
ask for content through an agent tool (`workspace_read_file` / `workspace_retrieve`), which is
what keeps "전체 파일 무조건 삽입" out of the chat path.

When a request has no workspace (or `workspace.enabled=false`) `attach_workspace_scope`
returns the request byte-identical and `None`, so the existing chat path is untouched.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.workspace.contracts import (
    FileKind,
    IndexState,
    ScopeView,
    WorkspaceErrorCode,
    WorkspaceLifecycleState,
)
from app.workspace.manager import WorkspaceFailure, WorkspaceManager
from app.workspace.retrieval import WorkspaceRetriever
from app.workspace.tools import READ_TOOLS, WRITE_TOOLS, relative_path_of


class WorkspaceChatSelection(BaseModel):
    """`workspace` block a client may attach to a chat request."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    context_budget_tokens: Optional[int] = Field(default=None, ge=64, le=131072)
    #: Client-side narrowing only; the server never widens what the scope allows.
    granted_capabilities: Optional[Dict[str, Any]] = None


class WorkspaceChatEnvelope(BaseModel):
    """Pass-through view of a chat request body with an optional `workspace` overlay."""

    model_config = ConfigDict(extra="allow")

    workspace: Optional[WorkspaceChatSelection] = None


class ExecutionScopeView(BaseModel):
    """The identity the workspace engine will use for this turn (ids only)."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    owner_id: str
    service_id: str
    role: Optional[str] = None
    authenticated: bool = True
    auth_source: Optional[str] = None


class WorkspaceRootView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_id: Optional[str] = None
    display_path: str = ""
    server_side_path_access: bool = True
    zero_copy: bool = False
    read_only: bool = False
    lifecycle_state: str = WorkspaceLifecycleState.OPEN.value


class FileRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    kind: str
    name: str
    relative_path: str
    size_bytes: int = 0
    index_status: Optional[str] = None


class WorkspaceFileMetadata(BaseModel):
    """File *metadata* for the context block — never file contents."""

    model_config = ConfigDict(extra="forbid")

    files: int = 0
    folders: int = 0
    bytes: int = 0
    indexed: int = 0
    pending: int = 0
    failed: int = 0
    stale: int = 0
    top_level: List[FileRef] = Field(default_factory=list)
    truncated: bool = False


class RetrievalAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: bool = False
    vector: bool = False
    hybrid: bool = False
    embedding_provider: Optional[str] = None
    embedding_model: Optional[str] = None
    index_key: Optional[str] = None
    vectors: int = 0
    degraded: bool = False
    reasons: List[str] = Field(default_factory=list)


class ContextPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_contents_included: bool = False
    max_read_tokens: int = 2000
    note: str = "파일 본문은 컨텍스트에 넣지 않습니다. 모델이 필요하면 workspace_read_file/workspace_retrieve 도구로 요청합니다."


class WorkspaceChatContext(BaseModel):
    """Bounded workspace scope handed to a chat turn."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    workspace_name: str = ""
    workspace_root: WorkspaceRootView
    storage_provider: str
    execution_scope: ExecutionScopeView
    granted_capabilities: Dict[str, Any]
    file_metadata: WorkspaceFileMetadata
    retrieval_availability: RetrievalAvailability
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)

    def tool_context_args(self) -> Dict[str, Any]:
        """Arguments the runtime should pass to the tool adapter for this turn."""

        return {
            "workspace_id": self.workspace_id,
            "granted_capabilities": self.granted_capabilities,
            "context_budget": self.granted_capabilities.get("context_budget_tokens"),
        }


def build_execution_scope_view(scope: ScopeView) -> ExecutionScopeView:
    return ExecutionScopeView(
        tenant_id=str(getattr(scope, "tenant_id", "") or ""),
        owner_id=str(getattr(scope, "owner_id", "") or ""),
        service_id=str(getattr(scope, "service_id", "") or ""),
        role=getattr(scope, "role", None),
        authenticated=bool(getattr(scope, "authenticated", True)),
        auth_source=getattr(scope, "auth_source", None),
    )


def granted_capabilities(
    *,
    read_only: bool,
    client: Optional[Dict[str, Any]] = None,
    context_budget: Optional[int] = None,
    settings: Any = None,
) -> Dict[str, Any]:
    """Server-derived capability profile; a client request can only narrow it."""

    client = client or {}
    allowed = list(READ_TOOLS) + ([] if read_only else list(WRITE_TOOLS))
    requested = client.get("allowed_tools")
    if isinstance(requested, list):
        allowed = [name for name in allowed if name in set(requested)]
    budget = context_budget or int(getattr(settings, "context_budget_tokens", 4096) or 4096)
    if settings is not None:
        budget = min(int(budget), int(getattr(settings, "context_budget_tokens", budget)))
    return {
        "allowed_tools": allowed,
        "read_only": bool(read_only),
        "write": (not read_only) and bool(client.get("write", True)),
        "delete": (not read_only) and bool(client.get("delete", True)) and bool(client.get("write", True)),
        "context_budget_tokens": max(64, int(budget)),
        "approvals_required": []
        if read_only
        else ["workspace_create_file:overwrite", "workspace_move_file:overwrite", "workspace_delete_file"],
    }


def retrieval_availability(
    manager: WorkspaceManager, retriever: WorkspaceRetriever, workspace_id: str
) -> RetrievalAvailability:
    chunks = manager.store.count_chunks(workspace_id)
    embedder = getattr(retriever, "embedder", None)
    available = bool(embedder is not None and getattr(embedder, "available", False))
    index_key = getattr(embedder, "index_key", None) if embedder is not None else None
    vectors = manager.store.count_vectors(workspace_id, index_key) if (available and index_key) else 0
    reasons: List[str] = []
    if chunks == 0:
        reasons.append("no_indexed_chunks")
    if not available:
        reasons.append("embedding_unavailable")
    elif vectors == 0:
        reasons.append("vector_index_empty")
    keyword = chunks > 0
    vector = available and vectors > 0
    return RetrievalAvailability(
        keyword=keyword,
        vector=vector,
        hybrid=keyword and vector,
        embedding_provider=getattr(embedder, "provider_name", None) if embedder is not None else None,
        embedding_model=getattr(embedder, "model_id", None) if embedder is not None else None,
        index_key=index_key,
        vectors=vectors,
        degraded=chunks == 0 or not vector,
        reasons=reasons,
    )


def build_workspace_chat_context(
    manager: WorkspaceManager,
    retriever: WorkspaceRetriever,
    workspace_id: str,
    scope: ScopeView,
    *,
    selection: Optional[WorkspaceChatSelection] = None,
    settings: Any = None,
    top_level_limit: int = 40,
) -> WorkspaceChatContext:
    workspace = manager.get_workspace(workspace_id, scope)
    manager.ensure_open(workspace_id, scope)
    provider = workspace.root.storage_provider.value
    read_only = bool(workspace.root.read_only)

    identities = manager.list_files(workspace_id, scope, recursive=True)
    files = [item for item in identities if item.kind is FileKind.FILE]
    folders = [item for item in identities if item.kind is FileKind.FOLDER]
    statuses = [item.index_status for item in files if item.index_status is not None]
    metadata = WorkspaceFileMetadata(
        files=len(files),
        folders=len(folders),
        bytes=sum(item.size_bytes for item in files),
        indexed=sum(1 for status in statuses if status in {IndexState.READY}),
        pending=sum(1 for status in statuses if status in {IndexState.PENDING, IndexState.INDEXING}),
        failed=sum(1 for status in statuses if status is IndexState.FAILED),
        stale=sum(1 for status in statuses if status is IndexState.STALE),
        top_level=[
            FileRef(
                file_id=item.file_id,
                kind=item.kind.value,
                name=item.name,
                relative_path=relative_path_of(item),
                size_bytes=item.size_bytes,
                index_status=item.index_status.value if item.index_status else None,
            )
            for item in (folders + files)[:top_level_limit]
        ],
        truncated=len(folders) + len(files) > top_level_limit,
    )

    budget = selection.context_budget_tokens if selection is not None else None
    capabilities = granted_capabilities(
        read_only=read_only,
        client=selection.granted_capabilities if selection is not None else None,
        context_budget=budget,
        settings=settings,
    )
    return WorkspaceChatContext(
        workspace_id=workspace_id,
        workspace_name=workspace.name,
        workspace_root=WorkspaceRootView(
            root_id=workspace.root.root_id,
            display_path=workspace.root.display_path,
            server_side_path_access=workspace.root.server_side_path_access,
            zero_copy=workspace.root.zero_copy,
            read_only=read_only,
            lifecycle_state=workspace.lifecycle_state.value,
        ),
        storage_provider=provider,
        execution_scope=build_execution_scope_view(scope),
        granted_capabilities=capabilities,
        file_metadata=metadata,
        retrieval_availability=retrieval_availability(manager, retriever, workspace_id),
    )


def attach_workspace_scope(
    body: Dict[str, Any],
    manager: WorkspaceManager,
    retriever: WorkspaceRetriever,
    scope: ScopeView,
    *,
    settings: Any = None,
) -> tuple[Dict[str, Any], Optional[WorkspaceChatContext]]:
    """Overlay a workspace scope on a chat request body.

    No workspace selected → the same dict object is returned (the existing chat path keeps
    100% of its behaviour). A selected workspace adds `workspace_context`; it never injects
    file bodies and never rewrites the user's message.
    """

    envelope = WorkspaceChatEnvelope.model_validate(body or {})
    selection = envelope.workspace
    if selection is None or not selection.enabled:
        return body, None
    if not selection.workspace_id:
        raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "workspace_id is required")
    context = build_workspace_chat_context(
        manager, retriever, selection.workspace_id, scope, selection=selection, settings=settings
    )
    augmented = dict(body)
    augmented["workspace_context"] = context.model_dump(mode="json")
    return augmented, context
