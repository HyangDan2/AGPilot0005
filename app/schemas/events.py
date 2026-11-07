"""SSE Event Schemas and Payload Definitions."""
import time
from typing import Any, Dict, Literal, Optional
from pydantic import BaseModel, Field

EventType = Literal[
    "run.started",
    "step.started",
    "model.started",
    "tool.started",
    "tool.completed",
    "step.completed",
    "answer.delta",
    "run.completed",
    "run.failed",
    "run.cancelled",
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
