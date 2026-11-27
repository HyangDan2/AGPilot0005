"""REST API for the R2-W workspace engine (contract §3).

Mounted by the independent preview app (`app.workspace.preview_app`) today; the R1.3 runtime is
untouched. Every endpoint derives tenant/owner/service from the verified ExecutionScope.
"""
from __future__ import annotations

import functools
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.core.auth import ExecutionScope, resolve_caller_scope
from app.workspace.contracts import (
    ArtifactReferenceInput,
    ContextRequest,
    CitationValidationRequest,
    FileIdentity,
    FileUpdateRequest,
    FolderCreateRequest,
    SearchMode,
    WorkspaceCreateRequest,
    WorkspaceErrorCode,
    WorkspaceUpdateRequest,
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
    runtime.manager.get_workspace(workspace_id, scope)


def _ensure_index_ready(runtime: WorkspaceRuntime, workspace_id: str) -> None:
    if runtime.store.count_files(workspace_id, kind="file") > 0 and runtime.store.count_chunks(workspace_id) == 0:
        raise WorkspaceFailure(WorkspaceErrorCode.INDEX_NOT_READY, "workspace has no indexed content yet")


def _iter_upload(upload: UploadFile):
    while True:
        chunk = upload.file.read(UPLOAD_CHUNK)
        if not chunk:
            break
        yield chunk


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
    return runtime.manager.delete_workspace(workspace_id, scope)


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
    file: UploadFile = File(...),
    parent_id: Optional[str] = Form(default=None),
    overwrite: bool = Form(default=False),
    expected_revision: Optional[int] = Form(default=None),
    scope: ExecutionScope = Depends(get_scope),
    runtime: WorkspaceRuntime = Depends(get_workspace_runtime),
) -> Dict[str, Any]:
    from starlette.concurrency import run_in_threadpool

    runtime.manager.get_workspace(workspace_id, scope)  # access check before reading the body

    def _upload() -> FileIdentity:
        return runtime.manager.upload_file(
            workspace_id,
            scope,
            file.filename or "upload.bin",
            _iter_upload(file),
            parent_id=parent_id or None,
            overwrite=bool(overwrite),
            expected_revision=expected_revision,
            mime_type=file.content_type,
        )

    try:
        identity = await run_in_threadpool(_upload)
    except WorkspaceFailure as failure:
        raise _http(failure) from failure
    finally:
        await file.close()
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
