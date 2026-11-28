"""FIX-03 Real MCP transports (JSON-RPC 2.0).

Two transports implement the MCP client side without the optional ``mcp`` SDK, which is
not installed in the R3 environment:

``StdioMCPClient``
    Spawns the server with :func:`asyncio.create_subprocess_exec` and speaks
    newline-delimited JSON-RPC on stdin/stdout (``initialize`` ->
    ``notifications/initialized`` -> ``tools/list`` / ``tools/call`` /
    ``resources/*`` / ``prompts/*``).  Closing terminates the process (SIGTERM) and kills
    it if it does not exit.

``StreamableHTTPMCPClient``
    POSTs JSON-RPC to one endpoint with ``httpx.AsyncClient``, accepts either a JSON body
    or a ``text/event-stream`` response, carries the ``Mcp-Session-Id`` returned by
    ``initialize`` on every later request (and the ``MCP-Protocol-Version`` header), and
    ``DELETE``s the session when it closes.

Both raise :class:`MCPTransportError` (or :class:`MCPTransportTimeout` for a lost answer)
so the manager can degrade a server instead of crashing a workflow.  The module holds no
credentials: an :class:`CredentialStore` reference is resolved by
``app.workflow.mcp`` and passed in as a header mapping.
"""
from __future__ import annotations

import asyncio
import json
import shlex
from collections import deque
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "MCPTransportError",
    "MCPTransportTimeout",
    "JsonRpcSession",
    "StdioMCPClient",
    "StreamableHTTPMCPClient",
    "PROTOCOL_VERSION",
    "CLIENT_INFO",
]

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO: Dict[str, str] = {"name": "agcore-r3-workflow", "version": "1.0.0"}


class MCPTransportError(RuntimeError):
    """A JSON-RPC level failure (transport closed, server error, malformed frame)."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


class MCPTransportTimeout(MCPTransportError):
    """The request was sent but no answer arrived: the remote side may have run it."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("mcp_timeout", detail)


class JsonRpcSession:
    """Bookkeeping shared by both transports (ids, initialize handshake state)."""

    def __init__(self) -> None:
        self._next_id = 100
        self.initialized = False
        self.server_info: Dict[str, Any] = {}
        self.server_capabilities: Dict[str, Any] = {}
        self.protocol_version: Optional[str] = None

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    @staticmethod
    def initialize_params() -> Dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
            "clientInfo": dict(CLIENT_INFO),
        }

    def accept_initialize(self, result: Any) -> None:
        if not isinstance(result, Mapping):
            raise MCPTransportError("invalid_initialize_result", type(result).__name__)
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.server_info = dict(result.get("serverInfo") or {})
        self.server_capabilities = dict(result.get("capabilities") or {})
        self.initialized = True

    @staticmethod
    def raise_for_error(message: Mapping[str, Any]) -> None:
        error = message.get("error")
        if not error:
            return
        if isinstance(error, Mapping):
            code = str(error.get("code") or "mcp_server_error")
            detail = str(error.get("message") or "")[:400]
        else:  # pragma: no cover - nonconforming server
            code, detail = "mcp_server_error", str(error)[:400]
        raise MCPTransportError(f"mcp_rpc_{code}", detail)


def _text_content(payload: Any) -> Any:
    """Normalise an MCP ``tools/call`` result into plain Python data.

    The wire format is ``{"content":[{"type":"text","text":"..."}], "isError": bool}``.
    Text parts that contain JSON are decoded so the trajectory stays structured; anything
    else keeps the raw envelope.
    """
    if not isinstance(payload, Mapping):
        return payload
    if payload.get("isError"):
        raise MCPTransportError("mcp_tool_error", jsonlib_dumps(payload.get("content"))[:400])
    content = payload.get("content")
    if not isinstance(content, list):
        return dict(payload)
    if len(content) == 1 and isinstance(content[0], Mapping) and content[0].get("type") == "text":
        text = content[0].get("text")
        if isinstance(text, str):
            stripped = text.strip()
            if stripped[:1] in ("{", "["):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    return text
            return text
    return {"content": list(content), "structuredContent": payload.get("structuredContent")}


def jsonlib_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


