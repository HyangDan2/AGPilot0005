"""Minimal MCP server over stdio, used by ``tests/test_r3_fix01_hardening.py``.

It speaks newline-delimited JSON-RPC 2.0 exactly like a real MCP server:

    initialize -> notifications/initialized -> tools/list -> tools/call
    resources/list -> resources/read
    prompts/list -> prompts/get
    ping

The implementation is deliberately tiny and stdlib-only so the test exercises the real
transport (subprocess + pipes) instead of a double.
"""
from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the argument back",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "purge",
        "description": "Delete everything (destructive)",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
        "annotations": {"destructiveHint": True},
    },
]

RESOURCES = [{"uri": "memo://1", "name": "memo", "mimeType": "text/plain"}]
PROMPTS = [{"name": "summarise", "description": "summarise a memo", "arguments": [{"name": "uri"}]}]


def _respond(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id, result):
    _respond({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code, message):
    _respond({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            _result(request_id, {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "fixture-stdio", "version": "0.0.1"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _result(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                _result(request_id, {
                    "content": [{"type": "text", "text": json.dumps({"echo": arguments.get("text")})}],
                    "isError": False,
                })
            elif name == "purge":
                _result(request_id, {"content": [{"type": "text", "text": "purged"}], "isError": False})
            elif name == "slow":
                time.sleep(float(arguments.get("seconds", 2)))
                _result(request_id, {"content": [{"type": "text", "text": "slow done"}], "isError": False})
            else:
                _error(request_id, -32602, f"unknown tool {name}")
        elif method == "resources/list":
            _result(request_id, {"resources": RESOURCES})
        elif method == "resources/read":
            _result(request_id, {"contents": [{"uri": params.get("uri"), "text": "memo body"}]})
        elif method == "prompts/list":
            _result(request_id, {"prompts": PROMPTS})
        elif method == "prompts/get":
            _result(request_id, {
                "description": "summarise",
                "messages": [{"role": "user", "content": {"type": "text", "text": "summarise it"}}],
            })
        elif method == "ping":
            _result(request_id, {})
        elif request_id is not None:
            _error(request_id, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()
