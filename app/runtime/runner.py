"""Agent Runner with Lifecycle, SSE Event Streaming, and Cancellation."""
import asyncio
import time
import uuid
from typing import Any, AsyncGenerator, Dict, Optional, Set
from app.config import settings
from app.runtime.graph import agent_executor
from app.runtime.state import AgentState
from app.schemas.agent import AgentRunRequest, AgentRunResponse, ToolExecutionTrace
from app.schemas.events import SSEEvent
from app.schemas.gateway import GatewayMessage

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

    async def create_run(self, req: AgentRunRequest, service_id: str = "hd2ai-admin") -> AgentRunSession:
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        opts = req.options
        
        initial_state = AgentState(
            run_id=run_id,
            session_id=req.session_id,
            service_id=service_id,
            messages=[GatewayMessage(role="user", content=req.message)],
            max_steps=opts.max_steps if opts and opts.max_steps else settings.MAX_AGENT_STEPS,
            max_tool_calls=opts.max_tool_calls if opts and opts.max_tool_calls else settings.MAX_TOOL_CALLS,
            stream=req.stream,
            status="queued",
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
            "agent_id": "general-agent",
            "session_id": state.session_id,
            "max_steps": state.max_steps,
            "max_tool_calls": state.max_tool_calls,
        })
        
        timeout_seconds = settings.RUN_TIMEOUT_SECONDS
        start_time = time.time()

        try:
            async with asyncio.timeout(timeout_seconds):
                # We execute the graph step-by-step to emit fine-grained SSE events
                while state.step_count < state.max_steps and state.status == "running":
                    await session.emit("step.started", {"step": state.step_count + 1})
                    await session.emit("model.started", {"step": state.step_count + 1})
                    
                    # 1. Delta callback for streaming user answer tokens
                    async def on_delta(token: str):
                        await session.emit("answer.delta", {"delta": token})

                    # 2. Invoke Model Node
                    from app.runtime.graph import model_node, tool_node
                    model_res = await model_node(state, on_delta=on_delta)
                    for k, v in model_res.items():
                        setattr(state, k, v)

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

            # Completed successfully
            state.status = "completed"
            session.finished_at = time.time()
            await session.emit("run.completed", {
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
        except Exception as e:
            state.status = "failed"
            state.error = {"code": "internal_error", "message": str(e)}
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
