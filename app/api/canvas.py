"""Canvas artifact REST API (R2).

Base: ``/api/v1/canvas``. Every endpoint requires a verified ``ExecutionScope``; identity is never
read from the body. Errors use the structured envelope shared with the R2-W workspace contract:

    {"detail": {"code": "...", "message": "...", "retryable": false, "context": {...}}}
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.canvas.approval import build_user_approval, current_user_approval
from app.canvas.config import load_canvas_settings
from app.canvas.errors import CanvasError
from app.canvas.events import GenerationRequest
from app.canvas.models import PatchOperation, PatchRequest
from app.canvas.offsets import OffsetUnit, Selection
from app.canvas.service import CanvasService, get_canvas_service
from app.canvas.tools import manifest as tools_manifest
from app.core.auth import ExecutionScope, require_authenticated_caller

def require_canvas_enabled() -> None:
    """WO-C17 / C-FIX-05: the feature stays off until an operator enables it explicitly.

    Merging this branch into an operating service therefore exposes no working Canvas endpoint:
    ``AGCORE_CANVAS_ENABLED`` defaults to false and every route answers ``503 canvas_disabled``.
    """
    settings = load_canvas_settings()
    if not settings.enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "canvas_disabled",
                "message": "canvas engine is disabled (set AGCORE_CANVAS_ENABLED=true to activate it)",
                "retryable": False,
                "context": {
                    "feature_flag": "AGCORE_CANVAS_ENABLED",
                    "feature_flag_default": False,
                    "dev_preview_flag": "AGCORE_CANVAS_DEV_PREVIEW",
                },
            },
        )


router = APIRouter(
    prefix="/canvas",
    tags=["Canvas Artifacts"],
    dependencies=[Depends(require_authenticated_caller), Depends(require_canvas_enabled)],
)


# --------------------------------------------------------------------------- helpers


def _service(request: Request) -> CanvasService:
    service = getattr(request.app.state, "canvas", None)
    return service or get_canvas_service()


def _scope(request: Request) -> ExecutionScope:
    return require_authenticated_caller(request)


def _http(exc: CanvasError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.to_detail())


# --------------------------------------------------------------------------- request models


class ArtifactCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    artifact_type: str = Field(default="markdown")
    content: str = Field(default="")
    language: Optional[str] = None
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None
    creation_source: Literal["user", "ai", "import", "tool"] = "user"
    change_reason: Optional[str] = Field(default=None, max_length=500)
    idempotency_key: Optional[str] = Field(default=None, max_length=120)


class ArtifactRenameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)


class PatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1)
    operation: PatchOperation
    selection: Optional[Selection] = None
    target_range: Optional[Selection] = None
    expected_original_text: Optional[str] = None
    replacement_text: Optional[str] = None
    change_reason: Optional[str] = Field(default=None, max_length=500)
    offset_unit: Optional[OffsetUnit] = None
    actor: Literal["user", "ai", "import", "system"] = "user"


class LinkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: Optional[str] = None
    conversation_id: Optional[str] = None


class ExportToWorkspaceBody(BaseModel):
    """Amendment 01 (§A1.5): write this artifact (or one revision) into a workspace root.

    ``path`` is a workspace-relative path and passes the shared ``validate_relative_path`` gate;
    when omitted the export filename derived from the artifact title is used.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1, max_length=64)
    path: Optional[str] = Field(default=None, max_length=1024)
    revision: Optional[int] = Field(default=None, ge=1)
    format: Optional[str] = Field(default=None, max_length=32)
    expected_content_hash: Optional[str] = Field(default=None, max_length=80)
    create_parents: bool = True
    overwrite: bool = True
    mime_type: Optional[str] = Field(default=None, max_length=200)


