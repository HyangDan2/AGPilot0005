"""REST API for the R2-W workspace engine (contract §3).

Mounted by the independent preview app (`app.workspace.preview_app`) today; the R1.3 runtime is
untouched. Every endpoint derives tenant/owner/service from the verified ExecutionScope.
"""
from __future__ import annotations

import functools
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.core.auth import ExecutionScope, resolve_caller_scope
from app.workspace.contracts import (
    ArtifactReferenceInput,
    ContextRequest,
    CitationValidationRequest,
    FileIdentity,
    FileUpdateRequest,
    FolderCreateRequest,
    FolderOpenRequest,
    LocalBrowserWorkspaceRequest,
    NewWorkspaceRequest,
    SearchMode,
    WorkspaceCloseRequest,
    WorkspaceCreateRequest,
    WorkspaceDisconnectRequest,
    WorkspaceErrorCode,
    WorkspaceUpdateRequest,
    WorkspaceWriteRequest,
)
from app.workspace.manager import WorkspaceFailure
from app.workspace.runtime import WorkspaceRuntime, get_runtime

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])

UPLOAD_CHUNK = 1024 * 1024
PREVIEW_CHAR_LIMIT = 256 * 1024


# ----------------------------------------------------------------------------- helpers
def get_scope(request: Request) -> ExecutionScope:
    try:
        scope = resolve_caller_scope(request)
    except Exception as exc:  # CredentialScopeViolation → fail closed
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": str(exc)[:200]}) from exc
    if not scope.authenticated:
        raise HTTPException(status_code=401, detail={"code": "forbidden", "message": "authentication_required"})
    return scope


def get_workspace_runtime() -> WorkspaceRuntime:
    return get_runtime()


def _http(failure: WorkspaceFailure) -> HTTPException:
    return HTTPException(status_code=failure.status_code, detail=failure.error.model_dump(mode="json"))


