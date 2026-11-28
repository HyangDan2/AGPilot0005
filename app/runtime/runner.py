"""Agent Runner with Lifecycle, SSE Event Streaming, and Cancellation."""
import asyncio
from app.tools.registry import registry
from app.tools.adapters import ContextAdapter
from app.gateway.client import GatewayStreamError
import time
import uuid
import re
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple
from app.config import settings
from app.core.runtime_config import get_runtime_config
from app.runtime.state import AgentState
from app.schemas.agent import AgentRunRequest, AgentRunResponse, ToolExecutionTrace
from app.schemas.events import SSEEvent
from app.schemas.gateway import GatewayMessage
from app.adapters.document import document_adapter, document_owner_id
from app.core.session_attachments import ATTACHMENT_RUN_PREFIX, is_session_attachment
from app.runtime.event_bus import RunEventBus
from app.runtime.fence import HardDeadline, TerminalStateFence
from app.knowledge.references import (
    DEFAULT_ENRICHMENT_LIMIT,
    DocumentReference,
    EvidenceTracker,
    ReferenceResolution,
    ReferenceScope,
    SynthesisBudget,
    attachment_reference,
    is_workspace_reference_id,
    resolve_references,
    workspace_reference,
)


def _workspace_inventory(runtime, workspace_id: str, scope, reference_scope: ReferenceScope) -> List[DocumentReference]:
    """Files this conversation's workspace holds — names and revisions only, no content."""

    from app.workspace.tools import relative_path_of

    inventory: List[DocumentReference] = []
    for identity in runtime.manager.list_files(workspace_id, scope, recursive=True):
        if getattr(identity, "kind", None) is not None and getattr(identity.kind, "value", "") != "file":
            continue
        try:
            path = relative_path_of(identity)
        except Exception:
            path = None
        reference = workspace_reference(
            file_id=identity.file_id,
            name=identity.name,
            mime_type=getattr(identity, "mime_type", "") or "application/octet-stream",
            revision=int(getattr(identity, "revision", 1) or 1),
            path=path,
            scope=reference_scope,
            size_bytes=getattr(identity, "size_bytes", None),
        )
        # An inventory entry is available by definition, but not requested until chosen or named.
        inventory.append(reference.model_copy(update={"matched_by": []}))
    return inventory


