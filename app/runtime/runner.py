"""Agent Runner with Lifecycle, SSE Event Streaming, and Cancellation."""
import asyncio
from app.gateway.client import GatewayStreamError
import time
import uuid
import re
from typing import Any, AsyncGenerator, Dict, Optional, Set
from app.config import settings
from app.core.runtime_config import get_runtime_config
from app.runtime.state import AgentState
from app.schemas.agent import AgentRunRequest, AgentRunResponse, ToolExecutionTrace
from app.schemas.events import SSEEvent
from app.schemas.gateway import GatewayMessage
from app.adapters.document import document_adapter, document_owner_id

class DocumentReferenceError(ValueError):
    pass

class AgentRunSession:
    def __init__(self, state: AgentState):
        self.state = state
        self.queue: asyncio.Queue[Optional[SSEEvent]] = asyncio.Queue(maxsize=128)
        self.task: Optional[asyncio.Task] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self._event_counter = 0

    def next_event_id(self) -> str:
        self._event_counter += 1
        return f"{self.state.run_id}-{self._event_counter}"

    async def emit(self, event_name: str, data: Dict[str, Any]):
        evt = SSEEvent(
            run_id=self.state.run_id,
            event_id=self.next_event_id(),
            event=event_name, # type: ignore
            status=self.state.status,
            data=data,
        )
        await self.queue.put(evt)

