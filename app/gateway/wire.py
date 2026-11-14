"""Translate internal tool names to provider-compatible wire names."""
from typing import Dict, List, Optional

from app.schemas.gateway import GatewayMessage, ToolCall, ToolDefinition


def to_wire_name(name: str) -> str:
    return name.replace(".", "_")


class ToolNameCodec:
    def __init__(self, tools: Optional[List[ToolDefinition]]):
        self.wire_to_internal: Dict[str, str] = {
            to_wire_name(tool.function.name): tool.function.name for tool in (tools or [])
        }

    def messages(self, messages: List[GatewayMessage]) -> list[dict]:
        encoded = []
        for message in messages:
            item = message.model_dump(exclude_none=True)
            for call in item.get("tool_calls", []):
                call["function"]["name"] = to_wire_name(call["function"]["name"])
            if item.get("name"):
                item["name"] = to_wire_name(item["name"])
            encoded.append(item)
        return encoded

    def tools(self, tools: Optional[List[ToolDefinition]]) -> list[dict]:
        encoded = []
        for tool in tools or []:
            item = tool.model_dump(exclude_none=True)
            item["function"]["name"] = to_wire_name(item["function"]["name"])
            encoded.append(item)
        return encoded

    def decode_calls(self, calls: List[ToolCall]) -> List[ToolCall]:
        for call in calls:
            call.function.name = self.wire_to_internal.get(call.function.name, call.function.name)
        return calls
