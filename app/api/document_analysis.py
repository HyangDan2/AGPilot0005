"""Owner-scoped asynchronous full-document analysis API."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal

from app.runtime.tasks.bootstrap import services
from app.runtime.tasks.domain import BudgetExceeded

router = APIRouter(prefix="/agent/analyses", tags=["Document Analysis"])


class AnalysisRequest(BaseModel):
    reasoning_effort: Literal['off','on','low','medium','high','default'] = 'off'
    message: str = Field(min_length=1, max_length=4000)
    context_refs: list[str] = Field(min_length=1, max_length=10)


def _identity(request: Request) -> tuple[str, str]:
    return request.headers.get("X-HD2Lab-User") or "default_user", request.headers.get("X-HD2Lab-Service") or "hd2ai-admin"


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
    try:
        result = services(request.app).get(job_id, owner, service)
    except ValueError:
        result = None
    if result is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return result


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
