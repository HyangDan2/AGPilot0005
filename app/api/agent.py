"""Agent Execution and Tracing REST API Endpoints."""
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal, Optional
from app.runtime.runner import runner, DocumentReferenceError
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.tools.registry import registry
from app.runtime.tasks.bootstrap import services
from app.tracing.store import get_trajectory_logger
import json
import re

router = APIRouter(prefix="/agent", tags=["Agent"])


class RunFeedbackRequest(BaseModel):
    feedback_type: Literal["thumbs_up", "thumbs_down", "delayed_eval"]
    reward_signal: float = Field(ge=-1.0, le=1.0)
    decision_id: Optional[str] = None
    trajectory_id: Optional[str] = None


def durable_job(run_id):
    return 'ana_' + run_id[4:] if re.fullmatch(r'run-[a-f0-9]{32}', run_id) else None

@router.post("/runs")
async def create_agent_run(req: AgentRunRequest, request: Request):
    """Initiates an agent run session."""
    # Service identity extracted from header or default
    service_id = request.headers.get("X-HD2Lab-Service", "hd2ai-admin")
    owner = request.headers.get("X-HD2Lab-User") or "default_user"
    if any(m.get('role') not in ('user', 'assistant') or set(m) - {'role', 'content'} for m in req.history) or len(json.dumps(req.history, ensure_ascii=False).encode()) > 24000:
        raise HTTPException(422, 'invalid_task_history')
    if service_id in ('hd2ai-admin', 'hd2ai-beta', 'hd2ai-public') and (req.context_refs or req.analysis_refs):
        try:
            job = await services(request.app).create(owner, service_id, req.message, req.context_refs, req.history, req.analysis_refs,
                session_id=req.session_id, reasoning_effort=(req.options.reasoning_effort if req.options else None) or 'off')
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        run_id = 'run-' + job['job_id'][4:]
        if req.stream:
            async def deferred():
                yield 'event: run.started\ndata: ' + json.dumps({'run_id': run_id, 'executor': 'document'}) + '\n\n'
                yield 'event: run.deferred\ndata: ' + json.dumps({'run_id': run_id, 'job_id': job['job_id'], 'status': job['status']}) + '\n\n'
            return StreamingResponse(deferred(), media_type='text/event-stream', headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
        return JSONResponse({'run_id': run_id, **job}, status_code=202)
    try:
        session = await runner.create_run(req, service_id=service_id,
                                          owner_id=request.headers.get("X-HD2Lab-User") or "default_user")
    except DocumentReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runner.start_run(session)
    
    if req.stream:
        return StreamingResponse(
            runner.stream_events(session.state.run_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        
    return JSONResponse(
        status_code=202,
        content={
            "run_id": session.state.run_id,
            "agent_id": req.agent_id,
            "session_id": session.state.session_id,
            "status": session.state.status,
            "created_at": session.created_at,
        },
    )

@router.get("/runs/{run_id}", response_model=AgentRunResponse)
async def get_agent_run(run_id: str, request: Request):
    """Retrieves current run status, final answer, and execution traces."""
    job_id = durable_job(run_id)
    if job_id:
        job = services(request.app).get(job_id, request.headers.get('X-HD2Lab-User') or 'default_user', request.headers.get('X-HD2Lab-Service', 'hd2ai-admin'))
        if job is None: raise HTTPException(404, 'Run not found')
        return AgentRunResponse(run_id=run_id, agent_id='general-agent', status=job['status'], answer=job['answer'], model_label=job.get('model_label'),
            usage={'job_id':job_id,'model_calls':job['model_calls'],'document_calls':job.get('document_calls',0)},
            created_at=job['created_at'], error={'code':job['error_code'],'message':job['error_code']} if job['error_code'] else None)
    session = runner.get_session(run_id)
    if not session or session.state.owner_id != (request.headers.get('X-HD2Lab-User') or 'default_user') or session.state.service_id != request.headers.get('X-HD2Lab-Service', 'hd2ai-admin'):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        
    state = session.state
    return AgentRunResponse(
        run_id=state.run_id,
        agent_id="general-agent",
        session_id=state.session_id,
        status=state.status,
        answer=state.final_answer,
        model_label=state.model_label if state.status == "completed" else None,
        traces=state.traces,
        usage={
            "steps": state.step_count,
            "tool_calls": state.tool_call_count,
            "duration_seconds": round(session.finished_at - session.created_at, 2) if session.finished_at else None,
        },
        created_at=session.created_at,
        finished_at=session.finished_at,
        error=state.error,
    )

@router.get("/runs/{run_id}/events")
async def get_agent_events(run_id: str, request: Request):
    """Streams SSE events for a specific run."""
    job_id = durable_job(run_id)
    if job_id:
        job = services(request.app).get(job_id, request.headers.get('X-HD2Lab-User') or 'default_user', request.headers.get('X-HD2Lab-Service', 'hd2ai-admin'))
        if job is None: raise HTTPException(404, 'Run not found')
        async def snapshot():
            yield 'event: run.deferred\ndata: ' + json.dumps({'run_id':run_id,'job_id':job_id,'status':job['status']}) + '\n\n'
        return StreamingResponse(snapshot(), media_type='text/event-stream', headers={'Cache-Control':'no-store'})
    session = runner.get_session(run_id)
    if not session or session.state.owner_id != (request.headers.get('X-HD2Lab-User') or 'default_user') or session.state.service_id != request.headers.get('X-HD2Lab-Service', 'hd2ai-admin'):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        
    return StreamingResponse(
        runner.stream_events(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )

@router.post("/runs/{run_id}/cancel")
async def cancel_agent_run(run_id: str, request: Request):
    """Cancels an in-progress agent run."""
    job_id = durable_job(run_id)
    if job_id:
        job = services(request.app).cancel(job_id, request.headers.get('X-HD2Lab-User') or 'default_user', request.headers.get('X-HD2Lab-Service', 'hd2ai-admin'))
        if job is None: raise HTTPException(404, 'Run not found')
        return {'run_id':run_id,'status':job['status']}
    session = runner.get_session(run_id)
    if not session or session.state.owner_id != (request.headers.get('X-HD2Lab-User') or 'default_user') or session.state.service_id != request.headers.get('X-HD2Lab-Service', 'hd2ai-admin'):
        raise HTTPException(404, 'Run not found')
    success = await runner.cancel_run(run_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot cancel run (already finished or not found).")
    return {"run_id": run_id, "status": "cancelled", "message": "Run cancelled."}


@router.post("/runs/{run_id}/feedback", status_code=201)
async def submit_run_feedback(run_id: str, body: RunFeedbackRequest, request: Request):
    """FIX-R1-03 / REG-POL-18: Records asynchronous FeedbackEvent linked to a run/trajectory."""
    if not re.fullmatch(r"run-[A-Za-z0-9_-]{4,64}", run_id):
        raise HTTPException(status_code=422, detail="invalid_run_id")
    tlogger = get_trajectory_logger()
    existing = tlogger.store.list_events(run_id=run_id)
    trajectory_id = body.trajectory_id or (existing[0]["trajectory_id"] if existing else f"traj-{run_id}")
    ev = tlogger.log_feedback(
        trajectory_id=trajectory_id,
        run_id=run_id,
        decision_id=body.decision_id,
        feedback_type=body.feedback_type,
        reward_signal=body.reward_signal,
    )
    return {
        "event_id": ev.event_id,
        "trajectory_id": ev.trajectory_id,
        "run_id": ev.run_id,
        "decision_id": ev.decision_id,
        "feedback_type": ev.feedback_type,
        "reward_signal": ev.reward_signal,
        "timestamp": ev.timestamp,
    }


@router.get("/runs/{run_id}/trajectory")
async def get_run_trajectory(run_id: str, request: Request):
    """Reconstructs append-only trajectory events for offline evaluation / admin diagnostics."""
    if not re.fullmatch(r"run-[A-Za-z0-9_-]{4,64}", run_id):
        raise HTTPException(status_code=422, detail="invalid_run_id")
    role = (request.headers.get("X-HD2Lab-Role") or "").lower()
    service_id = request.headers.get("X-HD2Lab-Service", "hd2ai-admin")
    if service_id == "hd2ai-public" and role not in ("admin", "admiral"):
        raise HTTPException(status_code=403, detail="forbidden")
    tlogger = get_trajectory_logger()
    return tlogger.store.reconstruct_trajectory(run_id=run_id)


@router.get("/agents")
async def list_agents():
    """Lists registered agents and their allowed tools."""
    tools = registry.list_tools()
    return {
        "agents": [
            {
                "id": "general-agent",
                "name": "General Assistant Agent",
                "description": "다양한 도구를 활용할 수 있는 범용 에이전트",
                "allowed_tools": [t.name for t in tools],
            }
        ]
    }