class StdioMCPClient:
    """MCP client over a child process' stdin/stdout (newline-delimited JSON-RPC)."""

    def __init__(
        self,
        command: str,
        *,
        timeout_seconds: float = 15.0,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        max_stderr_lines: int = 200,
    ) -> None:
        self.command = command
        self.timeout = float(timeout_seconds)
        self._env = dict(env) if env else None
        self._cwd = cwd
        self._max_stderr_lines = max_stderr_lines
        self.session = JsonRpcSession()
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stderr: Deque[str] = deque(maxlen=max_stderr_lines)
        self._stderr_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        #: Exit status of the server process once :meth:`close` returned (None = still running).
        self.returncode: Optional[int] = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # -- lifecycle ---------------------------------------------------------------------
    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process is not None else None

    @property
    def stderr_tail(self) -> List[str]:
        return list(self._stderr)

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is not None and self._process.returncode is None:
            return self._process
        argv = shlex.split(self.command)
        if not argv:
            raise MCPTransportError("mcp_command_required")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
            )
        except (OSError, ValueError) as exc:
            raise MCPTransportError("mcp_command_failed", f"{argv[0]}: {type(exc).__name__}") from exc
        if self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))
        return self._process

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                self._stderr.append(line.decode("utf-8", "replace").rstrip("\n")[:400])
        except Exception:  # noqa: BLE001 - diagnostics only
            return

    async def initialize(self) -> Dict[str, Any]:
        result = await self._request("initialize", self.session.initialize_params())
        self.session.accept_initialize(result)
        await self._notify("notifications/initialized", {})
        return self.session.server_info

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    process.kill()
                except ProcessLookupError:  # pragma: no cover - already gone
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except asyncio.TimeoutError:  # pragma: no cover
                    pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        self.returncode = process.returncode

    # -- frames ------------------------------------------------------------------------
    async def _write(self, payload: Mapping[str, Any]) -> None:
        process = await self._ensure_process()
        if process.stdin is None:  # pragma: no cover - subprocess without stdin
            raise MCPTransportError("mcp_stdin_unavailable")
        blob = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            process.stdin.write(blob)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise MCPTransportError("mcp_stdin_closed", type(exc).__name__) from exc

    async def _read_message(self) -> Dict[str, Any]:
        process = await self._ensure_process()
        if process.stdout is None:  # pragma: no cover
            raise MCPTransportError("mcp_stdout_unavailable")
        while True:
            line = await process.stdout.readline()
            if not line:
                returncode = process.returncode
                raise MCPTransportError("mcp_stream_closed", f"exit={returncode}")
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError as exc:
                raise MCPTransportError("mcp_invalid_frame", text[:200]) from exc
            if isinstance(message, Mapping):
                return dict(message)

    async def _send(self, payload: Mapping[str, Any], *, expect_id: Optional[int]) -> Dict[str, Any]:
        await self._write(payload)
        if expect_id is None:
            return {}
        deadline = asyncio.get_running_loop().time() + self.timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise MCPTransportTimeout(f"{payload.get('method')} after {self.timeout}s")
            try:
                message = await asyncio.wait_for(self._read_message(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise MCPTransportTimeout(f"{payload.get('method')} after {self.timeout}s") from exc
            if message.get("id") != expect_id:
                # Server-initiated notification or a stale answer: ignore and keep reading.
                continue
            self.session.raise_for_error(message)
            return dict(message.get("result") or {})

    async def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        request_id = self.session.next_id()
        message = await self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
            expect_id=request_id,
        )
        return message

    async def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)}, expect_id=None)

    # -- MCP surface -------------------------------------------------------------------
    async def list_tools(self) -> Sequence[Mapping[str, Any]]:
        result = await self._request("tools/list", {})
        return list((result or {}).get("tools") or ())

    async def list_resources(self) -> Sequence[Mapping[str, Any]]:
        result = await self._request("resources/list", {})
        return list((result or {}).get("resources") or ())

    async def list_prompts(self) -> Sequence[Mapping[str, Any]]:
        result = await self._request("prompts/list", {})
        return list((result or {}).get("prompts") or ())

    async def read_resource(self, uri: str) -> Any:
        result = await self._request("resources/read", {"uri": uri})
        return result

    async def get_prompt(self, name: str, arguments: Optional[Mapping[str, Any]] = None) -> Any:
        return await self._request("prompts/get", {"name": name, "arguments": dict(arguments or {})})

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        result = await self._request("tools/call", {"name": name, "arguments": dict(arguments or {})})
        return _text_content(result)

    async def ping(self) -> bool:
        try:
            await self._request("ping", {})
        except MCPTransportError:
            return False
        return True


