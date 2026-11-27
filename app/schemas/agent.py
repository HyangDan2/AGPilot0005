"""Agent Execution REST API Request/Response Schemas."""
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator

class AgentRunOptions(BaseModel):
    reasoning_effort: Optional[Literal['off', 'on', 'low', 'medium', 'high', 'default']] = None
    max_steps: Optional[int] = None
    max_tool_calls: Optional[int] = None
    temperature: Optional[float] = None
    timeout_seconds: Optional[float] = None

class AgentRunRequest(BaseModel):
    agent_id: str = "general-agent"
    message: str
    session_id: Optional[str] = None
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    context_refs: List[str] = Field(default_factory=list)
    stream: bool = False
    options: Optional[AgentRunOptions] = None
    history: List[Dict[str, str]] = Field(default_factory=list, max_length=16)
    analysis_refs: List[str] = Field(default_factory=list, max_length=10)
    workspace_id: Optional[str] = None
    execution_mode: Optional[Literal["chat", "workspace", "document_analysis"]] = None
    approvals: List[str] = Field(default_factory=list, max_length=32)

    @field_validator("approvals")
    @classmethod
    def _validate_approvals(cls, values: List[str]) -> List[str]:
        cleaned: List[str] = []
        for raw in values or []:
            if not isinstance(raw, str):
                raise ValueError("approval token must be a string")
            tok = raw.strip()
            if not tok or len(tok) > 256 or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in tok):
                raise ValueError("invalid approval token")
            if tok not in cleaned:
                cleaned.append(tok)
        return cleaned

class ToolExecutionTrace(BaseModel):
    step: int
    tool_name: str
    tool_call_id: str
    arguments: Dict[str, Any]
    status: Literal["running", "completed", "failed"]
    duration_ms: Optional[int] = None
    result_size_bytes: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

class AgentRunResponse(BaseModel):
    model_label: Optional[str] = None
    run_id: str
    agent_id: str
    session_id: Optional[str] = None
    status: Literal["queued", "planning", "running", "verifying", "completed", "needs_input", "partial", "paused", "failed", "cancelled"]
    answer: Optional[str] = None
    traces: List[ToolExecutionTrace] = Field(default_factory=list)
    usage: Dict[str, Any] = Field(default_factory=dict)
    created_at: float
    finished_at: Optional[float] = None
    error: Optional[Dict[str, str]] = None
