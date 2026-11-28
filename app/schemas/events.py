"""SSE Event Schemas and Payload Definitions."""
import time
from typing import Any, Dict, Literal, Optional
from pydantic import BaseModel, Field

EventType = Literal[
    "run.started",
    "run.deferred",
    "step.started",
    "model.started",
        "model.completed",
        "reasoning.delta",
    "tool.started",
    "tool.completed",
    "step.completed",
    "answer.delta",
    "run.completed",
    "run.failed",
    "run.cancelled",
    # R2 Canvas artifact events (additive; WO-C06)
    "artifact.created",
    "artifact.delta",
    "artifact.updated",
    "artifact.edit.proposed",
    "artifact.edit.applied",
    "artifact.edit.rejected",
    "artifact.error",
    # R3 Workflow events (additive; WO-R3-PREDEV WP-02/WP-09)
    "workflow.started",
    "workflow.plan.created",
    "workflow.step.started",
    "workflow.step.completed",
    "workflow.approval.required",
    "workflow.checkpoint.saved",
    "workflow.completed",
    "workflow.failed",
    "workflow.cancelled",
]

class SSEEvent(BaseModel):
    run_id: str
    event_id: str
    event: EventType
    timestamp: float = Field(default_factory=time.time)
    status: str
    data: Dict[str, Any] = Field(default_factory=dict)

    def to_sse(self) -> str:
        import json
        payload = {
            "run_id": self.run_id,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "status": self.status,
            **self.data
        }
        return f"event: {self.event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