class SaveToWorkspaceBody(BaseModel):
    """WO-C18-04 *Save to Workspace*: export + record the workspace file link.

    ``workspace_id`` / ``path`` may be omitted once the artifact is linked (its own workspace and
    relative path are used); ``expected_content_hash`` defaults to the hash Canvas last reconciled
    with, so an external edit is answered with ``409 content_conflict`` instead of an overwrite.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: Optional[str] = Field(default=None, max_length=64)
    path: Optional[str] = Field(default=None, max_length=1024)
    revision: Optional[int] = Field(default=None, ge=1)
    format: Optional[str] = Field(default=None, max_length=32)
    expected_content_hash: Optional[str] = Field(default=None, max_length=80)
    create_parents: bool = True
    overwrite: bool = True
    mime_type: Optional[str] = Field(default=None, max_length=200)


class LoadFromWorkspaceBody(BaseModel):
    """WO-C18-04 *Load from Workspace*: open an existing workspace file as a Canvas session."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1, max_length=64)
    workspace_file_id: Optional[str] = Field(default=None, max_length=64)
    path: Optional[str] = Field(default=None, max_length=1024)
    title: Optional[str] = Field(default=None, max_length=200)
    artifact_type: Optional[str] = Field(default=None, max_length=32)
    language: Optional[str] = Field(default=None, max_length=32)
    conversation_id: Optional[str] = Field(default=None, max_length=64)
    revision: Optional[int] = Field(default=None, ge=1)
    force: bool = False


class EditProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=4000)
    base_revision: Optional[int] = Field(default=None, ge=1)
    selection: Optional[Selection] = None
    offset_unit: Optional[OffsetUnit] = None
    instruction_kind: str = "edit_selection"
    budget_tokens: Optional[int] = Field(default=None, ge=128, le=32000)
    scripted_replacement: Optional[str] = None
    dev_rule: Optional[str] = None
    change_reason: Optional[str] = Field(default=None, max_length=500)


class ApplyProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_reason: Optional[str] = Field(default=None, max_length=500)
    # WO-C17 / C-FIX-06: the user's approval of this proposal, carried by the interactive UI.
    user_approved: bool = False
    approval_reference: Optional[str] = Field(default=None, max_length=120)


class RejectProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- introspection


@router.get("/capabilities")
async def capabilities(request: Request) -> Dict[str, Any]:
    try:
        return _service(request).capabilities()
    except CanvasError as exc:
        raise _http(exc) from exc


@router.get("/types")
async def artifact_types(request: Request) -> Dict[str, Any]:
    try:
        return _service(request).types_catalogue()
    except CanvasError as exc:
        raise _http(exc) from exc


@router.get("/tools/manifest")
async def canvas_tool_manifest() -> Dict[str, Any]:
    return tools_manifest()


# --------------------------------------------------------------------------- artifacts


@router.post("/artifacts", status_code=201)
async def create_artifact(body: ArtifactCreateBody, request: Request) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        artifact, revision, duplicate, meta = _service(request).manager.create(
            scope,
            title=body.title,
            artifact_type=body.artifact_type,
            content=body.content,
            language=body.language,
            conversation_id=body.conversation_id,
            workspace_id=body.workspace_id,
            creation_source=body.creation_source,
            change_reason=body.change_reason,
            actor=body.creation_source if body.creation_source in ("user", "ai", "import") else "user",
            actor_id=scope.owner_id,
            idempotency_key=body.idempotency_key,
        )
        return {
            "artifact": artifact.model_dump(),
            "revision": revision.model_dump(),
            "duplicate": duplicate,
            **meta,
        }
    except CanvasError as exc:
        raise _http(exc) from exc