class AgentRunner:
    def __init__(self):
        self.sessions: Dict[str, AgentRunSession] = {}
        self._lock = asyncio.Lock()

    def get_session(self, run_id: str) -> Optional[AgentRunSession]:
        return self.sessions.get(run_id)

    async def create_run(
        self,
        req: AgentRunRequest,
        service_id: str = "hd2ai-admin",
        custom_client: Optional[Any] = None,
        owner_id: str = "default_user",
    ) -> AgentRunSession:
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        opts = req.options
        refs = list(dict.fromkeys(req.context_refs))
        if len(refs) > 10 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ref) for ref in refs):
            raise DocumentReferenceError("Invalid document references")
        if service_id == "hd2ai-public" and len(refs) > 2:
            raise DocumentReferenceError("Too many public document references")
        owner_token = document_owner_id.set(owner_id)
        titles = {}
        try:
            for ref in refs:
                outline = await document_adapter.get_outline(ref)
                if outline.get("error") or outline.get("status") != "completed":
                    raise DocumentReferenceError(f"Document '{ref}' is unavailable or not yet parsed")
                titles[ref] = outline.get('document_name') or '첨부 문서'
        finally:
            document_owner_id.reset(owner_token)

        messages = []
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
        self.sessions[run_id] = session
        return session

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
        session.state.status = "cancelled"
        await session.emit("run.cancelled", {"message": "User requested cancellation"})
        await session.queue.put(None)
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
        start_time = time.time()

        try:
            async with asyncio.timeout(timeout_seconds):
                # We execute the graph step-by-step to emit fine-grained SSE events
                while state.step_count < state.max_steps and state.status == "running":
                    await session.emit("step.started", {"step": state.step_count + 1})
                    await session.emit("model.started", {"step": state.step_count + 1, 'reasoning_effort': state.reasoning_effort or 'default'})
                    
                    # 1. Delta callback for streaming user answer tokens
                    answer_parts = []
                    has_streamed_delta = False
                    is_pseudo_tool = False
                    async def on_delta(token: str):
                        nonlocal has_streamed_delta, is_pseudo_tool
                        answer_parts.append(token)
                        if "<tool_call" in token or "<function" in token or re.search(r"<\s*(?:tool_call|function\b)", "".join(answer_parts), re.I):
                            is_pseudo_tool = True
                        if not is_pseudo_tool and not state.pending_tool_calls:
                            has_streamed_delta = True
                            await session.emit("answer.delta", {"delta": token})

                    async def on_reasoning(token: str):
                        await session.emit('reasoning.delta', {'delta': token})

                    # 2. Invoke Model Node
                    from app.runtime.graph import model_node, tool_node
                    model_res = await model_node(state, on_delta=on_delta, on_reasoning_delta=on_reasoning)
                    for k, v in model_res.items():
                        setattr(state, k, v)
                    await session.emit('model.completed', {'step': state.step_count, 'reasoning_effort': state.reasoning_effort or 'default'})

                    if state.pending_tool_calls and state.tool_call_count + len(state.pending_tool_calls) > state.max_tool_calls:
                        raise DocumentReferenceError("tool_call_budget_exceeded")
                    if not state.pending_tool_calls:
                        answer = state.final_answer or "".join(answer_parts)
                        if state.citations:
                            from app.runtime.tasks.presentation import legacy_citations
                            answer, _ = legacy_citations({'answer':answer, 'document_titles':state.document_titles,
                                'entries':[{'doc_id':c['doc_id'],'chunk_id':c['chunk_id'],'source':{'page':c.get('page')}} for c in state.citations]})
                            state.final_answer = answer
                        if re.search(r"<\s*(?:tool_call|function\b)", answer, re.I):
                            raise DocumentReferenceError("model_tool_syntax_in_answer")
                        if answer:
                            if not has_streamed_delta:
                                await session.emit("answer.delta", {"delta": answer})
                            else:
                                citation_tail = answer[len("".join(answer_parts)):]
                                if citation_tail:
                                    await session.emit("answer.delta", {"delta": citation_tail})

                    # 3. If model requested tools, invoke Tool Node
                    if state.pending_tool_calls and state.tool_call_count < state.max_tool_calls:
                        async def on_tool_event(event_type: str, item: Any, step: int):
                            if event_type == "started":
                                await session.emit("tool.started", {
                                    "step": step,
                                    "tool_name": item.function.name,
                                    "tool_call_id": item.id,
                                    "arguments": item.function.arguments,
                                })
                            elif event_type == "completed":
                                await session.emit("tool.completed", {
                                    "step": step,
                                    "tool_name": item.name,
                                    "tool_call_id": item.tool_call_id,
                                    "duration_ms": item.trace.duration_ms,
                                    "result_size_bytes": item.trace.result_size_bytes,
                                    "is_error": item.is_error,
                                    "status": "failed" if item.is_error else "completed",
                                })

                        tool_res = await tool_node(state, on_tool_event=on_tool_event)
                        for k, v in tool_res.items():
                            setattr(state, k, v)
                            
                        await session.emit("step.completed", {
                            "step": state.step_count,
                            "tool_calls_executed": len(tool_res.get("traces", [])),
                        })
                    else:
                        # No tools needed or reached limit; model produced final response
                        await session.emit("step.completed", {"step": state.step_count})
                        break

            if state.pending_tool_calls or not (state.final_answer or "").strip():
                raise DocumentReferenceError("agent_answer_incomplete")
            # Completed successfully
            state.status = "completed"
            session.finished_at = time.time()
            await session.emit("run.completed", {
                'citations': state.citations, 'answer_kind': 'answer',
                "answer": state.final_answer or "",
                "total_steps": state.step_count,
                "total_tool_calls": state.tool_call_count,
                "duration_seconds": round(session.finished_at - start_time, 2),
            })

        except asyncio.CancelledError:
            state.status = "cancelled"
            session.finished_at = time.time()
            await session.emit("run.cancelled", {"message": "Execution cancelled."})
        except TimeoutError:
            state.status = "failed"
            state.error = {"code": "run_timeout", "message": f"전체 실행 제한 시간({timeout_seconds}초)을 초과했습니다."}
            session.finished_at = time.time()
            await session.emit("run.failed", state.error)
        except DocumentReferenceError as exc:
            state.status = "failed"
            state.error = {"code": str(exc), "message": "도구 예산 또는 답변 검증으로 작업을 완료하지 못했습니다."}
            session.finished_at = time.time()
            await session.emit("run.failed", state.error)
        except GatewayStreamError as exc:
            state.status = 'failed'
            state.error = {'code':'gateway_' + exc.code, 'message':'모델 게이트웨이 요청을 완료하지 못했습니다 (' + exc.code + ').'}
            session.finished_at = time.time()
            await session.emit('run.failed', state.error)
        except Exception as e:
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
            await session.queue.put(None)

    async def stream_events(self, run_id: str) -> AsyncGenerator[str, None]:
        session = self.get_session(run_id)
        if not session:
            return
        
        while True:
            try:
                evt = await asyncio.wait_for(session.queue.get(), timeout=2.0)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue

            if evt is None:
                break
            yield evt.to_sse()

runner = AgentRunner()
