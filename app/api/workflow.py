"""R3 Workflow REST + SSE API.

Base: ``/api/v1/agent/workflows``.

Identity is never read from the body: every endpoint resolves the caller through
:func:`app.core.auth.require_authenticated_caller`, and a run is only visible to the
tenant/owner/service that created it (``_authorize_run``).  A run that belongs to someone
else answers ``404 workflow_run_not_found`` — the same answer as a run that does not
exist, so the endpoint cannot be used to probe for other tenants' runs.

Errors use the structured envelope shared with the R2-W workspace / Canvas contract::

    {"detail": {"code": "...", "message": "...", "retryable": false, "context": {...}}}
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.core.auth import ExecutionScope, require_authenticated_caller
from app.schemas.events import SSEEvent
from app.workflow import build_workflow_stack
from app.workflow.checkpoint import WorkflowCheckpoint, WorkflowCheckpointStore, default_checkpoint_path
from app.workflow.mcp import MCPConfigError, MCPError, MCPRegistrationError, MCPServerConfig
from app.workflow.office_adapter import OfficeAdapterError, get_office_adapter
from app.workflow.planner import PlannerError, PlannerRequest
from app.workflow.runtime import (
    WorkflowRunRequest,
    WorkflowRuntime,
    WorkflowRuntimeError,
)
from app.workflow.safety import ApprovalError, ExecutionContext, ScopeViolation

__all__ = [
    "router",
    "WorkflowService",
    "get_workflow_service",
    "reset_workflow_service",
    "set_workflow_service",
]


def _http(status_code: int, code: str, message: str, **context: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": False, "context": context},
    )


# --------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------
class PlanRequestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = ""
    context: Dict[str, Any] = Field(default_factory=dict)
    budget: Dict[str, Any] = Field(default_factory=dict)
    execution_mode: Literal["auto", "direct", "workflow"] = "auto"
    available_capabilities: List[str] = Field(default_factory=list)


class RunRequestBody(WorkflowRunRequest):
    model_config = ConfigDict(extra="ignore")

    stream: bool = False


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    approval: Optional[Dict[str, Any]] = None


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    approved_by: Optional[str] = None
    ttl_seconds: int = 300


class MCPServerBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    server_id: str
    transport: Literal["stdio", "streamable_http"] = "streamable_http"
    command_or_url: str
    allowed_tools: List[str] = Field(default_factory=list)
    timeout_seconds: float = 15.0
    max_retries: int = 2
    credential_ref: Optional[str] = None
    description: str = ""
    connect: bool = True


# --------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------
def _default_office_adapter() -> Any:
    """The office adapter chosen by ``AGCORE_R3_OFFICE_ADAPTER`` (``mock`` | ``runtime``).

    A misconfigured runtime adapter degrades to ``None`` instead of answering capability
    calls with fake data: a step that needs it then fails with ``mcp_unavailable``-style
    evidence instead of a silent mock result.
    """
    kind = (os.environ.get("AGCORE_R3_OFFICE_ADAPTER") or "mock").strip().lower()
    try:
        return get_office_adapter(
            kind,
            environment=settings.ENVIRONMENT,
            base_url=(os.environ.get("AGCORE_AGTOOLS_BASE_URL") or "").strip() or None,
        )
    except OfficeAdapterError:
        return None


_DEFAULT_OFFICE = object()


class WorkflowService:
    """Owns one runtime + checkpoint store for the API process."""

    def __init__(
        self,
        *,
        runtime: Optional[WorkflowRuntime] = None,
        office: Any = _DEFAULT_OFFICE,
        checkpoint_path: Optional[str] = None,
        checkpoint_backend: str = "sqlite",
        workspace_root: Optional[str] = None,
    ) -> None:
        if runtime is not None:
            self.runtime = runtime
            self.office = runtime.office
            return
        self.office = _default_office_adapter() if office is _DEFAULT_OFFICE else office
        stack = build_workflow_stack(workspace_root=workspace_root)
        store = WorkflowCheckpointStore(
            checkpoint_path or str(default_checkpoint_path()), backend=checkpoint_backend,  # type: ignore[arg-type]
        )
        self.runtime = WorkflowRuntime(
            registry=stack.registry,
            skills=stack.skills,
            rules=stack.rules,
            hooks=stack.hooks,
            mcp=stack.mcp,
            office=self.office,
            store=store,
            approvals=stack.safety["approvals"],
            ledger=stack.safety["idempotency"],
            max_steps=stack.budget.max_step_budget,
            max_tool_calls=stack.budget.max_tool_calls,
            max_context_tokens=stack.budget.max_context_tokens,
            workspace_root=workspace_root,
        )

    # -- scope helpers ----------------------------------------------------------------
    @staticmethod
    def execution_context(scope: ExecutionScope, request: WorkflowRunRequest) -> ExecutionContext:
        context = request.context or {}
        try:
            return ExecutionContext(
                tenant_id=scope.tenant_id,
                owner_id=scope.owner_id,
                service_id=scope.service_id or None,
                session_id=_clean(context.get("session_id")),
                conversation_id=_clean(context.get("conversation_id")),
                workspace_id=_clean(context.get("workspace_id")),
            )
        except ValueError as exc:
            raise _http(400, "invalid_scope", str(exc)) from exc

    def authorize_run(self, run_id: str, scope: ExecutionScope) -> WorkflowCheckpoint:
        checkpoint = self.runtime.store.load_raw(run_id)
        if checkpoint is None:
            raise _http(404, "workflow_run_not_found", f"no workflow run {run_id}")
        stored = checkpoint.scope or {}
        same_owner = (
            str(stored.get("tenant_id")) == str(scope.tenant_id)
            and str(stored.get("owner_id")) == str(scope.owner_id)
        )
        admin_transfer = scope.is_admin and scope.can_access_tenant(stored.get("tenant_id"))
        if not (same_owner or admin_transfer):
            raise _http(404, "workflow_run_not_found", f"no workflow run {run_id}")
        if stored.get("service_id") and not scope.can_access_service(stored.get("service_id")):
            raise _http(404, "workflow_run_not_found", f"no workflow run {run_id}")
        return checkpoint

    @staticmethod
    def run_context(scope: ExecutionScope, checkpoint: WorkflowCheckpoint) -> ExecutionContext:
        """Rebuild the run's execution context and re-verify its scope (WP-10).

        The stored session/conversation/workspace dimensions are restored so
        :func:`assert_scope_access` compares them, while tenant/owner/service come from
        the authenticated credential.
        """
        stored = checkpoint.scope or {}
        admin_transfer = scope.is_admin and str(stored.get("tenant_id")) != str(scope.tenant_id)
        try:
            return ExecutionContext(
                tenant_id=str(stored.get("tenant_id")) if admin_transfer else scope.tenant_id,
                owner_id=str(stored.get("owner_id")) if admin_transfer else scope.owner_id,
                service_id=(stored.get("service_id") or scope.service_id) if admin_transfer else scope.service_id,
                session_id=stored.get("session_id"),
                conversation_id=stored.get("conversation_id"),
                workspace_id=stored.get("workspace_id"),
            )
        except ValueError as exc:
            raise _http(404, "workflow_run_not_found", str(exc)) from exc


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_SERVICE: Optional[WorkflowService] = None
_SERVICE_LOCK = threading.Lock()


def get_workflow_service() -> WorkflowService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = WorkflowService()
    return _SERVICE


def set_workflow_service(service: Optional[WorkflowService]) -> None:
    global _SERVICE
    _SERVICE = service


def reset_workflow_service() -> None:
    global _SERVICE
    _SERVICE = None


# --------------------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------------------
router = APIRouter(
    prefix="/agent/workflows",
    tags=["R3 Workflows"],
    dependencies=[Depends(require_authenticated_caller)],
)


def _service() -> WorkflowService:
    return get_workflow_service()


def _sse(payload: Dict[str, Any]) -> str:
    event = str(payload.get("event") or "workflow.started")
    run_id = str(payload.get("run_id") or "-")
    data = {key: value for key, value in payload.items() if key not in ("event", "run_id")}
    try:
        frame = SSEEvent(
            run_id=run_id, event_id=f"evt_{uuid.uuid4().hex[:16]}", event=event,  # type: ignore[arg-type]
            status=str(payload.get("status") or "running"), data=data,
        )
        return frame.to_sse()
    except Exception:  # noqa: BLE001 - unknown event names still reach the client
        body = {"run_id": run_id, "status": payload.get("status"), **data}
        return f"event: {event}\ndata: {json.dumps(body, ensure_ascii=False, default=str)}\n\n"


@router.post("/plan")
async def plan_workflow(
    body: PlanRequestBody,
    scope: ExecutionScope = Depends(require_authenticated_caller),
) -> JSONResponse:
    """Route decision + plan without executing anything (fast path lives here)."""
    service = _service()
    request = PlannerRequest(
        query=body.query,
        context=body.context,
        budget=body.budget,
        execution_mode=body.execution_mode,
        available_capabilities=tuple(body.available_capabilities),
    )
    try:
        decision = service.runtime.planner.plan(request)
    except PlannerError as exc:
        raise _http(422, exc.code, str(exc)) from exc
    planner = service.runtime.planner
    return JSONResponse(
        status_code=200,
        content={
            **decision.explain(),
            "fast_path": {
                "model_calls": planner.model_calls,
                "skill_loads": planner.skill_loads,
                "graph_runs": service.runtime.graph_runs,
            },
        },
    )


@router.post("/runs")
async def create_run(
    body: RunRequestBody,
    scope: ExecutionScope = Depends(require_authenticated_caller),
):
    """Start a run; ``stream=true`` returns the nine ``workflow.*`` events as SSE."""
    service = _service()
    request = WorkflowRunRequest(**body.model_dump(exclude={"stream"}))
    context = service.execution_context(scope, request)
    if not body.stream:
        try:
            result = await service.runtime.start(request, context)
        except WorkflowRuntimeError as exc:
            raise _http(exc.http_status, exc.code, exc.detail) from exc
        except ScopeViolation as exc:
            raise _http(403, exc.code, str(exc)) from exc
        status_code = 202 if result.status == "awaiting_approval" else 200
        return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def sink(event: Dict[str, Any]) -> None:
            await queue.put(event)

        async def runner() -> None:
            try:
                await service.runtime.start(request, context, emit=sink)
            except Exception as exc:  # noqa: BLE001 - report, never break the stream
                await queue.put(
                    {
                        "event": "workflow.failed", "run_id": request.run_id or "-",
                        "status": "failed", "error": {"code": type(exc).__name__, "message": str(exc)[:500]},
                    }
                )
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield _sse(item)
        finally:
            with contextlib.suppress(Exception):
                await task

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    scope: ExecutionScope = Depends(require_authenticated_caller),
) -> JSONResponse:
    service = _service()
    checkpoint = service.authorize_run(run_id, scope)
    context = service.run_context(scope, checkpoint)
    try:
        payload = service.runtime.get(run_id, context=context)
    except ScopeViolation as exc:
        raise _http(404, "workflow_run_not_found", str(exc)) from exc
    return JSONResponse(status_code=200, content=payload)


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    body: ResumeBody,
    scope: ExecutionScope = Depends(require_authenticated_caller),
) -> JSONResponse:
    service = _service()
    checkpoint = service.authorize_run(run_id, scope)
    context = service.run_context(scope, checkpoint)
    try:
        result = await service.runtime.resume(run_id, context, approval=body.approval)
    except ApprovalError as exc:
        raise _http(403, exc.code, str(exc)) from exc
    except ScopeViolation as exc:
        raise _http(404, "workflow_run_not_found", str(exc)) from exc
    except WorkflowRuntimeError as exc:
        raise _http(exc.http_status, exc.code, exc.detail) from exc
    return JSONResponse(status_code=200, content=result.model_dump(mode="json"))


@router.post("/runs/{run_id}/approve")
async def approve_run(
    run_id: str,
    body: ApproveBody,
    scope: ExecutionScope = Depends(require_authenticated_caller),
) -> JSONResponse:
    service = _service()
    checkpoint = service.authorize_run(run_id, scope)
    context = service.run_context(scope, checkpoint)
    try:
        token = await service.runtime.approve(
            run_id, context, approved_by=body.approved_by, ttl_seconds=body.ttl_seconds,
        )
    except ApprovalError as exc:
        raise _http(403, exc.code, str(exc)) from exc
    except ScopeViolation as exc:
        raise _http(404, "workflow_run_not_found", str(exc)) from exc
    except WorkflowRuntimeError as exc:
        raise _http(exc.http_status, exc.code, exc.detail) from exc
    return JSONResponse(
        status_code=200,
        content={
            "run_id": run_id,
            "approval": token.model_dump(mode="json"),
            "next": "POST /agent/workflows/runs/{run_id}/resume with {\"approval\": <token>}",
        },
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    scope: ExecutionScope = Depends(require_authenticated_caller),
) -> JSONResponse:
    service = _service()
    checkpoint = service.authorize_run(run_id, scope)
    context = service.run_context(scope, checkpoint)
    try:
        updated = await service.runtime.cancel(run_id, context)
    except ScopeViolation as exc:
        raise _http(404, "workflow_run_not_found", str(exc)) from exc
    except WorkflowRuntimeError as exc:
        raise _http(exc.http_status, exc.code, exc.detail) from exc
    return JSONResponse(status_code=200, content={"run_id": run_id, "checkpoint": updated.public_view()})


@router.get("/tools")
async def list_tools(
    scope: ExecutionScope = Depends(require_authenticated_caller),
) -> JSONResponse:
    service = _service()
    runtime = service.runtime
    specs = runtime.registry.list_specs() if runtime.registry is not None else []
    return JSONResponse(
        status_code=200,
        content={
            "registry": runtime.registry.describe() if runtime.registry is not None else {},
            "tools": [
                {
                    "qualified_name": spec.qualified_name,
                    "capability": spec.capability,
                    "source": spec.source,
                    "permission": spec.permission,
                    "side_effect": spec.side_effect,
                    "approval_required": spec.approval_required,
                }
                for spec in specs
            ],
            "office_capabilities": list(runtime.office.capabilities) if runtime.office is not None else [],
            "mcp": runtime.mcp.describe() if runtime.mcp is not None else {"servers": []},
        },
    )


@router.get("/skills")
async def list_skills(
    query: Optional[str] = Query(default=None),
    scope: ExecutionScope = Depends(require_authenticated_caller),
) -> JSONResponse:
    service = _service()
    runtime = service.runtime
    summaries = runtime.skills.discover_skills(query=query) if runtime.skills is not None else []
    return JSONResponse(
        status_code=200,
        content={
            "skills": [summary.model_dump(mode="json") for summary in summaries],
            "loaded": runtime.skills.loaded_skill_count() if runtime.skills is not None else 0,
        },
    )


@router.post("/mcp/servers")
async def register_mcp_server(
    body: MCPServerBody,
    scope: ExecutionScope = Depends(require_authenticated_caller),
) -> JSONResponse:
    service = _service()
    runtime = service.runtime
    if runtime.mcp is None:
        raise _http(503, "mcp_unavailable", "MCP manager is not configured")
    try:
        config = MCPServerConfig(
            server_id=body.server_id,
            transport=body.transport,
            command_or_url=body.command_or_url,
            allowed_tools=tuple(body.allowed_tools),
            timeout_seconds=body.timeout_seconds,
            max_retries=body.max_retries,
            credential_ref=body.credential_ref,
            description=body.description,
        )
        runtime.mcp.register_server(config)
    except MCPRegistrationError as exc:
        raise _http(409, "mcp_server_exists", str(exc)) from exc
    except MCPConfigError as exc:
        raise _http(400, "mcp_config_invalid", str(exc)) from exc
    except ValueError as exc:
        raise _http(400, "mcp_config_invalid", str(exc)) from exc

    report: Dict[str, Any] = {"server_id": config.server_id, "state": runtime.mcp.state(config.server_id).value}
    if body.connect:
        try:
            await runtime.mcp.connect(config.server_id)
            discovery = await runtime.mcp.discover_tools(config.server_id)
            report.update(
                {
                    "state": runtime.mcp.state(config.server_id).value,
                    "registered_tools": list(discovery.registered),
                    "blocked_tools": list(discovery.blocked),
                    "reasons": dict(discovery.reasons),
                }
            )
        except MCPError as exc:
            report.update({"state": runtime.mcp.state(config.server_id).value, "error": str(exc)})
            return JSONResponse(status_code=202, content=report)
    return JSONResponse(status_code=201, content=report)