def _reference_system_message(resolution: ReferenceResolution) -> str:
    lines = [
        "이번 요청에서 사용자가 지정한 자료입니다. 답변 전에 각 자료를 해당 도구로 실제로 읽으세요.",
    ]
    for ref in resolution.requested_refs:
        where = "워크스페이스 파일" if ref.source_type.value == "workspace_file" else "세션 첨부"
        tool = "workspace_retrieve/workspace_read_file" if where == "워크스페이스 파일" else "document.get_outline/read_section/search"
        lines.append(
            f"- [{where}] {ref.name or ref.document_id} (id={ref.document_id}, revision={ref.revision}, "
            f"citation_ref={ref.citation_ref}, 확인 도구={tool})"
        )
    lines.append(
        "자료 내용을 추측하지 말고, 읽은 근거로만 답하세요. 읽지 못한 자료가 있으면 어떤 자료를 왜 읽지 못했는지 답변에 밝히세요."
    )
    if resolution.unresolved:
        lines.append(
            "다음 참조는 이 대화 범위에서 확인되지 않았습니다: "
            + ", ".join(f"{item.mention} ({item.reason})" for item in resolution.unresolved)
        )
    return " ".join(lines)

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

        prefix = ATTACHMENT_RUN_PREFIX if any(is_session_attachment(ref) for ref in req.context_refs) else "run-"
        # HF-06: Atomic synchronous admission check & placeholder reservation inside lock (ZERO await inside lock!)
        async with self._lock:
            self.evict_expired_sessions()
            active_count = (
                sum(1 for s in self.sessions.values() if s.state.status in ("queued", "running"))
                + len(self._reserved_run_ids)
            )
            if active_count >= self.max_active_runs:
                raise RuntimeError("active_run_limit_exceeded")
            run_id = f"{prefix}{uuid.uuid4().hex[:16]}"
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
            from app.core.auth import current_execution_scope, current_run_conversation_id

            workspace_file_refs = [ref for ref in refs if is_workspace_reference_id(ref)]
            if workspace_file_refs and not (req.workspace_id or "").strip():
                raise DocumentReferenceError("workspace_reference_requires_binding")

            owner_token = document_owner_id.set(owner_id)
            scope_token = current_execution_scope.set(caller_scope) if caller_scope is not None else None
            conversation_token = current_run_conversation_id.set(req.conversation_id)
            titles = {}
            try:
                for ref in (item for item in refs if not is_workspace_reference_id(item)):
                    outline = await document_adapter.get_outline(ref)
                    if outline.get("error") or outline.get("status") != "completed":
                        raise DocumentReferenceError(f"Document '{ref}' is unavailable or not yet parsed")
                    titles[ref] = outline.get('document_name') or '첨부 문서'
            finally:
                current_run_conversation_id.reset(conversation_token)
                if scope_token is not None:
                    current_execution_scope.reset(scope_token)
                document_owner_id.reset(owner_token)

            active_tool_names = [t.name for t in registry.list_tools(service=service_id)]
            messages = [GatewayMessage(role="system", content=ContextAdapter.build_system_message(active_tool_names))]
            reference_scope = ReferenceScope(
                workspace_id=(req.workspace_id or "").strip() or None,
                conversation_id=(req.conversation_id or "").strip() or None,
                session_id=req.session_id,
            )
            workspace_inventory: List[DocumentReference] = []
            workspace_missing: List[tuple] = []
            workspace_id: Optional[str] = (req.workspace_id or "").strip() or None
            if workspace_id is not None:
                from app.config import is_workspace_enabled, is_workspace_enabled_for_service

                if not is_workspace_enabled():
                    raise DocumentReferenceError("workspace_disabled")
                if not is_workspace_enabled_for_service(service_id):
                    raise DocumentReferenceError("workspace_disabled_for_service")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace_id):
                    raise DocumentReferenceError("invalid_workspace_id")
                from app.workspace.manager import WorkspaceFailure
                from app.workspace.runtime import get_runtime as _get_workspace_runtime

                ws_scope = caller_scope
                if ws_scope is None:
                    from app.core.auth import ExecutionScope

                    ws_scope = ExecutionScope(
                        authenticated=is_authenticated,
                        auth_source="dev_local",
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        service_id=service_id,
                        role="user",
                    )
                try:
                    ws_identity = _get_workspace_runtime().manager.ensure_open(workspace_id, ws_scope)
                except WorkspaceFailure as exc:
                    raise DocumentReferenceError(exc.error.code.value) from exc
                ws_mode = "읽기 전용" if getattr(ws_identity, "read_only", False) else "읽기/쓰기"
                workspace_details = f"name={ws_identity.name}, mode={ws_mode}."
                if req.bind_workspace:
                    workspace_guidance = "현재 대화의 워크스페이스는 실행 시 자동으로 적용됩니다."
                else:
                    workspace_details = f"workspace_id={workspace_id}, {workspace_details}"
                    workspace_guidance = "이 workspace_id만 사용하세요."
                messages.append(
                    GatewayMessage(
                        role="system",
                        content=(
                            f"활성 워크스페이스 컨텍스트: {workspace_details} "
                            "워크스페이스 파일 탐색·읽기·검색·수정 및 캔버스 연계 시 등록된 도구 스키마를 자율적으로 활용하세요. "
                            f"{workspace_guidance}"
                        ),
                    )
                )
                # WO-R21-ADD-01: the workspace inventory is metadata only — it names what this
                # conversation *may* read. A file becomes `requested` only when the client
                # selected it or the user named it in the message.
                try:
                    workspace_inventory = _workspace_inventory(
                        _get_workspace_runtime(), workspace_id, ws_scope, reference_scope
                    )
                except WorkspaceFailure as exc:
                    raise DocumentReferenceError(exc.error.code.value) from exc
                known_file_ids = {item.document_id for item in workspace_inventory}
                for ref in workspace_file_refs:
                    if ref not in known_file_ids:
                        # A file outside this conversation's workspace is refused, never guessed.
                        raise DocumentReferenceError("workspace_file_not_found")
            if refs:
                messages.append(GatewayMessage(
                    role="system",
                    content=("사용자가 참조한 AGTools 문서 ID: " + ", ".join(refs) +
                             ". 문서 내용을 추측하지 말고 document.search, document.read_section 등으로 확인한 후 답하세요. "
                             "문서 도구에는 이 목록의 doc_id만 사용하세요."),
                ))

            # WO-R21-ADD-01: one reference contract for workspace files and session attachments.
            unified_available: List[DocumentReference] = list(workspace_inventory)
            for ref in (item for item in refs if is_session_attachment(item)):
                unified_available.append(
                    attachment_reference(
                        document_id=ref,
                        name=titles.get(ref, ""),
                        scope=reference_scope,
                    )
                )
            unified_explicit = [item for item in refs if is_session_attachment(item) or is_workspace_reference_id(item)]
            resolution = resolve_references(
                message=req.message,
                explicit_ids=unified_explicit,
                available=unified_available,
                scope=reference_scope,
                missing_explicit=workspace_missing,
            )
            synthesis_reserve = (
                max(256, int(settings.CONTEXT_BUDGET_TOKENS * float(settings.SYNTHESIS_RESERVE_RATIO)))
                if resolution.has_requested else 0
            )
            if resolution.has_requested:
                messages.insert(1, GatewayMessage(role="system", content=_reference_system_message(resolution)))
            evidence = EvidenceTracker(
                resolution.requested_refs,
                enrichment_limit=max(0, int(settings.EVIDENCE_ENRICHMENT_LIMIT)),
            )
            evidence.set_budget(
                SynthesisBudget(
                    reserved_tokens=synthesis_reserve,
                    evidence_tokens=max(0, int(settings.CONTEXT_BUDGET_TOKENS) - synthesis_reserve),
                    total_input_budget_tokens=int(settings.CONTEXT_BUDGET_TOKENS),
                )
            )
            for message in req.history:
                if message.get("role") in ("user", "assistant"):
                    messages.append(GatewayMessage(role=message["role"], content=message.get("content", "")))
            messages.append(GatewayMessage(role="user", content=req.message))

            initial_state = AgentState(
                run_id=run_id,
                session_id=req.session_id,
                conversation_id=(req.conversation_id or "").strip() or None,
                service_id=service_id,
                owner_id=owner_id,
                tenant_id=tenant_id,
                authenticated=is_authenticated,
                execution_scope=caller_scope,
                context_refs=refs,
                workspace_id=workspace_id,
                workspace_bound=req.bind_workspace,
                approvals=list(req.approvals or ()),
                document_titles=titles,
                messages=messages,
                references=resolution,
                evidence=evidence.coverage() if resolution.has_requested else None,
                synthesis_reserve_tokens=synthesis_reserve,
                request_message=req.message,
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
            if state.status in ("completed", "partial"):
                session.fence.try_finalize(state.status)
                coverage = state.evidence
                terminal_payload = {
                    "citations": state.citations,
                    "answer_kind": "answer",
                    "answer": state.final_answer or "",
                    "model_label": state.model_label,
                    "total_steps": state.step_count,
                    "total_tool_calls": state.tool_call_count,
                    "duration_seconds": round(session.finished_at - start_time, 2),
                    "terminal_status": state.status,
                    "fallback_applied": bool(r1_res.get("fallback_applied_any", False)),
                    "reason_code": r1_res.get("last_fallback_reason"),
                    "runtime_engine": "langgraph_r1",
                }
                # WO-R21-ADD-01: a partial answer is still an answer — deliver the secured
                # evidence and the missing references with their reasons, never silently.
                terminal_payload["partial"] = state.status == "partial"
                if state.references is not None:
                    terminal_payload["references"] = state.references.for_report()
                if coverage is not None:
                    terminal_payload["evidence_coverage"] = coverage.for_report()
                    terminal_payload["missing_references"] = coverage.missing_references()
                    terminal_payload["partial_reasons"] = coverage.reasons()
                    terminal_payload["budget"] = coverage.budget.model_dump(mode="json")
                await session.emit("run.completed", terminal_payload)
            else:
                if state.evidence is not None:
                    tracker = EvidenceTracker.from_coverage(state.evidence)
                    tracker.mark_execution_failure((state.error or {}).get("code"))
                    state.evidence = tracker.coverage()
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
