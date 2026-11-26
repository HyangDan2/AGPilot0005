"""Tool Execution Dispatcher with Sandbox, Timeout, and Isolation."""
import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple
from app.config import settings
from app.schemas.agent import ToolExecutionTrace
from app.schemas.gateway import ToolCall
from app.tools.registry import registry
from app.tools.policy import tool_policy

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


def _truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _apply_dynamic_tool_budget(
    reg: Any,
    args: Dict[str, Any],
    input_budget_tokens: Optional[int],
    remaining_input_tokens: Optional[int],
    batch_size: int,
) -> Tuple[Dict[str, Any], int]:
    """Schema-driven dynamic tool I/O budget allocation (no tool-name hardcoding)."""
    default_max_bytes = settings.MAX_TOOL_RESULT_TOKENS * 4
    if not input_budget_tokens or input_budget_tokens <= 0:
        return args, default_max_bytes

    remaining = remaining_input_tokens if (remaining_input_tokens and remaining_input_tokens > 0) else input_budget_tokens
    per_tool_tokens = max(2048, remaining // max(2, batch_size + 1))
    # Cap per-tool payload to at most 40% of total route input budget to preserve synthesis headroom
    per_tool_tokens = min(per_tool_tokens, max(2048, int(input_budget_tokens * 0.4)))
    dynamic_max_chars = max(2000, min(80000, int(per_tool_tokens * 2.0)))
    dynamic_max_bytes = max(default_max_bytes, per_tool_tokens * 4)

    params = {}
    try:
        params = (reg.definition.function.parameters or {}).get("properties") or {}
    except Exception:
        params = {}

    if not params:
        return args, dynamic_max_bytes

    updated = dict(args)

    def _schema_bounds(prop_name: str, lo: int, hi: int) -> Tuple[int, int]:
        spec = params.get(prop_name) or {}
        s_lo = spec.get("minimum")
        s_hi = spec.get("maximum")
        return (
            max(lo, int(s_lo)) if isinstance(s_lo, (int, float)) else lo,
            min(hi, int(s_hi)) if isinstance(s_hi, (int, float)) else hi,
        )

    if "max_chars" in params:
        lo, hi = _schema_bounds("max_chars", 500, 100000)
        target = max(lo, min(hi, dynamic_max_chars))
        if updated.get("max_chars") is None:
            updated["max_chars"] = target
        else:
            try:
                updated["max_chars"] = max(lo, min(target, int(updated["max_chars"])))
            except Exception:
                updated["max_chars"] = target

    if "max_snippet_chars" in params:
        lo, hi = _schema_bounds("max_snippet_chars", 100, 4000)
        target = max(lo, min(hi, max(300, min(2000, dynamic_max_chars // 15))))
        if updated.get("max_snippet_chars") is None:
            updated["max_snippet_chars"] = target
        else:
            try:
                updated["max_snippet_chars"] = max(lo, min(target, int(updated["max_snippet_chars"])))
            except Exception:
                updated["max_snippet_chars"] = target

    for count_key in ("limit", "top_k"):
        if count_key in params and updated.get(count_key) is None:
            lo, hi = _schema_bounds(count_key, 1, 50)
            scaled = 10 if input_budget_tokens >= 64000 else (6 if input_budget_tokens >= 20000 else 4)
            updated[count_key] = max(lo, min(hi, scaled))

    return updated, dynamic_max_bytes

class ToolDispatcher:
    def __init__(self, tool_registry=None):
        self.registry = tool_registry or registry

    async def execute_tool_call(
        self,
        call: ToolCall,
        step: int = 1,
        service: Optional[str] = None,
        timeout: Optional[float] = None,
        allowed_document_ids: Optional[List[str]] = None,
        input_budget_tokens: Optional[int] = None,
        remaining_input_tokens: Optional[int] = None,
        batch_size: int = 1,
        constraints: Optional[Any] = None,
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

        if func_name.startswith("document.") and allowed_document_ids is not None and args.get("doc_id") not in allowed_document_ids:
            trace.status = "failed"
            trace.error_code = "document_not_referenced"
            trace.error_message = "Document is not in this run's authorized references."
            return DispatchResult(call_id, func_name, json.dumps({"error": True, "code": trace.error_code}), trace, is_error=True)

        # 2. Check registration and ACL
        reg = self.registry.get_registration(func_name)
        handler = self.registry.get_handler(func_name)
        if not reg or not handler:
            trace.status = "failed"
            trace.error_code = "tool_not_found"
            trace.error_message = f"도구 '{func_name}'을(를) 찾을 수 없습니다."
            error_json = json.dumps({"error": True, "code": "tool_not_found", "message": trace.error_message}, ensure_ascii=False)
            return DispatchResult(call_id, func_name, error_json, trace, is_error=True)

        if constraints is not None:
            allowed, deny_reason = constraints.check_tool_permission(func_name)
        else:
            allowed, deny_reason = tool_policy.is_tool_allowed(func_name, service)
        if not allowed or (service and reg.allowed_services and service not in reg.allowed_services):
            trace.status = "failed"
            trace.error_code = "permission_denied"
            trace.error_message = f"서비스 '{service}'는 도구 '{func_name}'에 대한 권한이 없습니다 ({deny_reason or 'acl'})."
            error_json = json.dumps({"error": True, "code": "permission_denied", "message": trace.error_message}, ensure_ascii=False)
            return DispatchResult(call_id, func_name, error_json, trace, is_error=True)

        # 2.5 Apply schema-driven dynamic I/O budget
        args, dynamic_max_bytes = _apply_dynamic_tool_budget(
            reg, args, input_budget_tokens, remaining_input_tokens, batch_size
        )
        trace.arguments = args

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
            if isinstance(result, dict) and result.get("error") is True:
                trace.status = "failed"
                trace.duration_ms = duration_ms
                trace.error_code = str(result.get("code") or "document_error")
                trace.error_message = str(result.get("message") or "Document tool failed")
                return DispatchResult(call_id, func_name, json.dumps(result, ensure_ascii=False), trace, is_error=True)
            res_str = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
            
            # Check result size (dynamic per-tool budget ceiling)
            if len(res_str.encode("utf-8")) > dynamic_max_bytes:
                res_str = _truncate_utf8_bytes(res_str, dynamic_max_bytes) + "\n...[도구 출력 결과가 컨텍스트 예산 상한을 초과하여 잘렸습니다.]"

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
        allowed_document_ids: Optional[List[str]] = None,
        input_budget_tokens: Optional[int] = None,
        remaining_input_tokens: Optional[int] = None,
    ) -> List[DispatchResult]:
        """Executes multiple tool calls concurrently if they are read-only."""
        batch_size = max(1, len(calls))
        tasks = [
            self.execute_tool_call(
                call,
                step=step,
                service=service,
                timeout=timeout,
                allowed_document_ids=allowed_document_ids,
                input_budget_tokens=input_budget_tokens,
                remaining_input_tokens=remaining_input_tokens,
                batch_size=batch_size,
            )
            for call in calls
        ]
        return await asyncio.gather(*tasks)

dispatcher = ToolDispatcher()
