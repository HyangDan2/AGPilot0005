"""Agent Runtime State Definition for LangGraph."""
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field
from app.schemas.agent import ToolExecutionTrace
from app.schemas.gateway import GatewayMessage, ToolCall

class AgentState(BaseModel):
    run_id: str
    session_id: Optional[str] = None
    service_id: Optional[str] = None
    
    # Message history
    messages: List[GatewayMessage] = Field(default_factory=list)
    
    # Step & Budget counters
    step_count: int = 0
    tool_call_count: int = 0
    max_steps: int = 10
    max_tool_calls: int = 6
    
    # State tracking
    status: Literal["queued", "running", "completed", "failed", "cancelled"] = "queued"
    pending_tool_calls: List[ToolCall] = Field(default_factory=list)
    final_answer: Optional[str] = None
    error: Optional[Dict[str, str]] = None
    
    # Traces & Events
    traces: List[ToolExecutionTrace] = Field(default_factory=list)
    
    # Stream flag
    stream: bool = False
    
    # Custom client for Direct Demo Adapter
    custom_client: Optional[Any] = None

    model_config = {"arbitrary_types_allowed": True}
