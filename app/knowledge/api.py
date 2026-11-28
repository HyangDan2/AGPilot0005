"""`dk/v1` knowledge API exposed by AGCore (additive, feature-flagged).

Mounted at `/api/v1/knowledge`. Authentication and tenant/owner/service scope come from the
same `resolve_caller_scope` used by the workspace API; a service must be allowlisted through
`AGCORE_KNOWLEDGE_ALLOWED_SERVICES` and the feature through `AGCORE_KNOWLEDGE_ENABLED`.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import ExecutionScope, resolve_caller_scope
from app.knowledge.contracts import (
    ArtifactCreateRequest,
    ArtifactRollbackRequest,
    ArtifactUpdateRequest,
    ArtifactValidateRequest,
    DocumentNamespace,
    FederatedSearchRequest,
    KnowledgeContextRequest,
    KnowledgeError,
    ReadRequest,
    VisionAnalyzeRequest,
)
from app.knowledge.service import DocumentKnowledgeService
from app.workspace.runtime import WorkspaceRuntime, get_runtime

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


def get_scope(request: Request) -> ExecutionScope:
    try:
        scope = resolve_caller_scope(request)
    except Exception as exc:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": str(exc)[:200]}) from exc
    if not scope.authenticated:
        raise HTTPException(status_code=401, detail={"code": "forbidden", "message": "authentication_required"})
    return scope


def get_knowledge_runtime(scope: ExecutionScope = Depends(get_scope)) -> WorkspaceRuntime:
    from app.config import is_knowledge_enabled, is_knowledge_service_allowed

    rt = get_runtime()
    if not (rt.settings.enabled or is_knowledge_enabled()):
        raise HTTPException(
            status_code=503,
            detail={"code": "knowledge_disabled", "message": "AGCORE_KNOWLEDGE_ENABLED is off"},
        )
    if not is_knowledge_service_allowed(scope.service_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "knowledge_disabled_for_service",
                "message": f"knowledge is not enabled for service '{scope.service_id}'",
            },
        )
    return rt


def get_service(rt: WorkspaceRuntime = Depends(get_knowledge_runtime)) -> DocumentKnowledgeService:
    return _service_for(rt)


_SERVICES: dict[int, DocumentKnowledgeService] = {}


def _service_for(runtime: WorkspaceRuntime) -> DocumentKnowledgeService:
    key = id(runtime)
    if key not in _SERVICES:
        _SERVICES[key] = DocumentKnowledgeService(runtime)
    return _SERVICES[key]


def guarded(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except KnowledgeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.body.model_dump(mode="json")) from exc

    return wrapper


class KnowledgeReadRequest(ReadRequest):
    """dk/v1 read with the AGCore routing fields it needs (namespace/scope)."""

    workspace_id: Optional[str] = None
    conversation_id: Optional[str] = None
    namespace: DocumentNamespace = DocumentNamespace.WORKSPACE


@router.get("/capabilities")
@guarded
def capabilities(
    scope: ExecutionScope = Depends(get_scope),
    service: DocumentKnowledgeService = Depends(get_service),
):
    return service.manifest()


@router.post("/search")
@guarded
def search(
    request: FederatedSearchRequest,
    scope: ExecutionScope = Depends(get_scope),
    service: DocumentKnowledgeService = Depends(get_service),
):
    return service.search(scope, request)


@router.post("/read")
@guarded
def read(
    request: KnowledgeReadRequest,
    scope: ExecutionScope = Depends(get_scope),
    service: DocumentKnowledgeService = Depends(get_service),
):
    base = ReadRequest(**request.model_dump(exclude={"workspace_id", "conversation_id", "namespace"}))
    return service.read(
        scope,
        base,
        workspace_id=request.workspace_id,
        conversation_id=request.conversation_id,
        namespace=request.namespace,
    )


@router.post("/context")
@guarded
def context(
    request: KnowledgeContextRequest,
    scope: ExecutionScope = Depends(get_scope),
    service: DocumentKnowledgeService = Depends(get_service),
):
    return service.context(scope, request)


@router.post("/artifacts")
@guarded
def create_artifact(
    request: ArtifactCreateRequest,
    scope: ExecutionScope = Depends(get_scope),
    service: DocumentKnowledgeService = Depends(get_service),
):
    return service.create_artifact(scope, request)


@router.post("/artifacts/update")
@guarded
def update_artifact(
    request: ArtifactUpdateRequest,
    scope: ExecutionScope = Depends(get_scope),
    service: DocumentKnowledgeService = Depends(get_service),
):
    return service.update_artifact(scope, request)


@router.post("/artifacts/rollback")
@guarded
def rollback_artifact(
    request: ArtifactRollbackRequest,
    scope: ExecutionScope = Depends(get_scope),
    service: DocumentKnowledgeService = Depends(get_service),
):
    return service.rollback_artifact(scope, request)


@router.post("/artifacts/validate")
@guarded
def validate_artifact(
    request: ArtifactValidateRequest,
    scope: ExecutionScope = Depends(get_scope),
    service: DocumentKnowledgeService = Depends(get_service),
):
    return service.validate_artifact(scope, request)


@router.post("/vision/analyze")
@guarded
def vision_analyze(
    request: VisionAnalyzeRequest,
    conversation_id: Optional[str] = None,
    scope: ExecutionScope = Depends(get_scope),
    service: DocumentKnowledgeService = Depends(get_service),
):
    return service.vision_analyze(scope, conversation_id, request)
