"""Agent Execution and Tracing REST API Endpoints."""
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal, Optional
from starlette.concurrency import run_in_threadpool
from app.adapters.document import document_adapter
from app.api.conversations import resolve_run_binding
from app.api.document_analysis import require_analysis_session
from app.config import is_durable_analysis_enabled_for_service
from app.core.auth import (
    current_execution_scope,
    current_run_conversation_id,
    require_authenticated_caller,
    scoped_analysis_owner,
)
from app.runtime.event_bus import EventReplayExpiredError
from app.core.session_attachments import ATTACHMENT_RUN_PREFIX, attachments_enabled, is_session_attachment
from app.runtime.runner import runner, DocumentReferenceError
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.tools.registry import registry
from app.runtime.tasks.bootstrap import services
from app.tracing.store import get_trajectory_logger
import json
import re
import time

router = APIRouter(prefix="/agent", tags=["Agent"])

_SERVER_FEEDBACK_REWARD = {
    "thumbs_up": 1.0,
    "thumbs_down": -1.0,
}


class RunFeedbackRequest(BaseModel):
    feedback_type: Literal["thumbs_up", "thumbs_down", "delayed_eval"]
    reward_signal: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    decision_id: Optional[str] = None
    trajectory_id: Optional[str] = None


async def _create_workflow_run(req: AgentRunRequest, scope):
    """Execute the request on the shared R3 workflow runtime (FIX-04).

    Identity comes from the authenticated scope; the body only contributes the message, the
    capabilities it wants and its context.  Status is mapped into the agent status
    vocabulary so the existing client contract keeps working.
    """
    from app.api.workflow import get_workflow_service
    from app.workflow.omni import AGENT_STATUS_MAP, OmniRuntimeBridge
    from app.workflow.runtime import WorkflowRuntimeError
    from app.workflow.safety import ApprovalError, ScopeViolation

    bridge = OmniRuntimeBridge(get_workflow_service())
    try:
        result = await bridge.start_from_agent_request(req, scope)
    except ScopeViolation as exc:
        raise HTTPException(403, exc.code) from exc
    except ApprovalError as exc:
        raise HTTPException(403, exc.code) from exc
    except WorkflowRuntimeError as exc:
        raise HTTPException(exc.http_status, exc.code) from exc
    status = AGENT_STATUS_MAP.get(result.status, "running")
    payload = {
        "run_id": result.run_id,
        "agent_id": req.agent_id,
        "session_id": req.session_id,
        "status": status,
        "executor": "workflow",
        "workflow_status": result.status,
        "created_at": time.time(),
        "workflow": result.model_dump(mode="json"),
    }
    if result.status == "awaiting_approval":
        payload["approval_endpoint"] = f"/api/v1/agent/workflows/runs/{result.run_id}/approve"
    return JSONResponse(status_code=202 if status in ("queued", "running", "paused", "needs_input") else 200, content=payload)


def durable_job(run_id):
    return 'ana_' + run_id[4:] if re.fullmatch(r'run-[a-f0-9]{32}', run_id) else None


def require_run_session(session, request: Request) -> None:
    state = session.state
    if any(is_session_attachment(ref) for ref in state.context_refs) and (
        not state.conversation_id or request.query_params.get("conversation_id") != state.conversation_id
    ):
        raise HTTPException(404, "SESSION_NOT_FOUND")


def require_attachment_telemetry_session(run_id: str, request: Request) -> None:
    if run_id.startswith(ATTACHMENT_RUN_PREFIX):
        session = runner.get_session(run_id)
        if session is None:
            raise HTTPException(404, "SESSION_NOT_FOUND")
        require_run_session(session, request)