def guarded(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except WorkspaceFailure as failure:
            raise _http(failure) from failure

    return wrapper


def _runtime(runtime: WorkspaceRuntime, scope: ExecutionScope, workspace_id: str) -> None:
    # Access check + lifecycle: a closed workspace keeps its data but refuses reads.
    runtime.manager.ensure_open(workspace_id, scope)


def _ensure_index_ready(runtime: WorkspaceRuntime, workspace_id: str) -> None:
    if runtime.store.count_files(workspace_id, kind="file") > 0 and runtime.store.count_chunks(workspace_id) == 0:
        raise WorkspaceFailure(WorkspaceErrorCode.INDEX_NOT_READY, "workspace has no indexed content yet")


def _parse_multipart_upload(content_type: str, body: bytes) -> tuple[str, Optional[str], bytes, Dict[str, str]]:
    """Stdlib multipart/form-data parser avoiding external python-multipart dependency."""
    from email.parser import BytesParser
    from email.policy import default as email_default

    raw_msg = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    msg = BytesParser(policy=email_default).parsebytes(raw_msg)
    if not msg.is_multipart():
        raise HTTPException(status_code=400, detail={"code": "invalid_request", "message": "multipart/form-data required"})

    filename = "upload.bin"
    file_mime: Optional[str] = None
    file_bytes: Optional[bytes] = None
    fields: Dict[str, str] = {}
    for part in msg.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        part_filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if field_name == "file" or part_filename is not None:
            filename = part_filename or filename
            file_mime = part.get_content_type()
            file_bytes = payload
        elif field_name:
            fields[str(field_name)] = payload.decode("utf-8", errors="replace")
    if file_bytes is None:
        raise HTTPException(status_code=422, detail={"code": "invalid_request", "message": "missing multipart file field"})
    return filename, file_mime, file_bytes, fields


# ----------------------------------------------------------------------------- workspaces
@router.get("")
@guarded
def list_workspaces(
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    items = [item.model_dump(mode="json") for item in runtime.manager.list_workspaces(scope)]
    return {"items": items, "total": len(items)}


@router.post("", status_code=201)
@guarded
def create_workspace(
    body: WorkspaceCreateRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.create_workspace(scope, body.name, body.description).model_dump(mode="json")


# ----------------------------------------------------------------- Amendment 01 (§A1.2)
@router.get("/allowed-roots")
@guarded
def allowed_roots(
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    items = [item.model_dump(mode="json") for item in runtime.manager.allowed_roots(scope)]
    return {"items": items, "total": len(items)}


@router.post("/open-folder", status_code=201)
@guarded
def open_folder(
    body: FolderOpenRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.open_folder(
        scope, body.path, name=body.name, read_only=body.read_only, root_id=body.root_id
    ).model_dump(mode="json")


@router.post("/new", status_code=201)
@guarded
def new_workspace(
    body: NewWorkspaceRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.new_workspace(
        scope,
        body.name,
        description=body.description,
        parent_path=body.parent_path,
        root_id=body.root_id,
    ).model_dump(mode="json")


@router.post("/local-browser", status_code=201)
@guarded
def local_browser_workspace(
    body: LocalBrowserWorkspaceRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.create_local_browser_workspace(
        scope,
        body.name,
        client_handle_id=body.client_handle_id,
        entries=body.entries,
        display_path=body.display_path,
        description=body.description,
        read_only=body.read_only,
    ).model_dump(mode="json")


@router.get("/{workspace_id}")
@guarded
def get_workspace(
    workspace_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.get_workspace(workspace_id, scope).model_dump(mode="json")


@router.patch("/{workspace_id}")
@guarded
def update_workspace(
    workspace_id: str,
    body: WorkspaceUpdateRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.update_workspace(
        workspace_id,
        scope,
        name=body.name,
        description=body.description,
        expected_revision=body.expected_revision,
    ).model_dump(mode="json")


@router.delete("/{workspace_id}")
@guarded
def delete_workspace(
    workspace_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    """Disconnect: metadata only. Real files are never removed by this endpoint."""

    return runtime.manager.disconnect_workspace(workspace_id, scope, delete_source_files=False)


@router.post("/{workspace_id}/close")
@guarded
def close_workspace(
    workspace_id: str,
    body: Optional[WorkspaceCloseRequest] = Body(default=None),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    reason = (body.reason if body else "") or ""
    return runtime.manager.close_workspace(workspace_id, scope, reason).model_dump(mode="json")


@router.post("/{workspace_id}/reopen")
@guarded
def reopen_workspace(
    workspace_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.reopen_workspace(workspace_id, scope).model_dump(mode="json")


@router.post("/{workspace_id}/disconnect")
@guarded
def disconnect_workspace(
    workspace_id: str,
    body: Optional[WorkspaceDisconnectRequest] = Body(default=None),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.disconnect_workspace(
        workspace_id,
        scope,
        delete_source_files=bool(body.delete_source_files) if body else False,
        confirm=bool(body.confirm) if body else False,
    )


@router.post("/{workspace_id}/sync")
@guarded
def sync_workspace(
    workspace_id: str,
    body: Optional[Dict[str, Any]] = Body(default=None),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    full = bool((body or {}).get("full") or False)
    return runtime.manager.sync_workspace(workspace_id, scope, full=full).model_dump(mode="json")


@router.get("/{workspace_id}/watch")
@guarded
def watch_status(
    workspace_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.watch_status(workspace_id, scope).model_dump(mode="json")


@router.get("/{workspace_id}/root")
@guarded
def workspace_root(
    workspace_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.workspace_root_info(workspace_id, scope)


@router.put("/{workspace_id}/file")
@guarded
def write_file(
    workspace_id: str,
    body: WorkspaceWriteRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    """Canvas / Coding Agent common write contract (§A1.5)."""

    return runtime.manager.write_file(workspace_id, scope, body).model_dump(mode="json")


@router.get("/{workspace_id}/access")
@guarded
def workspace_access(
    workspace_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.check_workspace_access(workspace_id, scope).model_dump(mode="json")


@router.get("/{workspace_id}/usage")
@guarded
def workspace_usage(
    workspace_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.workspace_usage(workspace_id, scope)


# ----------------------------------------------------------------------------- files
@router.get("/{workspace_id}/files")
@guarded
def list_files(
    workspace_id: str,
    parent_id: Optional[str] = Query(default=None),
    recursive: bool = Query(default=False),
    include_deleted: bool = Query(default=False),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    items = runtime.manager.list_files(workspace_id, scope, parent_id, recursive, include_deleted)
    return {"items": [item.model_dump(mode="json") for item in items], "total": len(items)}


@router.post("/{workspace_id}/files", status_code=201)
async def upload_file(
    workspace_id: str,
    request: Request,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    from starlette.concurrency import run_in_threadpool

    runtime.manager.get_workspace(workspace_id, scope)  # access check before reading the body
    content_type = request.headers.get("content-type") or ""
    body = await request.body()
    filename, file_mime, file_bytes, fields = _parse_multipart_upload(content_type, body)
    parent_id = fields.get("parent_id") or None
    overwrite = str(fields.get("overwrite", "false")).strip().lower() in {"1", "true", "yes", "on"}
    raw_expected = fields.get("expected_revision")
    expected_revision: Optional[int] = None
    if raw_expected not in (None, ""):
        try:
            expected_revision = int(raw_expected)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_request", "message": "invalid expected_revision"}) from exc

    def _chunks():
        for start in range(0, len(file_bytes), UPLOAD_CHUNK):
            yield file_bytes[start : start + UPLOAD_CHUNK]

    def _upload() -> FileIdentity:
        return runtime.manager.upload_file(
            workspace_id,
            scope,
            filename,
            _chunks(),
            parent_id=parent_id,
            overwrite=overwrite,
            expected_revision=expected_revision,
            mime_type=file_mime,
        )

    try:
        identity = await run_in_threadpool(_upload)
    except WorkspaceFailure as failure:
        raise _http(failure) from failure
    return identity.model_dump(mode="json")


@router.post("/{workspace_id}/folders", status_code=201)
@guarded
def create_folder(
    workspace_id: str,
    body: FolderCreateRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.create_folder(workspace_id, scope, body.name, body.parent_id).model_dump(mode="json")


@router.get("/{workspace_id}/files/{file_id}")
@guarded
def file_metadata(
    workspace_id: str,
    file_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.get_file_metadata(workspace_id, file_id, scope).model_dump(mode="json")


@router.get("/{workspace_id}/files/{file_id}/content")
@guarded
def file_content(
    workspace_id: str,
    file_id: str,
    preview: Optional[str] = Query(default=None),
    revision: Optional[int] = Query(default=None),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
):
    metadata = runtime.manager.get_file_metadata(workspace_id, file_id, scope)
    if preview == "text":
        content = runtime.manager.read_file(workspace_id, file_id, scope, revision)
        text = content.text
        if text is None:
            raise WorkspaceFailure(WorkspaceErrorCode.UNSUPPORTED_MEDIA_TYPE, "binary file has no text preview")
        return PlainTextResponse(text[:PREVIEW_CHAR_LIMIT], media_type="text/plain; charset=utf-8")

    filename = metadata.name.replace('"', "")
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        runtime.manager.iter_file(workspace_id, file_id, scope, revision),
        media_type=metadata.mime_type or "application/octet-stream",
        headers=headers,
    )


@router.patch("/{workspace_id}/files/{file_id}")
@guarded
def update_file(
    workspace_id: str,
    file_id: str,
    body: FileUpdateRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.update_file(
        workspace_id,
        file_id,
        scope,
        name=body.name,
        parent_id=body.parent_id,
        expected_revision=body.expected_revision,
        move_parent="parent_id" in body.model_fields_set,
    ).model_dump(mode="json")


@router.delete("/{workspace_id}/files/{file_id}")
@guarded
def delete_file(
    workspace_id: str,
    file_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.delete_file(workspace_id, file_id, scope)


@router.get("/{workspace_id}/events")
@guarded
def list_events(
    workspace_id: str,
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.list_events(workspace_id, scope, since, limit)


# ----------------------------------------------------------------------------- index
@router.get("/{workspace_id}/index/status")
@guarded
def index_status(
    workspace_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.indexer.status(workspace_id, scope).model_dump(mode="json")


@router.post("/{workspace_id}/index/reindex", status_code=202)
@guarded
def reindex(
    workspace_id: str,
    body: Optional[Dict[str, Any]] = Body(default=None),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    file_id = (body or {}).get("file_id")
    jobs = runtime.indexer.reindex(workspace_id, scope, file_id)
    return {"jobs": [job.model_dump(mode="json") for job in jobs], "count": len(jobs)}


@router.get("/{workspace_id}/index/jobs")
@guarded
def index_jobs(
    workspace_id: str,
    state: Optional[str] = Query(default=None),
    file_id: Optional[str] = Query(default=None),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    jobs = runtime.indexer.list_jobs(workspace_id, scope, state=state, file_id=file_id)
    return {"items": [job.model_dump(mode="json") for job in jobs], "total": len(jobs)}


@router.post("/{workspace_id}/files/{file_id}/index/cancel")
@guarded
def cancel_index(
    workspace_id: str,
    file_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    job = runtime.indexer.cancel(workspace_id, scope, file_id)
    return job.model_dump(mode="json") if job else {"file_id": file_id, "state": "CANCELLED"}


# ----------------------------------------------------------------------------- retrieval
@router.get("/{workspace_id}/search")
@guarded
def search(
    workspace_id: str,
    q: str = Query(min_length=1, max_length=4000),
    mode: SearchMode = Query(default=SearchMode.HYBRID),
    top_k: int = Query(default=8, ge=1, le=50),
    file_id: Optional[str] = Query(default=None),
    mime_type: Optional[str] = Query(default=None),
    min_score: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    allow_degraded: bool = Query(default=True),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    _runtime(runtime, scope, workspace_id)
    _ensure_index_ready(runtime, workspace_id)
    response = runtime.retriever.search(
        workspace_id,
        q,
        mode=mode,
        top_k=top_k,
        file_ids=[file_id] if file_id else None,
        mime_types=[mime_type] if mime_type else None,
        min_score=min_score,
        allow_degraded=allow_degraded,
    )
    return response.model_dump(mode="json")


@router.post("/{workspace_id}/context")
@guarded
def build_context(
    workspace_id: str,
    body: ContextRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    _runtime(runtime, scope, workspace_id)
    _ensure_index_ready(runtime, workspace_id)
    response = runtime.assembler.build(
        workspace_id,
        body.query,
        top_k=body.top_k,
        token_budget=body.token_budget,
        file_ids=body.file_ids,
        mode=body.search_mode,
        allow_degraded=body.allow_degraded,
    )
    return response.model_dump(mode="json")


@router.post("/{workspace_id}/citations/validate")
@guarded
def validate_citations(
    workspace_id: str,
    body: CitationValidationRequest,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    _runtime(runtime, scope, workspace_id)
    return runtime.citations.validate(workspace_id, body.citation_ids).model_dump(mode="json")


@router.get("/{workspace_id}/citations/{citation_id}")
@guarded
def resolve_citation(
    workspace_id: str,
    citation_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    _runtime(runtime, scope, workspace_id)
    return runtime.citations.resolve(workspace_id, citation_id).model_dump(mode="json")


# ----------------------------------------------------------------------------- references
@router.post("/{workspace_id}/artifact-references", status_code=201)
@guarded
def add_artifact_reference(
    workspace_id: str,
    body: ArtifactReferenceInput,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.register_artifact_reference(workspace_id, scope, body).model_dump(mode="json")


@router.get("/{workspace_id}/artifact-references")
@guarded
def list_artifact_references(
    workspace_id: str,
    kind: Optional[str] = Query(default=None),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    items = runtime.manager.list_artifact_references(workspace_id, scope, kind)
    return {"items": [item.model_dump(mode="json") for item in items], "total": len(items)}


@router.delete("/{workspace_id}/artifact-references/{artifact_ref_id}")
@guarded
def delete_artifact_reference(
    workspace_id: str,
    artifact_ref_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    deleted = runtime.manager.delete_artifact_reference(workspace_id, scope, artifact_ref_id)
    return {"artifact_ref_id": artifact_ref_id, "deleted": deleted}


@router.post("/{workspace_id}/conversation-references", status_code=201)
@guarded
def add_conversation_reference(
    workspace_id: str,
    body: Dict[str, Any],
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    return runtime.manager.add_conversation_reference(workspace_id, scope, str(body.get("conversation_id") or ""))


@router.get("/{workspace_id}/conversation-references")
@guarded
def list_conversation_references(
    workspace_id: str,
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    items = runtime.manager.list_conversation_references(workspace_id, scope)
    return {"items": items, "total": len(items)}


# ----------------------------------------------------------------------------- Amendment 02
def _agent(runtime: WorkspaceRuntime):
    from app.workspace.agent import WorkspaceAgentAdapter

    if runtime.agent is None:
        runtime.agent = WorkspaceAgentAdapter(
            runtime.manager, runtime.retriever, runtime.assembler, runtime.tools, settings=runtime.settings
        )
    return runtime.agent


@router.post("/{workspace_id}/chat/context")
@guarded
def chat_context(
    workspace_id: str,
    body: Optional[Dict[str, Any]] = Body(default=None),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    """Workspace scope a chat request would receive (metadata only, no file bodies)."""

    from app.workspace.chat import WorkspaceChatSelection, build_workspace_chat_context

    payload = body or {}
    selection = WorkspaceChatSelection(workspace_id=workspace_id, **{k: v for k, v in payload.items() if k != "workspace_id"})
    context = build_workspace_chat_context(
        runtime.manager, runtime.retriever, workspace_id, scope, selection=selection, settings=runtime.settings
    )
    return context.model_dump(mode="json")


@router.post("/{workspace_id}/agent/turn")
@guarded
def agent_turn(
    workspace_id: str,
    body: Dict[str, Any],
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    """Agentic Execution Preview Adapter: one turn with real workspace tools."""

    from app.workspace.agent import AgentTurnRequest
    from app.workspace.chat import WorkspaceChatSelection, build_workspace_chat_context

    request = AgentTurnRequest.model_validate(body)
    selection = WorkspaceChatSelection(
        workspace_id=workspace_id,
        context_budget_tokens=request.context_budget_tokens,
        granted_capabilities=request.capability,
    )
    scope_context = build_workspace_chat_context(
        runtime.manager, runtime.retriever, workspace_id, scope, selection=selection, settings=runtime.settings
    )
    response = _agent(runtime).run(
        workspace_id,
        scope,
        request.message,
        conversation_id=request.conversation_id,
        capability=request.capability or scope_context.granted_capabilities,
        context_budget=scope_context.granted_capabilities.get("context_budget_tokens"),
        approvals=request.approvals,
    )
    payload = response.model_dump(mode="json")
    # The same seven-field scope a chat request receives, so the client can render "what the model saw".
    payload["workspace_context"] = scope_context.model_dump(mode="json")
    return payload
