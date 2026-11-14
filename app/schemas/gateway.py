"""AI Gateway message schemas and Tool Calling representations."""
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field

class FunctionCall(BaseModel):
    name: str
    arguments: str

class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall

class GatewayMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[str] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None

class ToolFunctionDefinition(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any]

class ToolDefinition(BaseModel):
    type: Literal["function"] = "function"
    function: ToolFunctionDefinition

class GatewayChatRequest(BaseModel):
    model: str
    messages: List[GatewayMessage]
    tools: Optional[List[ToolDefinition]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 4096
    stream: bool = False


class ModelUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ModelReply(BaseModel):
    content: Optional[str] = None
    tool_calls: List[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    context_truncated: bool = False
    usage: Optional[ModelUsage] = None
