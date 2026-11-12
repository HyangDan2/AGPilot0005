"""Direct OpenAI-compatible LLM Provider Client for Demo & Testing."""
import asyncio
import ipaddress
import json
import re
import time
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional, Tuple, Union
import httpx
from app.config import settings
from app.gateway.streaming import SSEStreamParser, ToolCallAccumulator
from app.schemas.gateway import GatewayMessage, ToolCall, ToolDefinition

class SSRFSecurityError(ValueError):
    pass

def validate_base_url(url: str, allow_localhost: bool = False) -> str:
    """Validates and normalizes OpenAI-compatible base URL preventing SSRF."""
    if not url or not isinstance(url, str):
        raise SSRFSecurityError("Base URL을 입력해 주세요.")
    
    url = url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[:-len("/chat/completions")]
    
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFSecurityError("URL 스키마는 http 또는 https만 지원됩니다.")
    if not parsed.hostname:
        raise SSRFSecurityError("유효하지 않은 호스트명입니다.")
    if parsed.username or parsed.password:
        raise SSRFSecurityError("URL에 사용자 인증 정보를 포함할 수 없습니다.")

    host = parsed.hostname.lower()
    
    # Check IP restrictions unless allow_localhost is explicitly True
    try:
        ip = ipaddress.ip_address(host)
        if not allow_localhost:
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise SSRFSecurityError("내부망 또는 로컬 IP 접근은 허용되지 않습니다.")
    except ValueError:
        # Domain name
        if not allow_localhost and (host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local") or host.endswith(".internal")):
            raise SSRFSecurityError("로컬 및 내부 도메인 접근은 허용되지 않습니다.")

    return url

def _to_wire_name(name: str) -> str:
    """Convert dot-separated internal tool names (e.g. system.echo) to OpenAI-compliant names (system_echo)."""
    return name.replace(".", "_")


class DirectLLMClient:
    """Direct client connecting to user-provided OpenAI-compatible model endpoints (e.g. NVIDIA API)."""
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        allow_localhost: bool = True,  # allow localhost for local test environments
        mock_mode: bool = False,
    ):
        self.mock_mode = mock_mode or base_url.strip().lower() == "mock"
        if self.mock_mode:
            self.base_url = "mock"
        else:
            self.base_url = validate_base_url(base_url, allow_localhost=allow_localhost)
        self.api_key = api_key.strip()
        self.model = model.strip()

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def test_connection(self) -> Dict[str, Any]:
        """Performs a minimal ping to verify model connectivity and measure latency."""
        if self.mock_mode:
            return {
                "success": True,
                "status_code": 200,
                "duration_ms": 15,
                "message": "Mock 테스트 모델 연결 성공 (15ms)",
            }
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(url, json=payload, headers=self._get_headers())
                duration_ms = int((time.monotonic() - start) * 1000)
                if r.status_code == 200:
                    return {
                        "success": True,
                        "status_code": 200,
                        "duration_ms": duration_ms,
                        "message": f"연결 성공 ({duration_ms}ms)",
                    }
                else:
                    try:
                        err_json = r.json()
                        detail = err_json.get("error", {}).get("message") or err_json.get("detail") or str(err_json)
                    except Exception:
                        detail = r.text[:200]
                    return {
                        "success": False,
                        "status_code": r.status_code,
                        "duration_ms": duration_ms,
                        "message": f"오류 ({r.status_code}): {detail}",
                    }
        except httpx.ConnectError:
            return {"success": False, "status_code": 503, "message": "호스트에 연결할 수 없습니다. URL을 확인해 주세요."}
        except httpx.TimeoutException:
            return {"success": False, "status_code": 504, "message": "연결 제한 시간(15초)을 초과했습니다."}
        except Exception as e:
            return {"success": False, "status_code": 500, "message": f"연결 실패: {str(e)}"}

    async def execute(
        self,
        messages: List[GatewayMessage],
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = "auto",
        temperature: float = 0.2,
        stream: bool = False,
        on_content_delta: Optional[Any] = None,
    ) -> Tuple[Optional[str], List[ToolCall], str]:
        """Executes completion directly against the target endpoint."""
        if self.mock_mode:
            from app.gateway.client import gateway_client
            return await gateway_client._mock_completion(messages, tools, on_content_delta)

        url = f"{self.base_url}/chat/completions"
        wire_to_internal: Dict[str, str] = {}
        if tools:
            wire_to_internal = {_to_wire_name(t.function.name): t.function.name for t in tools}

        # Serialize messages
        formatted_msgs = []
        for m in messages:
            msg_dict: Dict[str, Any] = {"role": m.role}
            if m.content is not None:
                msg_dict["content"] = m.content
            if m.tool_calls:
                serialized_tcs = []
                for tc in m.tool_calls:
                    tc_dict = tc.model_dump(exclude_none=True)
                    if "function" in tc_dict and "name" in tc_dict["function"]:
                        tc_dict["function"]["name"] = _to_wire_name(tc_dict["function"]["name"])
                    serialized_tcs.append(tc_dict)
                msg_dict["tool_calls"] = serialized_tcs
            if m.tool_call_id:
                msg_dict["tool_call_id"] = m.tool_call_id
            if m.name:
                msg_dict["name"] = _to_wire_name(m.name)
            formatted_msgs.append(msg_dict)

        req_body: Dict[str, Any] = {
            "model": self.model,
            "messages": formatted_msgs,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            wire_tools = []
            for t in tools:
                t_dict = t.model_dump(exclude_none=True)
                if "function" in t_dict and "name" in t_dict["function"]:
                    t_dict["function"]["name"] = _to_wire_name(t_dict["function"]["name"])
                wire_tools.append(t_dict)
            req_body["tools"] = wire_tools
            if tool_choice:
                req_body["tool_choice"] = tool_choice

        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.RUN_TIMEOUT_SECONDS, connect=10)) as client:
            if stream:
                return await self._stream_request(client, url, req_body, on_content_delta, wire_to_internal)
            else:
                return await self._unary_request(client, url, req_body, wire_to_internal)

    async def _unary_request(
        self, client: httpx.AsyncClient, url: str, body: Dict[str, Any], wire_to_internal: Optional[Dict[str, str]] = None
    ) -> Tuple[Optional[str], List[ToolCall], str]:
        r = await client.post(url, json=body, headers=self._get_headers())
        if r.status_code != 200:
            try:
                err_body = r.json()
                msg = err_body.get("error", {}).get("message") or str(err_body)
            except Exception:
                msg = r.text[:300]
            raise RuntimeError(f"Model Provider Error ({r.status_code}): {msg}")

        data = r.json()
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content")
        finish_reason = choice.get("finish_reason", "stop")
        
        tool_calls = []
        if "tool_calls" in msg and msg["tool_calls"]:
            for tc in msg["tool_calls"]:
                parsed_tc = ToolCall(**tc)
                if wire_to_internal and parsed_tc.function.name in wire_to_internal:
                    parsed_tc.function.name = wire_to_internal[parsed_tc.function.name]
                tool_calls.append(parsed_tc)
        return content, tool_calls, finish_reason

    async def _stream_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: Dict[str, Any],
        on_content_delta: Optional[Any],
        wire_to_internal: Optional[Dict[str, str]] = None,
    ) -> Tuple[Optional[str], List[ToolCall], str]:
        accumulator = ToolCallAccumulator()
        full_content = ""
        finish_reason = "stop"

        async with client.stream("POST", url, json=body, headers=self._get_headers()) as response:
            if response.status_code != 200:
                raw = await response.aread()
                try:
                    err_body = json.loads(raw)
                    msg = err_body.get("error", {}).get("message") or str(err_body)
                except Exception:
                    msg = raw.decode("utf-8", errors="ignore")[:300]
                raise RuntimeError(f"Model Provider Error ({response.status_code}): {msg}")

            async for line in response.aiter_lines():
                parsed = SSEStreamParser.parse_line(line)
                if not parsed:
                    continue
                evt_type, payload = parsed
                if evt_type == "[DONE]":
                    break
                if not payload:
                    continue

                choice = payload.get("choices", [{}])[0] if payload.get("choices") else {}
                delta = choice.get("delta", {})
                if "content" in delta and delta["content"]:
                    chunk = delta["content"]
                    full_content += chunk
                    if on_content_delta and callable(on_content_delta):
                        await on_content_delta(chunk)

                if "tool_calls" in delta and delta["tool_calls"]:
                    accumulator.feed_delta(delta["tool_calls"])

                if "finish_reason" in choice and choice["finish_reason"]:
                    finish_reason = choice["finish_reason"]

        tool_calls = accumulator.get_tool_calls()
        if wire_to_internal:
            for tc in tool_calls:
                if tc.function.name in wire_to_internal:
                    tc.function.name = wire_to_internal[tc.function.name]
        if tool_calls and (not finish_reason or finish_reason == "stop"):
            finish_reason = "tool_calls"
        return full_content if full_content else None, tool_calls, finish_reason
