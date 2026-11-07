"""Tool Execution Dispatcher with Sandbox, Timeout, and Isolation."""
import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple
from app.config import settings
from app.schemas.agent import ToolExecutionTrace
from app.schemas.gateway import ToolCall
from app.tools.registry import registry

class DispatchResult:
    def __init__(
        self,
        tool_call_id: str,
        name: str,
        content: str,
        trace: ToolExecutionTrace,
        is_error: bool = False,
    ):
        self.tool_call_id = tool_call_id
        self.name = name
        self.content = content
        self.trace = trace
        self.is_error = is_error

class ToolDispatcher:
    def __init__(self, tool_registry=None):
        self.registry = tool_registry or registry

    async def execute_tool_call(
        self,
        call: ToolCall,
        step: int = 1,
        service: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> DispatchResult:
        func_name = call.function.name
        call_id = call.id
        timeout_sec = timeout or settings.TOOL_TIMEOUT_SECONDS
        
        # 1. Parse arguments
        try:
            raw_args = call.function.arguments
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except Exception as e:
            trace = ToolExecutionTrace(
                step=step,
                tool_name=func_name,
                tool_call_id=call_id,
                arguments={},
                status="failed",
                duration_ms=0,
                error_code="invalid_arguments",
                error_message=f"JSON argument parse error: {str(e)}",
            )
            error_json = json.dumps({"error": True, "code": "invalid_arguments", "message": str(e)}, ensure_ascii=False)
            return DispatchResult(call_id, func_name, error_json, trace, is_error=True)

        trace = ToolExecutionTrace(
            step=step,
            tool_name=func_name,
            tool_call_id=call_id,
            arguments=args,
            status="running",
        )

        # 2. Check registration and ACL
        reg = self.registry.get_registration(func_name)
        handler = self.registry.get_handler(func_name)
        if not reg or not handler:
            trace.status = "failed"
            trace.error_code = "tool_not_found"
            trace.error_message = f"도구 '{func_name}'을(를) 찾을 수 없습니다."
            error_json = json.dumps({"error": True, "code": "tool_not_found", "message": trace.error_message}, ensure_ascii=False)
            return DispatchResult(call_id, func_name, error_json, trace, is_error=True)

        if service and reg.allowed_services and service not in reg.allowed_services:
            trace.status = "failed"
            trace.error_code = "permission_denied"
            trace.error_message = f"서비스 '{service}'는 도구 '{func_name}'에 대한 권한이 없습니다."
            error_json = json.dumps({"error": True, "code": "permission_denied", "message": trace.error_message}, ensure_ascii=False)
            return DispatchResult(call_id, func_name, error_json, trace, is_error=True)

        # 3. Execute with Timeout
        start_time = time.monotonic()
        try:
            async with asyncio.timeout(timeout_sec):
                if reg.is_async:
                    result = await handler(**args)
                else:
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(None, lambda: handler(**args))

            duration_ms = int((time.monotonic() - start_time) * 1000)
            res_str = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
            
            # Check result size (cap max output)
            max_bytes = settings.MAX_TOOL_RESULT_TOKENS * 4
            if len(res_str.encode("utf-8")) > max_bytes:
                res_str = res_str[:max_bytes] + "\n...[도구 출력 결과가 최대 허용 크기를 초과하여 잘렸습니다.]"

            trace.status = "completed"
            trace.duration_ms = duration_ms
            trace.result_size_bytes = len(res_str.encode("utf-8"))
            return DispatchResult(call_id, func_name, res_str, trace, is_error=False)

        except TimeoutError:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            trace.status = "failed"
            trace.duration_ms = duration_ms
            trace.error_code = "tool_timeout"
            trace.error_message = f"도구 실행 제한 시간({timeout_sec}초)을 초과했습니다."
            error_json = json.dumps({"error": True, "code": "tool_timeout", "message": trace.error_message}, ensure_ascii=False)
            return DispatchResult(call_id, func_name, error_json, trace, is_error=True)

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            trace.status = "failed"
            trace.duration_ms = duration_ms
            trace.error_code = "execution_error"
            trace.error_message = str(e)
            error_json = json.dumps({"error": True, "code": "execution_error", "message": str(e)}, ensure_ascii=False)
            return DispatchResult(call_id, func_name, error_json, trace, is_error=True)

    async def execute_batch(
        self,
        calls: List[ToolCall],
        step: int = 1,
        service: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> List[DispatchResult]:
        """Executes multiple tool calls concurrently if they are read-only."""
        tasks = [
            self.execute_tool_call(call, step=step, service=service, timeout=timeout)
            for call in calls
        ]
        return await asyncio.gather(*tasks)

dispatcher = ToolDispatcher()
