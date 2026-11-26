"""FIX-R1-03: Immutable Append-only Trajectory Event Schemas."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


TRAJECTORY_SCHEMA_VERSION = "1.0"


class DecisionEvent(BaseModel):
    """Recorded after AGPolicy decision & ActionValidator verification, before action execution."""

    model_config = ConfigDict(frozen=True)

    event_type: Literal["DecisionEvent"] = "DecisionEvent"
    event_id: str
    schema_version: str = TRAJECTORY_SCHEMA_VERSION
    trajectory_id: str
    run_id: str
    decision_id: str
    step_index: int
    decision_type: str
    state_features: Dict[str, Any] = Field(default_factory=dict)
    allowed_actions: List[str] = Field(default_factory=list)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    chosen_action: str
    proposed_parameters: Dict[str, Any] = Field(default_factory=dict)
    behavior_policy_version: str
    config_digest: str
    action_probability: Optional[float] = None
    validator_result: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float


class ActionEvent(BaseModel):
    """Recorded when a validated action (or validated fallback action) begins execution."""

    model_config = ConfigDict(frozen=True)

    event_type: Literal["ActionEvent"] = "ActionEvent"
    event_id: str
    schema_version: str = TRAJECTORY_SCHEMA_VERSION
    trajectory_id: str
    run_id: str
    decision_id: str
    action_id: str
    executed_action: str
    executed_parameters: Dict[str, Any] = Field(default_factory=dict)
    fallback_applied: bool = False
    timestamp: float


class OutcomeEvent(BaseModel):
    """Recorded on action completion, failure, or terminal run completion."""

    model_config = ConfigDict(frozen=True)

    event_type: Literal["OutcomeEvent"] = "OutcomeEvent"
    event_id: str
    schema_version: str = TRAJECTORY_SCHEMA_VERSION
    trajectory_id: str
    run_id: str
    decision_id: Optional[str] = None
    action_id: str
    next_state_features: Dict[str, Any] = Field(default_factory=dict)
    outcome: str  # "success" | "failed" | "denied" | "timeout" | "cancelled" | "UNKNOWN"
    reward_signals: Dict[str, float] = Field(default_factory=dict)
    execution_latency: float = 0.0
    token_usage: Dict[str, int] = Field(default_factory=dict)
    terminal_status: Optional[str] = None
    timestamp: float


class FeedbackEvent(BaseModel):
    """Recorded asynchronously when user feedback or delayed reward arrives (REG-POL-18)."""

    model_config = ConfigDict(frozen=True)

    event_type: Literal["FeedbackEvent"] = "FeedbackEvent"
    event_id: str
    schema_version: str = TRAJECTORY_SCHEMA_VERSION
    trajectory_id: str
    run_id: str
    decision_id: Optional[str] = None
    feedback_type: str  # e.g. "thumbs_up" | "thumbs_down" | "delayed_eval"
    reward_signal: float
    timestamp: float
