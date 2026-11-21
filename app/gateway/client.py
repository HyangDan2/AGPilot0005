"""AI Gateway Adapter and Protocol Client."""
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union
import httpx
from app.config import settings
from app.gateway.streaming import SSEStreamParser, ToolCallAccumulator
from app.gateway.wire import ToolNameCodec
from app.schemas.gateway import (
    GatewayChatRequest,
    GatewayMessage,
    ToolCall,
    ToolDefinition,
    ModelReply,
)

class GatewayStreamError(RuntimeError):
    """Stable gateway code only; never include a provider body or credentials."""
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,60}', code) else 'stream_failed'
        super().__init__('gateway_' + self.code)


def _separate_think_tags(text: Optional[str]) -> Tuple[str, str]:
    """Strip any <think>...</think> or unclosed <think>... blocks from content.
    Returns (clean_content, extracted_reasoning).
    """
    if not text:
        return "", ""
    reasoning_parts: List[str] = []

    def _collect(match: re.Match) -> str:
        reasoning_parts.append(match.group(1))
        return ""

    cleaned = re.sub(r"<think>([\s\S]*?)</think>", _collect, text, flags=re.I)
    unclosed = re.search(r"<think>([\s\S]*)$", cleaned, flags=re.I)
    if unclosed:
        reasoning_parts.append(unclosed.group(1))
        cleaned = cleaned[:unclosed.start()]
    return cleaned, "".join(reasoning_parts)


def safe_model_label(value):
    if isinstance(value, str) and '://' not in value and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}', value):
        return value
    return None


class AIGatewayClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        socket_path: Optional[str] = None,
        token: Optional[str] = None,
        route: Optional[str] = None,
        mock_mode: bool = False,
    ):
        self.base_url = (base_url or settings.GATEWAY_URL).rstrip("/")
        self.socket_path = socket_path or settings.GATEWAY_SOCKET
        self.route = route or settings.GATEWAY_ROUTE
        self.token = token or settings.GATEWAY_TOKEN
        self.mock_mode = mock_mode

    def _get_transport(self) -> Optional[httpx.AsyncHTTPTransport]:
        if os.path.exists(self.socket_path):
            return httpx.AsyncHTTPTransport(uds=self.socket_path)
        return None

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def execute(
        self,
        messages: List[GatewayMessage],
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = "auto",
        temperature: float = 0.2,
        stream: bool = False,
        on_content_delta: Optional[Any] = None,
        on_reasoning_delta: Optional[Any] = None,
        timeout_seconds: Optional[float] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        task_instructions: Optional[str] = None,
        include_persona: Optional[bool] = None,
        response_format: Optional[Dict[str, Any]] = None,
        _structured: bool = False,
        on_model_label=None,
    ) -> Tuple[Optional[str], List[ToolCall], str]:
        """
        Executes a completion through AI Gateway.
        Returns:
            Tuple[Optional[str], List[ToolCall], finish_reason]
        """
        if self.mock_mode:
            result = await self._mock_completion(messages, tools, on_content_delta)
            return ModelReply(content=result[0], tool_calls=result[1], finish_reason=result[2]) if _structured else result

        url = f"{self.base_url}/internal/{self.route}/chat/completions"
        transport = self._get_transport()

        codec = ToolNameCodec(tools)
        req_body = {
            "model": self.route,
            "messages": codec.messages(messages),
            "stream": stream,
            "temperature": temperature,
        }
        if max_tokens is not None:
            req_body["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            req_body["reasoning_effort"] = reasoning_effort
        if task_instructions is not None:
            req_body["task_instructions"] = task_instructions
        if include_persona is not None:
            req_body["include_persona"] = include_persona
        if response_format is not None:
            req_body["response_format"] = response_format
        if tools:
            req_body["tools"] = codec.tools(tools)
            if tool_choice:
                req_body["tool_choice"] = tool_choice

        try:
            async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout_seconds or settings.RUN_TIMEOUT_SECONDS, connect=5)) as client:
                if stream:
                    return await self._stream_request(client, url, req_body, on_content_delta, codec, on_reasoning_delta, _structured, on_model_label)
                else:
                    return await self._unary_request(client, url, req_body, codec, structured=_structured, on_reasoning_delta=on_reasoning_delta, on_model_label=on_model_label)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, httpx.HTTPError, OSError) as e:
            if not self.mock_mode and not settings.ALLOW_MOCK_FALLBACK:
                raise
            # Fallback to simulated execution if mock mode or explicitly allowed in local dev/test environment
            result = await self._mock_completion(messages, tools, on_content_delta)
            return ModelReply(content=result[0], tool_calls=result[1], finish_reason=result[2]) if _structured else result

    async def complete(self, **kwargs) -> ModelReply:
        """Typed nonstreaming port; legacy execute tuple contract stays compatible."""
        stream = kwargs.pop('stream', False)
        return await self.execute(**kwargs, stream=stream, _structured=True)

    async def _unary_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: Dict[str, Any],
        codec: ToolNameCodec,
        structured: bool = False,
        on_reasoning_delta: Optional[Any] = None,
        on_model_label=None,
    ) -> Tuple[Optional[str], List[ToolCall], str]:
        r = await client.post(url, json=body, headers=self._get_headers())
        r.raise_for_status()
        data = r.json()
        model_label = safe_model_label(data.get("model_label"))
        if on_model_label:
            on_model_label(model_label)
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        raw_content = msg.get("content") if isinstance(msg.get("content"), str) else None
        clean_content, tag_reasoning = _separate_think_tags(raw_content)
        reasoning_field = msg.get("reasoning_content") or msg.get("thinking") or ""
        if not isinstance(reasoning_field, str):
            reasoning_field = ""
        full_reasoning = reasoning_field + tag_reasoning
        if full_reasoning and on_reasoning_delta and callable(on_reasoning_delta):
            res = on_reasoning_delta(full_reasoning)
            if asyncio.iscoroutine(res):
                await res
        content = clean_content if clean_content.strip() else None
        finish_reason = choice.get("finish_reason") or "stop"
        
        tool_calls = []
        if "tool_calls" in msg and msg["tool_calls"]:
            for tc in msg["tool_calls"]:
                tool_calls.append(ToolCall(**tc))
        decoded = codec.decode_calls(tool_calls)
        if structured:
            return ModelReply(
                content=content,
                tool_calls=decoded,
                finish_reason=finish_reason,
                context_truncated=bool(data.get("context_truncated")),
                usage=data.get("usage"),
                reasoning_length=len(full_reasoning),
                stream_incomplete=False,
                model_label=model_label,
            )
        return content, decoded, finish_reason

    async def _stream_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: Dict[str, Any],
        on_content_delta: Optional[Any],
        codec: ToolNameCodec,
        on_reasoning_delta=None,
        structured=False,
        on_model_label=None,
    ) -> Tuple[Optional[str], List[ToolCall], str]:
        accumulator = ToolCallAccumulator()
        full_content = ""
        finish_reason = "stop"
        context_truncated = False
        usage = None
        reasoning_length = 0
        saw_done = False
        model_label = None

        async with client.stream("POST", url, json=body, headers=self._get_headers()) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                parsed = SSEStreamParser.parse_line(line)
                if not parsed:
                    continue
                evt_type, payload = parsed
                if evt_type == "[DONE]":
                    saw_done = True
                    break
                if not payload:
                    continue
                if payload.get('error'):
                    raise GatewayStreamError(payload.get('code'))
                context_truncated = context_truncated or bool(payload.get('context_truncated'))
                usage = payload.get('usage') or usage
                if payload.get('gateway_complete'):
                    model_label = safe_model_label(payload.get('model_label'))

                choice = payload.get("choices", [{}])[0] if payload.get("choices") else {}
                delta = choice.get("delta", {})
                reasoning = delta.get('reasoning_content') or delta.get('thinking')
                if isinstance(reasoning, str) and reasoning:
                    reasoning_length += len(reasoning)
                    if on_reasoning_delta:
                        result = on_reasoning_delta(reasoning)
                        if asyncio.iscoroutine(result):
                            await result
                if "content" in delta and isinstance(delta["content"], str) and delta["content"]:
                    chunk = delta["content"]
                    full_content += chunk
                    if on_content_delta and callable(on_content_delta):
                        if asyncio.iscoroutinefunction(on_content_delta):
                            await on_content_delta(chunk)
                        else:
                            on_content_delta(chunk)

                if "tool_calls" in delta and delta["tool_calls"]:
                    accumulator.feed_delta(delta["tool_calls"])

                if "finish_reason" in choice and choice["finish_reason"]:
                    finish_reason = choice["finish_reason"]

        clean_content, tag_reasoning = _separate_think_tags(full_content)
        if tag_reasoning:
            reasoning_length += len(tag_reasoning)
            if on_reasoning_delta:
                result = on_reasoning_delta(tag_reasoning)
                if asyncio.iscoroutine(result):
                    await result
        final_text = clean_content if clean_content.strip() else None

        tool_calls = codec.decode_calls(accumulator.get_tool_calls())
        if not saw_done:
            finish_reason = "stream_interrupted"
        elif tool_calls and (not finish_reason or finish_reason == "stop"):
            finish_reason = "tool_calls"
        model_label = model_label if saw_done else None
        if on_model_label:
            on_model_label(model_label)
        if structured:
            return ModelReply(
                content=final_text,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                context_truncated=context_truncated,
                usage=usage,
                reasoning_length=reasoning_length,
                stream_incomplete=not saw_done,
                model_label=model_label,
            )
        return final_text, tool_calls, finish_reason

    async def _mock_completion(
        self,
        messages: List[GatewayMessage],
        tools: Optional[List[ToolDefinition]],
        on_content_delta: Optional[Any],
    ) -> Tuple[Optional[str], List[ToolCall], str]:
        """Deterministic Mock logic for test coverage & standalone verification."""
        await asyncio.sleep(0.01)
        last_msg = messages[-1] if messages else None
        
        # 1. Check if last message was a tool result
        if last_msg and last_msg.role == "tool":
            # Synthesize final answer from tool result
            tool_content = last_msg.content or ""
            answer = f"도구 실행 결과에 따른 안내입니다: {tool_content}"
            if on_content_delta:
                await on_content_delta(answer)
            return answer, [], "stop"

        # 2. Check user prompt keywords to trigger tools
        user_text = ""
        for m in reversed(messages):
            if m.role == "user" and m.content:
                user_text = m.content
                break

        if "날짜" in user_text or "시간" in user_text or "datetime" in user_text:
            return None, [
                ToolCall(
                    id="call_datetime_01",
                    function={"name": "system.get_datetime", "arguments": json.dumps({"timezone": "Asia/Seoul"})},
                )
            ], "tool_calls"

        if any(op in user_text for op in ["계산", "+", "-", "*", "/", "sqrt"]):
            expr = "25 * 40"
            for token in user_text.split():
                if any(c in token for c in "+-*/0123456789"):
                    expr = token
                    break
            return None, [
                ToolCall(
                    id="call_math_01",
                    function={"name": "math.calculate", "arguments": json.dumps({"expression": expr})},
                )
            ], "tool_calls"

        if "개요" in user_text or "목차" in user_text or "outline" in user_text:
            return None, [
                ToolCall(
                    id="call_doc_01",
                    function={"name": "document.get_outline", "arguments": json.dumps({"doc_id": "hd2lab-guide"})},
                )
            ], "tool_calls"

        # General conversation (No tools needed)
        greeting = "안녕하세요! HD2Lab 범용 에이전트입니다. 무엇을 도와드릴까요?"
        if on_content_delta:
            await on_content_delta(greeting)
        return greeting, [], "stop"

    async def get_route_limits(self, route: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch active token limits for a route from AI Gateway."""
        if self.mock_mode:
            return None
        target = (route or self.route or "").strip()
        if not target:
            return None
        url = f"{self.base_url}/internal/{target}/limits"
        transport = self._get_transport()
        try:
            async with httpx.AsyncClient(transport=transport, timeout=5.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code != 200:
                    return None
                data = resp.json()
                if isinstance(data, dict) and "context_tokens" in data and "max_output_tokens" in data:
                    return data
        except Exception:
            return None
        return None

gateway_client = AIGatewayClient()
