"""Agent Runner with Lifecycle, SSE Event Streaming, and Cancellation."""
import asyncio
from app.tools.registry import registry
from app.tools.adapters import ContextAdapter
from app.gateway.client import GatewayStreamError
import time
import uuid
import re
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, Optional, Set, Tuple
from app.config import settings
from app.core.runtime_config import get_runtime_config
from app.runtime.state import AgentState
from app.schemas.agent import AgentRunRequest, AgentRunResponse, ToolExecutionTrace
from app.schemas.events import SSEEvent
from app.schemas.gateway import GatewayMessage
from app.adapters.document import document_adapter, document_owner_id
from app.runtime.event_bus import RunEventBus
from app.runtime.fence import HardDeadline, TerminalStateFence

class DocumentReferenceError(ValueError):
    pass

class AgentRunSession:
    def __init__(self, state: AgentState):
        self.state = state
        self.event_bus = RunEventBus(run_id=state.run_id)
        self.fence = TerminalStateFence()
        self.task: Optional[asyncio.Task] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

    async def emit(self, event_name: str, data: Dict[str, Any]):
        if event_name in ("run.completed", "run.failed", "run.cancelled"):
            self.event_bus.publish_terminal(event_name, self.state.status, data)
            return
        if self.fence.is_terminal:
            self.fence.record_late_result()
            return
        self.event_bus.publish(event_name, self.state.status, data)

@dataclass(frozen=True)
class _SessionTombstone:
    """NG-07 & CR-02: Minimal metadata-only tombstone with Tenant, Owner, Service."""

    run_id: str
    tenant_id: str
    owner_id: str
    service_id: str
    expired_at: float