@router.get("/artifacts")
async def list_artifacts(
    request: Request,
    conversation_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    artifact_type: Optional[str] = None,
    limit: Optional[int] = Query(default=None, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        page = _service(request).manager.list(
            scope,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            artifact_type=artifact_type,
            limit=limit,
            offset=offset,
        )
        return page.model_dump()
    except CanvasError as exc:
        raise _http(exc) from exc


@router.get("/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str, request: Request, revision: Optional[int] = Query(default=None, ge=1)
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).manager.get(scope, artifact_id, revision=revision)
    except CanvasError as exc:
        raise _http(exc) from exc


@router.patch("/artifacts/{artifact_id}")
async def rename_artifact(artifact_id: str, body: ArtifactRenameBody, request: Request) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        artifact = _service(request).manager.rename(scope, artifact_id, body.title)
        return {"artifact": artifact.model_dump()}
    except CanvasError as exc:
        raise _http(exc) from exc


@router.delete("/artifacts/{artifact_id}")
async def delete_artifact(
    artifact_id: str, request: Request, purge: bool = Query(default=False)
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).manager.delete(scope, artifact_id, purge=purge)
    except CanvasError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- patches / diff


@router.post("/artifacts/{artifact_id}/patches")
async def apply_patch(artifact_id: str, body: PatchBody, request: Request) -> Dict[str, Any]:
    scope = _scope(request)
    payload = body.model_dump()
    payload["artifact_id"] = artifact_id
    try:
        patch = PatchRequest(**payload)
        outcome = _service(request).patches.apply(scope, patch, actor_id=scope.owner_id)
        return {
            "artifact": outcome.artifact.model_dump(),
            "revision": outcome.revision.model_dump(),
            "changed": outcome.changed,
            "operation": outcome.operation,
            "applied_range_utf16": outcome.applied_range_utf16,
            "content_metrics": outcome.content_metrics,
        }
    except CanvasError as exc:
        raise _http(exc) from exc


@router.post("/artifacts/{artifact_id}/patches/preview")
async def preview_patch(artifact_id: str, body: PatchBody, request: Request) -> Dict[str, Any]:
    scope = _scope(request)
    payload = body.model_dump()
    payload["artifact_id"] = artifact_id
    try:
        patch = PatchRequest(**payload)
        return _service(request).patches.preview(scope, patch)
    except CanvasError as exc:
        raise _http(exc) from exc


@router.get("/artifacts/{artifact_id}/diff")
async def diff_revisions(
    artifact_id: str,
    request: Request,
    from_revision: Optional[int] = Query(default=None, ge=1),
    to_revision: Optional[int] = Query(default=None, ge=1),
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).revisions.diff(
            scope, artifact_id, from_revision=from_revision, to_revision=to_revision
        )
    except CanvasError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- revisions


@router.get("/artifacts/{artifact_id}/revisions")
async def list_revisions(
    artifact_id: str,
    request: Request,
    limit: Optional[int] = Query(default=None, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).revisions.history(scope, artifact_id, limit=limit, offset=offset).model_dump()
    except CanvasError as exc:
        raise _http(exc) from exc


@router.get("/artifacts/{artifact_id}/revisions/{revision}")
async def get_revision(artifact_id: str, revision: int, request: Request) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).revisions.get(scope, artifact_id, revision)
    except CanvasError as exc:
        raise _http(exc) from exc


@router.post("/artifacts/{artifact_id}/revisions/{revision}/restore")
async def restore_revision(
    artifact_id: str,
    revision: int,
    request: Request,
    body: Optional[ApplyProposalBody] = None,
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        outcome = _service(request).revisions.restore(
            scope,
            artifact_id,
            revision,
            change_reason=(body.change_reason if body else None),
            actor_id=scope.owner_id,
        )
        return outcome.model_dump()
    except CanvasError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- links


@router.post("/artifacts/{artifact_id}/links")
async def link_artifact(artifact_id: str, body: LinkBody, request: Request) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).manager.link(
            scope, artifact_id, workspace_id=body.workspace_id, conversation_id=body.conversation_id
        )
    except CanvasError as exc:
        raise _http(exc) from exc


@router.delete("/artifacts/{artifact_id}/links/{kind}")
async def unlink_artifact(artifact_id: str, kind: str, request: Request) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        artifact = _service(request).manager.unlink(scope, artifact_id, kind)
        return {"artifact": artifact.model_dump()}
    except CanvasError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- export / preview


@router.get("/artifacts/{artifact_id}/export")
async def export_artifact(
    artifact_id: str,
    request: Request,
    revision: Optional[int] = Query(default=None, ge=1),
    format: Optional[str] = Query(default=None),
) -> Response:
    scope = _scope(request)
    try:
        result = _service(request).manager.export(scope, artifact_id, revision=revision, format=format)
    except CanvasError as exc:
        raise _http(exc) from exc
    headers = {
        "Content-Disposition": result.content_disposition(),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "X-Canvas-Revision": str(result.revision),
        "X-Canvas-Format": result.format,
    }
    if result.warnings:
        headers["X-Canvas-Export-Warnings"] = ",".join(str(w.get("code")) for w in result.warnings)
    return Response(content=result.content, media_type=result.media_type, headers=headers)


@router.get("/artifacts/{artifact_id}/preview")
async def preview_artifact(
    artifact_id: str, request: Request, revision: Optional[int] = Query(default=None, ge=1)
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).manager.preview(scope, artifact_id, revision=revision)
    except CanvasError as exc:
        raise _http(exc) from exc


@router.post("/artifacts/{artifact_id}/export-to-workspace")
async def export_artifact_to_workspace(
    artifact_id: str, body: ExportToWorkspaceBody, request: Request
) -> Dict[str, Any]:
    """Export to Workspace (R2-W Amendment 01, `r2-w/v1+a01`).

    Writes the artifact (or ``revision``) into ``workspace_id`` through the shared
    ``PUT /workspaces/{id}/file`` contract: ``path``, ``content``, ``expected_content_hash``,
    ``create_parents``, ``overwrite``. The workspace engine performs the write; for a
    ``local_browser`` root the response carries a ``client_write`` directive instead and the
    browser writes through its own file handle.
    """
    scope = _scope(request)
    try:
        return _service(request).manager.export_to_workspace(
            scope,
            artifact_id,
            workspace_id=body.workspace_id,
            path=body.path,
            revision=body.revision,
            format=body.format,
            expected_content_hash=body.expected_content_hash,
            create_parents=body.create_parents,
            overwrite=body.overwrite,
            mime_type=body.mime_type,
        )
    except CanvasError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- workspace save / load


@router.post("/artifacts/from-workspace", status_code=201)
async def load_artifact_from_workspace(body: LoadFromWorkspaceBody, request: Request) -> Dict[str, Any]:
    """Open an **existing** workspace file as a Canvas editing session (WO-C18-04).

    Reads through ``get_file_metadata`` / ``list_workspace_files`` / ``read_file`` and creates (or
    reuses) a workspace-linked artifact. The workspace file stays the source of truth: Canvas keeps
    an edit buffer and a ``WorkspaceFileLink``, never a second authoritative copy.
    """
    scope = _scope(request)
    try:
        return _service(request).manager.load_from_workspace(
            scope,
            workspace_id=body.workspace_id,
            workspace_file_id=body.workspace_file_id,
            path=body.path,
            title=body.title,
            artifact_type=body.artifact_type,
            language=body.language,
            conversation_id=body.conversation_id,
            revision=body.revision,
            force=body.force,
        )
    except CanvasError as exc:
        raise _http(exc) from exc


@router.post("/artifacts/{artifact_id}/save-to-workspace")
async def save_artifact_to_workspace(
    artifact_id: str, body: SaveToWorkspaceBody, request: Request
) -> Dict[str, Any]:
    """*Save to Workspace*: write the artifact and record the workspace file link (WO-C18-04).

    Same bytes and same write contract as ``export-to-workspace``; the difference is the save
    state (``workspace_file_id``, ``relative_path``, ``last_saved_content_hash``) that makes the
    next save refuse a silently overwriting write with ``409 content_conflict``.
    """
    scope = _scope(request)
    try:
        return _service(request).manager.save_to_workspace(
            scope,
            artifact_id,
            workspace_id=body.workspace_id,
            path=body.path,
            revision=body.revision,
            format=body.format,
            expected_content_hash=body.expected_content_hash,
            create_parents=body.create_parents,
            overwrite=body.overwrite,
            mime_type=body.mime_type,
        )
    except CanvasError as exc:
        raise _http(exc) from exc


@router.get("/artifacts/{artifact_id}/workspace-link")
async def artifact_workspace_link(artifact_id: str, request: Request) -> Dict[str, Any]:
    """The saved link state of an artifact (``workspace_link: null`` when it was never saved)."""
    scope = _scope(request)
    try:
        return _service(request).manager.workspace_link(scope, artifact_id)
    except CanvasError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- AI edit proposals


@router.post("/artifacts/{artifact_id}/edit-proposals", status_code=201)
async def create_edit_proposal(artifact_id: str, body: EditProposalBody, request: Request) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).proposals.create(
            scope,
            artifact_id,
            instruction=body.instruction,
            base_revision=body.base_revision,
            selection=body.selection,
            offset_unit=body.offset_unit,
            instruction_kind=body.instruction_kind,
            budget_tokens=body.budget_tokens,
            scripted_replacement=body.scripted_replacement,
            dev_rule=body.dev_rule,
            change_reason=body.change_reason,
        )
    except CanvasError as exc:
        raise _http(exc) from exc


@router.get("/artifacts/{artifact_id}/edit-proposals")
async def list_edit_proposals(
    artifact_id: str,
    request: Request,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).proposals.list(
            scope, artifact_id, status=status, limit=limit, offset=offset
        )
    except CanvasError as exc:
        raise _http(exc) from exc


@router.get("/edit-proposals/{proposal_id}")
async def get_edit_proposal(proposal_id: str, request: Request) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).proposals.get(scope, proposal_id)
    except CanvasError as exc:
        raise _http(exc) from exc


