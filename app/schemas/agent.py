"""Agent Execution REST API Request/Response Schemas."""
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

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
    context_refs: List[str] = Field(default_factory=list)
    stream: bool = False
    options: Optional[AgentRunOptions] = None
    history: List[Dict[str, str]] = Field(default_factory=list, max_length=16)
    analysis_refs: List[str] = Field(default_factory=list, max_length=10)

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
