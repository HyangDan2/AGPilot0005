"""Minimal MCP ``streamable-http`` server for the FIX-03 transport test.

Runs on 127.0.0.1 (loopback) so the test proves the real HTTP transport without any
external dependency:

* ``initialize`` answers JSON and sets ``Mcp-Session-Id``;
* every later request must carry that header (otherwise 400), which proves the client tracks
  the session;
* ``resources/read`` answers as ``text/event-stream`` so the SSE parser is exercised too;
* ``DELETE`` ends the session.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

TOOLS = [
    {
        "name": "lookup",
        "description": "Look up a record",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    }
]


class _State:
    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self.requests: List[Dict[str, Any]] = []
        self.deleted = False
        self.missing_session_attempts = 0
        self.lock = threading.Lock()


def _handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # silence the test log
            return

        def _send(self, status: int, body: str = "", *, content_type: str = "application/json",
                  extra: Optional[Dict[str, str]] = None) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(payload)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_DELETE(self):  # noqa: N802 - http.server API
            state.deleted = True
            self._send(204)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                message = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, json.dumps({"error": "bad json"}))
                return
            method = message.get("method")
            request_id = message.get("id")
            params = message.get("params") or {}
            with state.lock:
                state.requests.append({"method": method, "session": self.headers.get("mcp-session-id")})

            if method == "initialize":
                new_session = "sess-test-1"
                with state.lock:
                    state.session_id = new_session
                self._send(200, json.dumps({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {
                        "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}, "resources": {}},
                        "serverInfo": {"name": "fixture-http", "version": "0.0.1"},
                    },
                }), extra={"mcp-session-id": new_session})
                return

            if self.headers.get("mcp-session-id") != state.session_id:
                with state.lock:
                    state.missing_session_attempts += 1
                self._send(400, json.dumps({"error": "missing session"}))
                return

            if method == "notifications/initialized":
                self._send(202)
                return

            if method == "tools/list":
                self._send(200, json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}))
                return
            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name != "lookup":
                    self._send(200, json.dumps({
                        "jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32602, "message": f"unknown tool {name}"},
                    }))
                    return
                body = {"ok": True, "q": arguments.get("q")}
                self._send(200, json.dumps({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(body)}], "isError": False},
                }))
                return
            if method == "resources/read":
                frame = json.dumps({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"contents": [{"uri": params.get("uri"), "text": "sse body"}]},
                })
                self._send(200, f"event: message\ndata: {frame}\n\n", content_type="text/event-stream")
                return
            if method == "resources/list":
                self._send(200, json.dumps({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"resources": [{"uri": "memo://http", "name": "memo"}]},
                }))
                return
            if method == "prompts/list":
                self._send(200, json.dumps({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"prompts": [{"name": "summarise", "description": "x", "arguments": []}]},
                }))
                return
            if method == "prompts/get":
                self._send(200, json.dumps({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"messages": [{"role": "user", "content": {"type": "text", "text": "s"}}]},
                }))
                return
            if method == "ping":
                self._send(200, json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {}}))
                return
            self._send(200, json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }))

    return Handler


def make_server() -> Tuple[ThreadingHTTPServer, _State, int]:
    state = _State()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    server.daemon_threads = True
    return server, state, int(server.server_address[1])