class StreamableHTTPMCPClient:
    """MCP ``streamable-http`` client (JSON body or SSE framed JSON-RPC)."""

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 15.0,
        headers: Optional[Mapping[str, str]] = None,
        client: Any = None,
    ) -> None:
        self.url = url
        self.timeout = float(timeout_seconds)
        self.session = JsonRpcSession()
        self._headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self._client = client
        self._owns_client = client is None
        self.session_id: Optional[str] = None

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _request_headers(self) -> Dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": self.session.protocol_version or PROTOCOL_VERSION,
        }
        headers.update(self._headers)
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    async def initialize(self) -> Dict[str, Any]:
        request_id = self.session.next_id()
        result, response_headers = await self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": self.session.initialize_params()},
            request_id=request_id,
        )
        self.session.accept_initialize(result)
        session_id = _header(response_headers, "mcp-session-id")
        if session_id:
            self.session_id = session_id
        # The spec's notification carries no id; servers that answer it with 202 are fine.
        await self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, request_id=None,
        )
        return self.session.server_info

    async def close(self) -> None:
        try:
            if self.session_id or True:
                client = await self._http()
                await client.delete(self.url, headers=self._request_headers())
        except Exception:  # noqa: BLE001 - cleanup must never raise
            pass
        finally:
            if self._owns_client and self._client is not None:
                try:
                    await self._client.aclose()
                except Exception:  # noqa: BLE001
                    pass
                self._client = None
            self.session_id = None

    # -- transport ---------------------------------------------------------------------
    async def _post(self, payload: Mapping[str, Any], *, request_id: Optional[int]) -> tuple:
        import httpx

        client = await self._http()
        try:
            response = await client.post(self.url, json=dict(payload), headers=self._request_headers())
        except httpx.TimeoutException as exc:
            raise MCPTransportTimeout(f"{payload.get('method')} after {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise MCPTransportError("mcp_http_error", type(exc).__name__) from exc
        if response.status_code >= 500:
            raise MCPTransportError("mcp_http_unavailable", f"status={response.status_code}")
        if response.status_code >= 400:
            raise MCPTransportError("mcp_http_rejected", f"status={response.status_code}")
        if request_id is None:
            return {}, response.headers
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/event-stream" in content_type:
            result = _parse_sse_result(response.text, request_id)
            return result, response.headers
        try:
            message = response.json()
        except ValueError as exc:
            raise MCPTransportError("mcp_invalid_frame", response.text[:200]) from exc
        if isinstance(message, list):
            message = next((item for item in message if isinstance(item, Mapping) and item.get("id") == request_id), None)
        if not isinstance(message, Mapping):
            raise MCPTransportError("mcp_invalid_frame", "response_without_matching_id")
        self.session.raise_for_error(message)
        return dict(message.get("result") or {}), response.headers

    async def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        request_id = self.session.next_id()
        result, _headers = await self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}, request_id=request_id,
        )
        return result

    # -- MCP surface -------------------------------------------------------------------
    async def list_tools(self) -> Sequence[Mapping[str, Any]]:
        return list((await self._request("tools/list", {}) or {}).get("tools") or ())

    async def list_resources(self) -> Sequence[Mapping[str, Any]]:
        return list((await self._request("resources/list", {}) or {}).get("resources") or ())

    async def list_prompts(self) -> Sequence[Mapping[str, Any]]:
        return list((await self._request("prompts/list", {}) or {}).get("prompts") or ())

    async def read_resource(self, uri: str) -> Any:
        return await self._request("resources/read", {"uri": uri})

    async def get_prompt(self, name: str, arguments: Optional[Mapping[str, Any]] = None) -> Any:
        return await self._request("prompts/get", {"name": name, "arguments": dict(arguments or {})})

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        return _text_content(await self._request("tools/call", {"name": name, "arguments": dict(arguments or {})}))

    async def ping(self) -> bool:
        try:
            await self._request("ping", {})
        except MCPTransportError:
            return False
        return True


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    for key, value in (headers or {}).items():
        if str(key).lower() == name:
            return str(value)
    return None


def _parse_sse_result(text: str, request_id: int) -> Dict[str, Any]:
    """Read a ``text/event-stream`` body up to the frame answering ``request_id``."""
    data_lines: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                message = _decode_frame(payload)
                if message is not None and message.get("id") == request_id:
                    JsonRpcSession.raise_for_error(message)
                    return dict(message.get("result") or {})
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        message = _decode_frame("\n".join(data_lines))
        if message is not None and message.get("id") == request_id:
            JsonRpcSession.raise_for_error(message)
            return dict(message.get("result") or {})
    raise MCPTransportError("mcp_stream_without_result", f"id={request_id}")


def _decode_frame(payload: str) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return dict(value) if isinstance(value, Mapping) else None