@router.post("/edit-proposals/{proposal_id}/apply")
async def apply_edit_proposal(
    proposal_id: str,
    request: Request,
    body: Optional[ApplyProposalBody] = None,
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        approval = build_user_approval(
            scope,
            proposal_id=proposal_id,
            user_approved=bool(body.user_approved) if body else False,
            approval_reference=(body.approval_reference if body else None),
        )
        token = current_user_approval.set(approval)
        try:
            return _service(request).proposals.apply(
                scope,
                proposal_id,
                change_reason=(body.change_reason if body else None),
                actor_id=scope.owner_id,
                approval=approval,
            )
        finally:
            current_user_approval.reset(token)
    except CanvasError as exc:
        raise _http(exc) from exc


@router.post("/edit-proposals/{proposal_id}/reject")
async def reject_edit_proposal(
    proposal_id: str,
    request: Request,
    body: Optional[RejectProposalBody] = None,
) -> Dict[str, Any]:
    scope = _scope(request)
    try:
        return _service(request).proposals.reject(scope, proposal_id, reason=(body.reason if body else None))
    except CanvasError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- generation (dev contract)


@router.post("/generations")
async def generate_artifact(
    body: GenerationRequest,
    request: Request,
    stream: bool = Query(default=False),
):
    scope = _scope(request)
    service = _service(request)
    try:
        service.generation.ensure_dev_preview()
        if stream:
            return StreamingResponse(
                service.generation.stream(scope, body),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Vary": "Accept"},
            )
        return service.generation.generate(scope, body)
    except CanvasError as exc:
        raise _http(exc) from exc


def _metrics_for(artifact, revision) -> Dict[str, int]:
    """Kept for callers that still ask for the current content metrics."""
    from app.canvas.offsets import content_metrics

    try:
        _artifact, _revision, content = get_canvas_service().storage.read_current(artifact.artifact_id)
    except Exception:
        return {}
    return content_metrics(content)
