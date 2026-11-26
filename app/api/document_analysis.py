"""Owner-scoped asynchronous full-document analysis API."""
import asyncio
import inspect
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

from app.core.auth import (
    ExecutionScope,
    current_execution_scope,
    require_authenticated_caller,
)
from app.runtime.tasks.bootstrap import services
from app.runtime.tasks.domain import BudgetExceeded

router = APIRouter(prefix="/agent/analyses", tags=["Document Analysis"], dependencies=[Depends(require_authenticated_caller)])


class AnalysisRequest(BaseModel):
    reasoning_effort: Literal['off','on','low','medium','high','default'] = 'off'
    message: str = Field(min_length=1, max_length=4000)
    context_refs: list[str] = Field(min_length=1, max_length=10)


def _identity(request: Request) -> tuple[ExecutionScope, str, str]:
    scope: ExecutionScope = require_authenticated_caller(request)
    return scope, scope.owner_id, scope.service_id


def _accepts_tenant(fn) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return "tenant_id" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


@router.post("", status_code=202)
async def create_analysis(body: AnalysisRequest, request: Request):
    scope, owner, service = _identity(request)
    tok = current_execution_scope.set(scope)
    try:
        mgr = services(request.app)
        kwargs = {"reasoning_effort": body.reasoning_effort}
        if _accepts_tenant(mgr.create):
            kwargs["tenant_id"] = scope.tenant_id
        return await mgr.create(owner, service, body.message, body.context_refs, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        current_execution_scope.reset(tok)


@router.get("/{job_id}")
async def get_analysis(job_id: str, request: Request):
    scope, owner, service = _identity(request)
    manager = services(request.app)
    get_kwargs = {"tenant_id": scope.tenant_id} if _accepts_tenant(manager.get) else {}
    try:
        result = manager.get(job_id, owner, service, **get_kwargs)
    except ValueError:
        result = None
    if result is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    if "text/event-stream" not in request.headers.get("accept", ""):
        return result

    async def snapshots():
        current, previous = result, None
        heartbeat = 0
        while current is not None:
            if current != previous:
                yield "event: analysis.snapshot\ndata: " + json.dumps(current, ensure_ascii=True) + "\n\n"
                previous = current
                heartbeat = 0
            elif heartbeat >= 20:
                yield ": heartbeat\n\n"
                heartbeat = 0
            if current["status"] not in {"queued", "planning", "running", "verifying"}:
                return
            await asyncio.sleep(0.5)
            current = manager.get(job_id, owner, service, **get_kwargs)
            heartbeat += 1

    return StreamingResponse(snapshots(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Vary": "Accept"})


@router.post("/{job_id}/resume")
async def resume_analysis(job_id: str, request: Request):
    scope, owner, service = _identity(request)
    tok = current_execution_scope.set(scope)
    try:
        mgr = services(request.app)
        kwargs = {"tenant_id": scope.tenant_id} if _accepts_tenant(mgr.resume) else {}
        result = mgr.resume(job_id, owner, service, **kwargs)
    except BudgetExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError:
        result = None
    finally:
        current_execution_scope.reset(tok)
    if result is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return result


@router.post("/{job_id}/cancel")
async def cancel_analysis(job_id: str, request: Request):
    scope, owner, service = _identity(request)
    try:
        mgr = services(request.app)
        kwargs = {"tenant_id": scope.tenant_id} if _accepts_tenant(mgr.cancel) else {}
        result = mgr.cancel(job_id, owner, service, **kwargs)
    except ValueError:
        result = None
    if result is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return result
