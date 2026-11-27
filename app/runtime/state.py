"""Agent Runtime State Definition for LangGraph."""
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field
from app.schemas.agent import ToolExecutionTrace
from app.schemas.gateway import GatewayMessage, ToolCall

class AgentState(BaseModel):
    run_id: str
    session_id: Optional[str] = None
    service_id: Optional[str] = None
    owner_id: str = "default_user"
    tenant_id: str = "default_tenant"
    authenticated: bool = False
    execution_scope: Optional[Any] = None
    context_refs: List[str] = Field(default_factory=list)
    workspace_id: Optional[str] = None
    approvals: List[str] = Field(default_factory=list)
    document_titles: Dict[str,str] = Field(default_factory=dict)
    citations: List[Dict[str,Any]] = Field(default_factory=list)
    
    # Message history
    messages: List[GatewayMessage] = Field(default_factory=list)
    
    # Step & Budget counters
    step_count: int = 0
    tool_call_count: int = 0
    max_steps: int = 20
    max_tool_calls: int = 16
    
    # State tracking
    status: Literal["queued", "running", "completed", "failed", "cancelled"] = "queued"
    pending_tool_calls: List[ToolCall] = Field(default_factory=list)
    final_answer: Optional[str] = None
    model_label: Optional[str] = None
    error: Optional[Dict[str, str]] = None
    
    # Traces & Events
    traces: List[ToolExecutionTrace] = Field(default_factory=list)
    usage: Dict[str, int] = Field(default_factory=dict)
    
    # Stream flag
    stream: bool = False
    reasoning_effort: Optional[str] = None
    
    # Custom client for Direct Demo Adapter
    custom_client: Optional[Any] = None

    model_config = {"arbitrary_types_allowed": True}
