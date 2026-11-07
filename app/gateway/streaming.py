"""Streaming Parser and Tool Call Chunk Reassembler."""
import json
from typing import Any, Dict, List, Optional, Tuple
from app.schemas.gateway import FunctionCall, ToolCall

class ToolCallAccumulator:
    """Reassembles fragmented tool call arguments from streaming deltas."""
    def __init__(self):
        self._calls_by_index: Dict[int, Dict[str, Any]] = {}

    def feed_delta(self, tool_call_deltas: List[Dict[str, Any]]):
        for delta in tool_call_deltas:
            idx = delta.get("index", 0)
            if idx not in self._calls_by_index:
                self._calls_by_index[idx] = {
                    "id": delta.get("id", f"call_{idx}"),
                    "type": delta.get("type", "function"),
                    "name": "",
                    "arguments": "",
                }
            
            target = self._calls_by_index[idx]
            if "id" in delta and delta["id"]:
                target["id"] = delta["id"]
            if "type" in delta and delta["type"]:
                target["type"] = delta["type"]
            
            func = delta.get("function", {})
            if "name" in func and func["name"]:
                target["name"] += func["name"]
            if "arguments" in func and func["arguments"]:
                target["arguments"] += func["arguments"]

    def is_empty(self) -> bool:
        return len(self._calls_by_index) == 0

    def get_tool_calls(self) -> List[ToolCall]:
        results = []
        for idx in sorted(self._calls_by_index.keys()):
            item = self._calls_by_index[idx]
            results.append(
                ToolCall(
                    id=item["id"],
                    type=item["type"],
                    function=FunctionCall(
                        name=item["name"],
                        arguments=item["arguments"],
                    ),
                )
            )
        return results

class SSEStreamParser:
    """Parses SSE stream lines and dispatches content deltas and tool calls."""
    @staticmethod
    def parse_line(line: str) -> Optional[Tuple[str, Optional[Dict[str, Any]]]]:
        line = line.strip()
        if not line or line.startswith(":"):
            return None
        if line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                return ("[DONE]", None)
            try:
                return ("data", json.loads(data_str))
            except json.JSONDecodeError:
                return None
        return None
