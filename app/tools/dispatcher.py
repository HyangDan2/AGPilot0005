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


def _validate_args_against_schema(args: Any, schema: Optional[Dict[str, Any]]) -> Optional[str]:
    """R1-FIX-12 B: JSON Schema validation for tool arguments (object, required, types, bounds, enum)."""
    if not isinstance(args, dict):
        return "Tool arguments must be a JSON object."
    if not isinstance(schema, dict):
        return None
    required = schema.get("required") or []
    for req_key in required:
        if req_key not in args or args[req_key] is None:
            return f"Missing required argument: '{req_key}'"
    props = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        extra_keys = [k for k in args.keys() if k not in props]
        if extra_keys:
            return f"Unexpected argument(s): {', '.join(sorted(extra_keys))}"
    for k, v in args.items():
        if k not in props or v is None:
            continue
        spec = props[k] or {}
        expected = spec.get("type")
        if expected == "string" and not isinstance(v, str):
            return f"Argument '{k}' must be a string."
        if expected == "integer" and (isinstance(v, bool) or not isinstance(v, int)):
            return f"Argument '{k}' must be an integer."
        if expected == "number" and (isinstance(v, bool) or not isinstance(v, (int, float))):
            return f"Argument '{k}' must be a number."
        if expected == "boolean" and not isinstance(v, bool):
            return f"Argument '{k}' must be a boolean."
        if expected == "array" and not isinstance(v, list):
            return f"Argument '{k}' must be an array."
        if expected == "object" and not isinstance(v, dict):
            return f"Argument '{k}' must be an object."
        if "enum" in spec and v not in spec["enum"]:
            return f"Argument '{k}' must be one of {spec['enum']}."
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in spec and v < spec["minimum"]:
                return f"Argument '{k}' must be >= {spec['minimum']}."
            if "maximum" in spec and v > spec["maximum"]:
                return f"Argument '{k}' must be <= {spec['maximum']}."
    return None


def _apply_dynamic_tool_budget(
    reg: Any,
    args: Dict[str, Any],
    input_budget_tokens: Optional[int],
    remaining_input_tokens: Optional[int],
    batch_size: int,
) -> Tuple[Dict[str, Any], int]:
    """R1-FIX-12 A: Schema-driven dynamic tool I/O budget using reg.parameters and strict remaining budget."""
    default_max_bytes = settings.MAX_TOOL_RESULT_TOKENS * 4
    if not input_budget_tokens or input_budget_tokens <= 0:
        return args, default_max_bytes

    # Never restore full input_budget_tokens when remaining_input_tokens == 0!
    if remaining_input_tokens is not None:
        remaining = max(0, int(remaining_input_tokens))
    else:
        remaining = int(input_budget_tokens)

    effective_batch = max(1, int(batch_size))
    if remaining <= 0:
        # FIX-206: Zero remaining budget -> allocate 0 new tool result bytes (no fixed floor!)
        return dict(args), 0

    # Strict batch bound: sum of per-tool allocations NEVER exceeds remaining budget
    per_tool_tokens = min(
        remaining // effective_batch,
        max(1, int(input_budget_tokens * 0.4)),
    )
    dynamic_max_chars = max(0, min(80000, int(per_tool_tokens * 2.0)))
    dynamic_max_bytes = per_tool_tokens * 4

    params = {}
    try:
        schema = getattr(reg, "parameters", None)
        if isinstance(schema, dict):
            params = schema.get("properties") or {}
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
        
        # 1. Parse arguments (must be JSON object per R1-FIX-12 B)
        try:
            raw_args = call.function.arguments
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args if raw_args is not None else {})
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be a JSON object.")
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

        # 2.2 Validate arguments against JSON Schema (R1-FIX-12 B)
        schema_err = _validate_args_against_schema(args, getattr(reg, "parameters", None))
        if schema_err:
            trace.status = "failed"
            trace.error_code = "invalid_arguments"
            trace.error_message = schema_err
            error_json = json.dumps({"error": True, "code": "invalid_arguments", "message": schema_err}, ensure_ascii=False)
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
            
            # Check result size (dynamic per-tool budget ceiling) — preserve valid JSON structure (FIX-206)
            raw_bytes = len(res_str.encode("utf-8"))
            if dynamic_max_bytes <= 0:
                res_str = json.dumps(
                    {
                        "truncated": True,
                        "budget_exhausted": True,
                        "original_bytes": raw_bytes,
                    },
                    ensure_ascii=False,
                )
            elif raw_bytes > dynamic_max_bytes:
                preview_limit = max(64, dynamic_max_bytes - 180)
                res_str = json.dumps(
                    {
                        "truncated": True,
                        "original_bytes": raw_bytes,
                        "content_preview": _truncate_utf8_bytes(res_str, preview_limit),
                    },
                    ensure_ascii=False,
                )

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
        constraints: Optional[Any] = None,
    ) -> List[DispatchResult]:
        """Executes read-only tool calls concurrently and side-effect calls sequentially (R1-FIX-12 C)."""
        batch_size = max(1, len(calls))
        read_only_indices: List[int] = []
        side_effect_indices: List[int] = []
        for idx, call in enumerate(calls):
            reg = self.registry.get_registration(call.function.name)
            if reg is None or reg.is_read_only:
                read_only_indices.append(idx)
            else:
                side_effect_indices.append(idx)

        results: List[Optional[DispatchResult]] = [None] * len(calls)
        if read_only_indices:
            ro_tasks = [
                self.execute_tool_call(
                    calls[i],
                    step=step,
                    service=service,
                    timeout=timeout,
                    allowed_document_ids=allowed_document_ids,
                    input_budget_tokens=input_budget_tokens,
                    remaining_input_tokens=remaining_input_tokens,
                    batch_size=batch_size,
                    constraints=constraints,
                )
                for i in read_only_indices
            ]
            ro_res = await asyncio.gather(*ro_tasks)
            for i, r in zip(read_only_indices, ro_res):
                results[i] = r

        for i in side_effect_indices:
            results[i] = await self.execute_tool_call(
                calls[i],
                step=step,
                service=service,
                timeout=timeout,
                allowed_document_ids=allowed_document_ids,
                input_budget_tokens=input_budget_tokens,
                remaining_input_tokens=remaining_input_tokens,
                batch_size=batch_size,
                constraints=constraints,
            )

        return [r for r in results if r is not None]

dispatcher = ToolDispatcher()