class AgentRunner:
    def __init__(
        self,
        *,
        finished_session_ttl_seconds: float = 3600.0,
        max_finished_sessions: int = 256,
        max_active_runs: int = 64,
        tombstone_ttl_seconds: float = 86400.0,
        max_tombstones: int = 1024,
    ):
        self.sessions: Dict[str, AgentRunSession] = {}
        self._tombstones: Dict[str, _SessionTombstone] = {}
        self._reserved_run_ids: Set[str] = set()
        self.finished_session_ttl_seconds = float(finished_session_ttl_seconds)
        self.max_finished_sessions = max(1, int(max_finished_sessions))
        self.max_active_runs = max(1, int(max_active_runs))
        self.tombstone_ttl_seconds = float(tombstone_ttl_seconds)
        self.max_tombstones = max(1, int(max_tombstones))
        self._lock = asyncio.Lock()

    def get_session(self, run_id: str) -> Optional[AgentRunSession]:
        return self.sessions.get(run_id)

    def evict_expired_sessions(self, *, now: Optional[float] = None) -> int:
        """NG-07: Evicts ONLY finished sessions past TTL or over max_finished_sessions; never evicts active runs."""
        ts = float(now if now is not None else time.time())
        evicted = 0

        # 1. Evict expired tombstones first
        for rid in [k for k, tb in self._tombstones.items() if ts - tb.expired_at > self.tombstone_ttl_seconds]:
            self._tombstones.pop(rid, None)

        # 2. Identify finished sessions eligible for eviction (NEVER queued/running)
        finished = [
            (rid, sess)
            for rid, sess in self.sessions.items()
            if sess.state.status not in ("queued", "running")
        ]
        finished.sort(key=lambda pair: pair[1].finished_at or pair[1].created_at)

        to_evict: list[tuple[str, AgentRunSession]] = []
        remaining_finished = 0
        for rid, sess in finished:
            fin_ts = sess.finished_at or sess.created_at
            if ts - fin_ts > self.finished_session_ttl_seconds:
                to_evict.append((rid, sess))
            else:
                remaining_finished += 1

        # Enforce max_finished_sessions cap (oldest first)
        overflow = remaining_finished - self.max_finished_sessions
        if overflow > 0:
            evict_ids = {rid for rid, _ in to_evict}
            for rid, sess in finished:
                if overflow <= 0:
                    break
                if rid not in evict_ids:
                    to_evict.append((rid, sess))
                    evict_ids.add(rid)
                    overflow -= 1

        for rid, sess in to_evict:
            self.sessions.pop(rid, None)
            self._tombstones[rid] = _SessionTombstone(
                run_id=rid,
                tenant_id=sess.state.tenant_id,
                owner_id=sess.state.owner_id,
                service_id=sess.state.service_id or "",
                expired_at=ts,
            )
            evicted += 1

        # Cap tombstone map size (oldest first)
        if len(self._tombstones) > self.max_tombstones:
            ordered = sorted(self._tombstones.items(), key=lambda kv: kv[1].expired_at)
            for rid, _ in ordered[: len(self._tombstones) - self.max_tombstones]:
                self._tombstones.pop(rid, None)

        return evicted

    def lookup_session(
        self,
        run_id: str,
        *,
        owner_id: str,
        service_id: str,
        tenant_id: Optional[str] = None,
        is_admin: bool = False,
        caller_scope: Optional[Any] = None,
    ) -> Tuple[Optional[AgentRunSession], Optional[str]]:
        """NG-07 / CR-02 / HF-02: Enforces Tenant + Owner + Service isolation (including explicit Admin tenant scope)."""
        self.evict_expired_sessions()
        sess = self.sessions.get(run_id)
        if sess is not None:
            if caller_scope is not None:
                if not caller_scope.can_access_tenant(sess.state.tenant_id) or not caller_scope.can_access_service(sess.state.service_id):
                    return None, "SESSION_NOT_FOUND"
                if not caller_scope.is_admin and (
                    sess.state.owner_id != caller_scope.owner_id or sess.state.service_id != caller_scope.service_id
                ):
                    return None, "SESSION_NOT_FOUND"
            elif not is_admin:
                if sess.state.owner_id != owner_id or sess.state.service_id != service_id:
                    return None, "SESSION_NOT_FOUND"
                if tenant_id is not None and sess.state.tenant_id != tenant_id:
                    return None, "SESSION_NOT_FOUND"
            return sess, None
        tb = self._tombstones.get(run_id)
        if tb is not None:
            if caller_scope is not None:
                if (
                    caller_scope.can_access_tenant(tb.tenant_id)
                    and caller_scope.can_access_service(tb.service_id)
                    and (
                        caller_scope.is_admin
                        or (tb.owner_id == caller_scope.owner_id and tb.service_id == caller_scope.service_id)
                    )
                ):
                    return None, "SESSION_EXPIRED"
            elif is_admin or (
                tb.owner_id == owner_id
                and tb.service_id == service_id
                and (tenant_id is None or tb.tenant_id == tenant_id)
            ):
                return None, "SESSION_EXPIRED"
        return None, "SESSION_NOT_FOUND"

    async def create_run(
        self,
        req: AgentRunRequest,
        service_id: str = "hd2ai-admin",
        custom_client: Optional[Any] = None,
        owner_id: str = "default_user",
        caller_scope: Optional[Any] = None,
    ) -> AgentRunSession:
        # FIX-202 & CR-02: Fail-closed authentication check & immutable ExecutionScope propagation
        if caller_scope is not None:
            if not getattr(caller_scope, "authenticated", False):
                raise PermissionError("unauthenticated_runtime_entry")
            service_id = caller_scope.service_id or service_id
            owner_id = caller_scope.owner_id or owner_id
            tenant_id = caller_scope.tenant_id or settings.AGTOOLS_TENANT
            is_authenticated = True
        else:
            if settings.ENVIRONMENT.lower() == "production":
                raise PermissionError("caller_scope_required_in_production")
            tenant_id = "public_tenant" if service_id == "hd2ai-public" else settings.AGTOOLS_TENANT
            is_authenticated = True

        # HF-06: Atomic synchronous admission check & placeholder reservation inside lock (ZERO await inside lock!)
        async with self._lock:
            self.evict_expired_sessions()
            active_count = (
                sum(1 for s in self.sessions.values() if s.state.status in ("queued", "running"))
                + len(self._reserved_run_ids)
            )
            if active_count >= self.max_active_runs:
                raise RuntimeError("active_run_limit_exceeded")
            run_id = f"run-{uuid.uuid4().hex[:16]}"
            self._reserved_run_ids.add(run_id)

        committed = False
        try:
            opts = req.options
            refs = list(dict.fromkeys(req.context_refs))
            if len(refs) > 10 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ref) for ref in refs):
                raise DocumentReferenceError("Invalid document references")
            if service_id == "hd2ai-public" and len(refs) > 2:
                raise DocumentReferenceError("Too many public document references")

            # HF-02: Propagate verified ExecutionScope into Document Reference pre-validation (OUTSIDE lock!)
            from app.core.auth import current_execution_scope

            owner_token = document_owner_id.set(owner_id)
            scope_token = current_execution_scope.set(caller_scope) if caller_scope is not None else None
            titles = {}
            try:
                for ref in refs:
                    outline = await document_adapter.get_outline(ref)
                    if outline.get("error") or outline.get("status") != "completed":
                        raise DocumentReferenceError(f"Document '{ref}' is unavailable or not yet parsed")
                    titles[ref] = outline.get('document_name') or '첨부 문서'
            finally:
                if scope_token is not None:
                    current_execution_scope.reset(scope_token)
                document_owner_id.reset(owner_token)

            active_tool_names = [t.name for t in registry.list_tools(service=service_id)]
            messages = [GatewayMessage(role="system", content=ContextAdapter.build_system_message(active_tool_names))]
            if refs:
                messages.append(GatewayMessage(
                    role="system",
                    content=("사용자가 참조한 AGTools 문서 ID: " + ", ".join(refs) +
                             ". 문서 내용을 추측하지 말고 document.search, document.read_section 등으로 확인한 후 답하세요. "
                             "문서 도구에는 이 목록의 doc_id만 사용하세요."),
                ))
            for message in req.history:
                if message.get("role") in ("user", "assistant"):
                    messages.append(GatewayMessage(role=message["role"], content=message.get("content", "")))
            messages.append(GatewayMessage(role="user", content=req.message))

            initial_state = AgentState(
                run_id=run_id,
                session_id=req.session_id,
                service_id=service_id,
                owner_id=owner_id,
                tenant_id=tenant_id,
                authenticated=is_authenticated,
                execution_scope=caller_scope,
                context_refs=refs,
                document_titles=titles,
                messages=messages,
                max_steps=min(opts.max_steps, get_runtime_config().max_agent_steps) if opts and opts.max_steps else get_runtime_config().max_agent_steps,
                max_tool_calls=min(opts.max_tool_calls, get_runtime_config().max_tool_calls) if opts and opts.max_tool_calls else get_runtime_config().max_tool_calls,
                stream=req.stream,
                reasoning_effort=opts.reasoning_effort if opts else None,
                status="queued",
                custom_client=custom_client,
            )
            session = AgentRunSession(initial_state)
            async with self._lock:
                self.sessions[run_id] = session
                self._reserved_run_ids.discard(run_id)
                committed = True
            return session
        finally:
            if not committed:
                self._reserved_run_ids.discard(run_id)

    async def start_run(self, session: AgentRunSession):
        """Spawns the LangGraph execution task in the background."""
        task = asyncio.create_task(self._run_loop(session))
        session.task = task
        return session

    async def cancel_run(self, run_id: str) -> bool:
        session = self.get_session(run_id)
        if not session or not session.task or session.task.done():
            return False
        session.task.cancel()
        if session.fence.try_finalize("cancelled"):
            session.state.status = "cancelled"
            session.finished_at = time.time()
            await session.emit("run.cancelled", {"message": "User requested cancellation"})
        return True

    async def _run_loop(self, session: AgentRunSession):
        state = session.state
        state.status = "running"
        await session.emit("run.started", {
            "executor": "agent",
            "agent_id": "general-agent",
            "session_id": state.session_id,
            "max_steps": state.max_steps,
            "max_tool_calls": state.max_tool_calls,
        })
        
        timeout_seconds = get_runtime_config().run_timeout_seconds
        run_deadline = HardDeadline(timeout_seconds)
        start_time = time.time()

        try:
            from app.runtime.langgraph_r1 import LangGraphR1Runtime
            from app.runtime.policy.constraints import ConstraintSpec
            from app.tracing.store import get_trajectory_logger

            allowed_tool_set = {t.name for t in registry.list_tools(service=state.service_id)}
            constraints = ConstraintSpec.from_runtime(
                service_id=state.service_id,
                owner_id=state.owner_id,
                authenticated=bool(state.authenticated),
                allowed_tools=allowed_tool_set,
                max_steps=state.max_steps,
                max_tool_calls=state.max_tool_calls,
                time_budget_seconds=run_deadline.remaining_seconds(),
                context_budget_tokens=settings.CONTEXT_BUDGET_TOKENS,
                default_reasoning_effort=state.reasoning_effort or "default",
            )
            traj_logger = get_trajectory_logger() if settings.TRAJECTORY_ENABLED else None
            r1_runtime = LangGraphR1Runtime(trajectory_logger=traj_logger)
            async with asyncio.timeout(run_deadline.remaining_seconds()):
                r1_res = await r1_runtime.execute(
                    state,
                    constraints,
                    emit=session.emit,
                    deadline=run_deadline,
                    fence=session.fence,
                )
            if state.status == "cancelled" or session.fence.terminal_state == "cancelled":
                if session.finished_at is None:
                    session.finished_at = time.time()
                return
            session.finished_at = time.time()
            if state.status == "completed":
                session.fence.try_finalize("completed")
                await session.emit("run.completed", {
                    "citations": state.citations,
                    "answer_kind": "answer",
                    "answer": state.final_answer or "",
                    "model_label": state.model_label,
                    "total_steps": state.step_count,
                    "total_tool_calls": state.tool_call_count,
                    "duration_seconds": round(session.finished_at - start_time, 2),
                    "terminal_status": r1_res.get("terminal_status") or "completed",
                    "fallback_applied": bool(r1_res.get("fallback_applied_any", False)),
                    "reason_code": r1_res.get("last_fallback_reason"),
                    "runtime_engine": "langgraph_r1",
                })
            else:
                session.fence.try_finalize("failed")
                err_info = state.error or {}
                state.error = {
                    "code": err_info.get("code") or "terminated",
                    "message": err_info.get("message") or "에이전트 실행이 중단되었습니다.",
                }
                await session.emit("run.failed", {
                    **state.error,
                    "terminal_status": r1_res.get("terminal_status") or "failed",
                    "fallback_applied": bool(r1_res.get("fallback_applied_any", False)),
                    "reason_code": r1_res.get("last_fallback_reason"),
                    "runtime_engine": "langgraph_r1",
                })
            return

        except asyncio.CancelledError:
            if session.fence.try_finalize("cancelled"):
                state.status = "cancelled"
                session.finished_at = time.time()
                await session.emit("run.cancelled", {"message": "Execution cancelled."})
        except TimeoutError:
            if session.fence.try_finalize("failed"):
                state.status = "failed"
                state.error = {"code": "run_timeout", "message": f"전체 실행 제한 시간({timeout_seconds}초)을 초과했습니다."}
                session.finished_at = time.time()
                await session.emit("run.failed", state.error)
        except DocumentReferenceError as exc:
            if session.fence.try_finalize("failed"):
                state.status = "failed"
                state.error = {"code": str(exc), "message": "도구 예산 또는 답변 검증으로 작업을 완료하지 못했습니다."}
                session.finished_at = time.time()
                await session.emit("run.failed", state.error)
        except GatewayStreamError as exc:
            if session.fence.try_finalize("failed"):
                state.status = 'failed'
                state.error = {'code':'gateway_' + exc.code, 'message':'모델 게이트웨이 요청을 완료하지 못했습니다 (' + exc.code + ').'}
                session.finished_at = time.time()
                await session.emit('run.failed', state.error)
        except Exception as e:
            if session.fence.try_finalize("failed"):
                state.status = "failed"
                empty_answer = isinstance(e, RuntimeError) and str(e) == "Public document answer was empty"
                provider_status = re.match(r"Model Provider Error \((\d{3})\)", str(e))
                state.error = {
                    "code": "model_empty_response" if empty_answer else "internal_error",
                    "message": ("모델이 문서 답변을 반환하지 않았습니다." if empty_answer
                                else f"Model Provider Error ({provider_status.group(1)})" if provider_status
                                else "에이전트 실행 중 내부 오류가 발생했습니다."),
                }
                session.finished_at = time.time()
                await session.emit("run.failed", state.error)
        finally:
            if not session.event_bus.is_finalized:
                session.event_bus.publish_terminal("run.failed", state.status, state.error or {"code": "terminated", "message": "실행이 종료되었습니다."})
            self.evict_expired_sessions()

    def open_event_stream(self, run_id: str, *, last_event_id: Optional[str] = None) -> AsyncGenerator[str, None]:
        """HF-05: Atomically validates cursor & registers subscriber synchronously before StreamingResponse."""
        session = self.get_session(run_id)
        if not session:
            async def _empty() -> AsyncGenerator[str, None]:
                if False:
                    yield ""
            return _empty()
        return session.event_bus.open_stream(last_event_id=last_event_id)

    async def stream_events(self, run_id: str, *, last_event_id: Optional[str] = None) -> AsyncGenerator[str, None]:
        session = self.get_session(run_id)
        if not session:
            return
        async for frame in session.event_bus.stream(last_event_id=last_event_id):
            yield frame

runner = AgentRunner()