@router.post("/runs")
async def create_agent_run(req: AgentRunRequest, request: Request):
    """Initiates an agent run session with verified Caller ExecutionScope (FIX-201 / FIX-202)."""
    scope = require_authenticated_caller(request)
    service_id = scope.service_id
    owner = scope.owner_id
    if any(m.get('role') not in ('user', 'assistant') or set(m) - {'role', 'content'} for m in req.history) or len(json.dumps(req.history, ensure_ascii=False).encode()) > 24000:
        raise HTTPException(422, 'invalid_task_history')

    if req.conversation_id or req.bind_workspace:
        req = await run_in_threadpool(resolve_run_binding, req, scope)
    mode = req.execution_mode
    workspace_active = bool((req.workspace_id or "").strip())
    has_document_refs = bool(req.context_refs or req.analysis_refs)
    analysis_enabled = is_durable_analysis_enabled_for_service(service_id)
    managed_attachments = [ref for ref in req.context_refs if is_session_attachment(ref)]
    managed_history = None
    if analysis_enabled and req.analysis_refs and not workspace_active:
        detect = getattr(services(request.app), "references_attachments", None)
        managed_history = detect and detect(req.analysis_refs, owner, service_id, scope.tenant_id)
    if managed_attachments or managed_history:
        if not attachments_enabled(service_id):
            raise HTTPException(403, "session_attachments_disabled")
        if not req.bind_workspace or not req.conversation_id:
            raise HTTPException(422, "attachment_conversation_required")
        if req.session_id is not None and req.session_id != req.conversation_id:
            raise HTTPException(409, "attachment_conversation_conflict")
        req = req.model_copy(update={"session_id": req.conversation_id})
    if mode == "workspace" and not workspace_active:
        raise HTTPException(422, "workspace_id_required")
    if workspace_active and mode not in (None, "workspace", "workflow"):
        raise HTTPException(422, "workspace_execution_mode_required")
    if mode == "workflow":
        # FIX-04: the R3 graph is a capability of this same runtime, not a second one.
        return await _create_workflow_run(req, scope)
    if mode == "document_analysis":
        if not has_document_refs:
            raise HTTPException(422, "document_references_required")
        if not analysis_enabled:
            raise HTTPException(403, "document_analysis_disabled_for_service")

    use_analysis = mode == "document_analysis" or (
        mode is None and not workspace_active and analysis_enabled and has_document_refs
    )
    if use_analysis:
        tok = current_execution_scope.set(scope)
        conversation_token = current_run_conversation_id.set(req.conversation_id)
        try:
            for ref in managed_attachments:
                outline = await document_adapter.get_outline(ref)
                if outline.get("error") or outline.get("status") != "completed":
                    raise HTTPException(422, "attachment_unavailable_or_pending")
            job = await services(request.app).create(
                owner,
                service_id,
                req.message,
                req.context_refs,
                req.history,
                req.analysis_refs,
                session_id=req.session_id,
                reasoning_effort=(req.options.reasoning_effort if req.options else None) or 'off',
                tenant_id=scope.tenant_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            current_run_conversation_id.reset(conversation_token)
            current_execution_scope.reset(tok)
        run_id = 'run-' + job['job_id'][4:]
        if req.stream:
            async def deferred():
                yield 'event: run.started\ndata: ' + json.dumps({'run_id': run_id, 'executor': 'document'}) + '\n\n'
                yield 'event: run.deferred\ndata: ' + json.dumps({'run_id': run_id, 'job_id': job['job_id'], 'status': job['status']}) + '\n\n'
            return StreamingResponse(deferred(), media_type='text/event-stream', headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
        return JSONResponse({'run_id': run_id, **job}, status_code=202)
    if workspace_active:
        # Keep the Workspace executor and admit only conversation-scoped attachments.
        req = req.model_copy(update={"context_refs": managed_attachments, "analysis_refs": []})
    # FIX-04: the ordinary Omni path shares the R3 hook registry, so an installation's
    # session_start hook (audit/redaction/kill switch) applies to chat as well.  Fail-open:
    # a chat must never fail because a hook did, and nothing is constructed when R3 is not
    # wired into this process.
    from app.workflow.omni import get_omni_bridge

    bridge = get_omni_bridge()
    shared_services = None
    if bridge is not None:
        await bridge.dispatch_session_start(req, scope)
        # FIX-02 P1: a chat run gets the shared Skills / Rules / Tool Registry / MCP
        # catalogue too.  Reading it constructs nothing when R3 is not wired.
        shared_services = bridge.shared_services(req, scope)
    try:
        session = await runner.create_run(
            req, service_id=service_id, owner_id=owner, caller_scope=scope,
            shared_services=shared_services,
        )
    except DocumentReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        if str(exc) == "active_run_limit_exceeded":
            raise HTTPException(status_code=429, detail="active_run_limit_exceeded") from exc
        raise
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
    scope = require_authenticated_caller(request)
    if run_id.startswith("wfrun_"):
        return _workflow_run_response(run_id, request, scope)
    job_id = durable_job(run_id)
    if job_id:
        manager = services(request.app)
        require_analysis_session(manager, job_id, request)
        job = manager.get(job_id, scope.owner_id, scope.service_id, tenant_id=scope.tenant_id)
        if job is None: raise HTTPException(404, 'Run not found')
        return AgentRunResponse(run_id=run_id, agent_id='general-agent', status=job['status'], answer=job['answer'], model_label=job.get('model_label'),
            usage={'job_id':job_id,'model_calls':job['model_calls'],'document_calls':job.get('document_calls',0)},
            created_at=job['created_at'], error={'code':job['error_code'],'message':job['error_code']} if job['error_code'] else None)
    session, lookup_err = runner.lookup_session(
        run_id,
        owner_id=scope.owner_id,
        service_id=scope.service_id,
        tenant_id=scope.tenant_id,
        is_admin=scope.is_admin,
        caller_scope=scope,
    )
    if lookup_err == "SESSION_EXPIRED":
        return JSONResponse(status_code=410, content={"error_code": "SESSION_EXPIRED", "detail": "Session has expired."})
    if not session:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
        
    require_run_session(session, request)
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

def _workflow_run_response(run_id: str, request: Request, scope) -> AgentRunResponse:
    """FIX-04: surface an R3 workflow run through the agent status contract."""
    from app.api.workflow import get_workflow_service
    from app.workflow.omni import AGENT_STATUS_MAP

    service = get_workflow_service()
    checkpoint = service.authorize_run(run_id, scope)          # 404 for other owners
    context = service.run_context(scope, checkpoint)           # re-verifies scope + binding
    payload = service.runtime.get(run_id, context=context)
    trajectory = payload.get("trajectory") or {}
    checkpoint_view = payload.get("checkpoint") or {}
    status = AGENT_STATUS_MAP.get(str(checkpoint_view.get("status")), "running")
    return AgentRunResponse(
        run_id=run_id,
        agent_id="workflow",
        status=status,
        answer=json.dumps(
            {"workflow_status": checkpoint_view.get("status"),
             "plan_id": checkpoint_view.get("plan_id"),
             "completed_steps": checkpoint_view.get("completed_steps")},
            ensure_ascii=False,
        ),
        usage={
            "steps": len(checkpoint_view.get("completed_steps") or []),
            "plan_version": checkpoint_view.get("plan_version"),
            "replans": checkpoint_view.get("replans"),
        },
        created_at=checkpoint.created_at.timestamp(),
        error=({"code": str(trajectory.get("status_reason") or ""), "message": str(trajectory.get("status_reason") or "")}
               if checkpoint_view.get("status") in ("failed", "recovery_required") else None),
    )


@router.get("/runs/{run_id}/events")
async def get_agent_events(run_id: str, request: Request):
    """Streams SSE events for a specific run with Last-Event-ID replay and HTTP 410 expiration."""
    scope = require_authenticated_caller(request)
    job_id = durable_job(run_id)
    if job_id:
        manager = services(request.app)
        require_analysis_session(manager, job_id, request)
        job = manager.get(job_id, scope.owner_id, scope.service_id, tenant_id=scope.tenant_id)
        if job is None: raise HTTPException(404, 'Run not found')
        async def snapshot():
            yield 'event: run.deferred\ndata: ' + json.dumps({'run_id':run_id,'job_id':job_id,'status':job['status']}) + '\n\n'
        return StreamingResponse(snapshot(), media_type='text/event-stream', headers={'Cache-Control':'no-store'})
    session, lookup_err = runner.lookup_session(
        run_id,
        owner_id=scope.owner_id,
        service_id=scope.service_id,
        tenant_id=scope.tenant_id,
        is_admin=scope.is_admin,
        caller_scope=scope,
    )
    if lookup_err == "SESSION_EXPIRED":
        return JSONResponse(status_code=410, content={"error_code": "SESSION_EXPIRED", "detail": "Session has expired."})
    if not session:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")

    require_run_session(session, request)
    last_event_id = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id")
    # HF-05: Atomically validate cursor + snapshot replay + register subscriber BEFORE StreamingResponse!
    try:
        stream_gen = runner.open_event_stream(run_id, last_event_id=last_event_id)
    except EventReplayExpiredError:
        return JSONResponse(
            status_code=410,
            content={"error_code": "event_replay_expired", "detail": "Requested event cursor is no longer retained."},
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_event_cursor")

    return StreamingResponse(
        stream_gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )

@router.post("/runs/{run_id}/cancel")
async def cancel_agent_run(run_id: str, request: Request):
    """Cancels an in-progress agent run."""
    scope = require_authenticated_caller(request)
    job_id = durable_job(run_id)
    if job_id:
        manager = services(request.app)
        require_analysis_session(manager, job_id, request)
        job = manager.cancel(job_id, scope.owner_id, scope.service_id, tenant_id=scope.tenant_id)
        if job is None: raise HTTPException(404, 'Run not found')
        return {'run_id':run_id,'status':job['status']}
    session, lookup_err = runner.lookup_session(
        run_id,
        owner_id=scope.owner_id,
        service_id=scope.service_id,
        tenant_id=scope.tenant_id,
        is_admin=scope.is_admin,
        caller_scope=scope,
    )
    if lookup_err == "SESSION_EXPIRED":
        return JSONResponse(status_code=410, content={"error_code": "SESSION_EXPIRED", "detail": "Session has expired."})
    if not session:
        raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
    require_run_session(session, request)
    success = await runner.cancel_run(run_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot cancel run (already finished or not found).")
    return {"run_id": run_id, "status": "cancelled", "message": "Run cancelled."}


@router.post("/runs/{run_id}/feedback", status_code=201)
async def submit_run_feedback(run_id: str, body: RunFeedbackRequest, request: Request):
    """R1-FIX-02 / FIX-R1-03: Verified owner & referential-integrity FeedbackEvent ingestion."""
    scope = require_authenticated_caller(request)
    if not re.fullmatch(r"run-[A-Za-z0-9_-]{4,64}", run_id):
        raise HTTPException(status_code=422, detail="invalid_run_id")
    require_attachment_telemetry_session(run_id, request)

    tlogger = get_trajectory_logger()
    meta = tlogger.store.get_run_metadata(run_id)
    session = runner.get_session(run_id)

    # 1. Run existence check (no arbitrary event creation for non-existent runs)
    if meta is None and session is None:
        raise HTTPException(status_code=404, detail="run_not_found")

    run_owner = (meta or {}).get("owner_id") or (session.state.owner_id if session else None)
    run_service = (meta or {}).get("service_id") or (session.state.service_id if session else None)
    run_tenant = (meta or {}).get("tenant_id") or (session.state.tenant_id if session else None)
    actual_trajectory_id = (meta or {}).get("trajectory_id") or f"traj-{run_id}"
    known_decisions = (meta or {}).get("decision_ids") or set()

    # 2. Ownership, tenant & service scope verification (FIX-204 / HF-02 / ADD-01)
    if not scope.can_access_tenant(run_tenant) or not scope.can_access_service(run_service):
        raise HTTPException(status_code=403, detail="forbidden")
    if not scope.is_admin:
        if not run_owner or not run_service:
            raise HTTPException(status_code=403, detail="unowned_run_admin_only")
        if run_owner != scope.owner_id or run_service != scope.service_id:
            raise HTTPException(status_code=403, detail="forbidden")

    # 3. Referential integrity: trajectory_id and decision_id must belong to this run
    if body.trajectory_id is not None and body.trajectory_id != actual_trajectory_id:
        raise HTTPException(status_code=422, detail="trajectory_run_mismatch")
    if body.decision_id is not None and body.decision_id not in known_decisions:
        raise HTTPException(status_code=422, detail="decision_not_found_in_trajectory")

    # 4. Server-side reward normalization (users cannot assign arbitrary reward weights)
    if body.feedback_type in _SERVER_FEEDBACK_REWARD:
        normalized_reward = _SERVER_FEEDBACK_REWARD[body.feedback_type]
    else:
        if not scope.is_admin:
            raise HTTPException(status_code=403, detail="delayed_eval_requires_admin")
        if body.reward_signal is None:
            raise HTTPException(status_code=422, detail="reward_signal_required")
        normalized_reward = float(body.reward_signal)

    # 5. FIX-208 A/B: Direct atomic DB commit in worker thread (bypasses async queue, enforces DB UNIQUE index)
    import asyncio
    from app.tracing.events import FeedbackEvent, TRAJECTORY_SCHEMA_VERSION
    from app.tracing.store import DuplicateFeedbackError

    ev = FeedbackEvent(
        event_id=tlogger.new_id("fb-evt"),
        schema_version=TRAJECTORY_SCHEMA_VERSION,
        trajectory_id=actual_trajectory_id,
        run_id=run_id,
        decision_id=body.decision_id,
        feedback_type=body.feedback_type,
        reward_signal=normalized_reward,
        timestamp=tlogger.clock.now_epoch(),
    )
    try:
        await asyncio.to_thread(
            tlogger.store.append_feedback_atomic,
            ev,
            owner_id=scope.owner_id,
            service_id=scope.service_id,
            tenant_id=scope.tenant_id,
        )
    except DuplicateFeedbackError:
        raise HTTPException(status_code=409, detail="duplicate_feedback")
    except Exception:
        raise HTTPException(status_code=503, detail="feedback_persistence_unavailable")

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
    """R1-FIX-02 / HF-02: Tenant + Owner isolated trajectory reconstruction with explicit Admin tenant scope."""
    scope = require_authenticated_caller(request)
    if not re.fullmatch(r"run-[A-Za-z0-9_-]{4,64}", run_id):
        raise HTTPException(status_code=422, detail="invalid_run_id")
    require_attachment_telemetry_session(run_id, request)

    tlogger = get_trajectory_logger()
    meta = tlogger.store.get_run_metadata(run_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="trajectory_not_found")

    if not scope.can_access_tenant(meta.get("tenant_id")) or not scope.can_access_service(meta.get("service_id")):
        raise HTTPException(status_code=403, detail="forbidden")
    if not scope.is_admin:
        if not meta.get("owner_id") or not meta.get("service_id"):
            raise HTTPException(status_code=403, detail="unowned_trajectory_admin_only")
        if meta.get("service_id") == "hd2ai-public":
            raise HTTPException(status_code=403, detail="forbidden")
        if meta.get("owner_id") != scope.owner_id or meta.get("service_id") != scope.service_id:
            raise HTTPException(status_code=403, detail="forbidden")

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
                # FIX-04: the R3 workflow graph is exposed as a capability of this agent.
                "capabilities": ["workflow.run"],
            }
        ]
    }
