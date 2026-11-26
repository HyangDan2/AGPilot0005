"""Owner-scoped asynchronous full-document analysis API."""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

from app.core.auth import ExecutionScope, require_authenticated_caller
from app.runtime.tasks.bootstrap import services
from app.runtime.tasks.domain import BudgetExceeded

router = APIRouter(prefix="/agent/analyses", tags=["Document Analysis"], dependencies=[Depends(require_authenticated_caller)])


class AnalysisRequest(BaseModel):
    reasoning_effort: Literal['off','on','low','medium','high','default'] = 'off'
    message: str = Field(min_length=1, max_length=4000)
    context_refs: list[str] = Field(min_length=1, max_length=10)


def _identity(request: Request) -> tuple[str, str]:
    scope: ExecutionScope = require_authenticated_caller(request)
    return scope.owner_id, scope.service_id


@router.post("", status_code=202)
async def create_analysis(body: AnalysisRequest, request: Request):
    owner, service = _identity(request)
    try:
        return await services(request.app).create(owner, service, body.message, body.context_refs, reasoning_effort=body.reasoning_effort)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{job_id}")
async def get_analysis(job_id: str, request: Request):
    owner, service = _identity(request)
    manager = services(request.app)
    try:
        result = manager.get(job_id, owner, service)
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
            current = manager.get(job_id, owner, service)
            heartbeat += 1

    return StreamingResponse(snapshots(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Vary": "Accept"})


@router.post("/{job_id}/resume")
async def resume_analysis(job_id: str, request: Request):
    owner, service = _identity(request)
    try:
        result = services(request.app).resume(job_id, owner, service)
    except BudgetExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError:
        result = None
    if result is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return result


@router.post("/{job_id}/cancel")
async def cancel_analysis(job_id: str, request: Request):
    owner, service = _identity(request)
    try:
        result = services(request.app).cancel(job_id, owner, service)
    except ValueError:
        result = None
    if result is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return result
