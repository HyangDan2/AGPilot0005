"""Stdio MCP server that reports its own process environment/session (FIX-02 P1 proof).

It exists so the test suite can observe what a spawned MCP server actually receives:

``env``          the environment variables the child was given (names + values)
``session``      its own pid / session id / process-group id, and the parent's, plus
                 ``forked_children`` - a child process it started and left running, so the
                 test can verify that closing the client kills the whole process group.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

TOOLS = [
    {
        "name": "env",
        "description": "Return the process environment",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "session",
        "description": "Return pid/session/process-group information",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
]


def _children() -> list:
    return [str(item) for item in CHILDREN]


CHILDREN: list = []


def _respond(payload) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id, result) -> None:
    _respond({"jsonrpc": "2.0", "id": request_id, "result": result})


def _tools_call(request_id, params) -> None:
    name = str((params or {}).get("name") or "")
    if name == "env":
        payload = dict(os.environ)
        result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    elif name == "session":
        if not CHILDREN:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            CHILDREN.append(child.pid)
        own_sid = os.getsid(0) if hasattr(os, "getsid") else os.getpgrp()
        payload = {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "sid": own_sid,
            "pgid": os.getpgrp(),
            "parent_sid": os.getsid(os.getppid()) if hasattr(os, "getsid") else None,
            "parent_pgid": os.getpgid(os.getppid()),
            "children": _children(),
        }
        result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    else:
        _respond({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": f"unknown tool {name}"}})
        return
    _result(request_id, result)


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
        if method == "initialize":
            _result(request_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "env-server", "version": "1.0.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _result(request_id, {"tools": TOOLS})
        elif method == "resources/list":
            _result(request_id, {"resources": []})
        elif method == "prompts/list":
            _result(request_id, {"prompts": []})
        elif method == "tools/call":
            _tools_call(request_id, message.get("params") or {})
        elif method == "ping":
            _result(request_id, {})
        elif request_id is not None:
            _respond({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32601, "message": "method not found"}})
    for pid in CHILDREN:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    time.sleep(0.05)


if __name__ == "__main__":
    main()
