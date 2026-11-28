"""WP-09 Trajectory & Observability.

One :class:`WorkflowTrajectory` is the structured record of a single R3 run: what was
planned, which capability was called with which (masked) arguments, which skill was
loaded, which rules were applied, what the hooks did, where retries/replans happened, and
why it ended.  It is the artifact the console renders and the raw material a future
``AGPolicy`` can learn from (RL training itself is out of scope for R3).

Two export shapes:

``to_dict()`` / ``to_json()``
    The full internal record.  Secrets are masked at record time — credential values,
    API keys, bearer tokens and any value matching
    :data:`app.tracing.sanitizer.SECRET_VALUE_PATTERNS` are replaced before they ever
    reach the object — while structural fields (step ids, capability names, counts,
    decisions) stay intact so the record remains useful.

``to_telemetry()``
    The same record passed through :func:`app.tracing.sanitizer.sanitize_mapping`, i.e. the
    strict allowlist used for the platform's existing telemetry.  Free-form text is
    redacted there; use it when the trajectory leaves the process.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.tracing.sanitizer import SECRET_VALUE_PATTERNS, sanitize_mapping

__all__ = [
    "MASKED",
    "TokenUsage",
    "StepRecord",
    "ToolCallRecord",
    "SkillSelectionRecord",
    "HookExecutionSummary",
    "MCPExecutionRecord",
    "RetryRecord",
    "ReplanRecord",
    "FailureRecord",
    "ArtifactRecord",
    "WorkflowTrajectory",
    "scrub_secrets",
]

MASKED = "[redacted]"

#: Key fragments that always hold a secret.  Deliberately narrower than the telemetry
#: allowlist in ``app/tracing/sanitizer`` — ``content``/``query``/``text`` are structural
#: here, and are covered by the value-pattern scan instead.
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "credential",
    "private_key",
    "bearer",
    "cookie",
    "session_key",
    "access_key",
)

_MAX_STRING = 2_000
_MAX_LIST = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_sensitive_key(key: str) -> bool:
    lower = (key or "").lower()
    return any(part in lower for part in _SENSITIVE_KEY_PARTS)


def _looks_like_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS)


def scrub_secrets(value: Any, *, _depth: int = 0) -> Any:
    """Return ``value`` with secrets masked (recursive, bounded)."""
    if _depth > 8:
        return "[depth_limit]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _looks_like_secret(value):
            return MASKED
        if len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + f"…[+{len(value) - _MAX_STRING} chars]"
        return value
    if isinstance(value, Mapping):
        return {
            str(key): (MASKED if _is_sensitive_key(str(key)) and item else scrub_secrets(item, _depth=_depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [scrub_secrets(item, _depth=_depth + 1) for item in value]
        if len(items) > _MAX_LIST:
            head = items[:_MAX_LIST]
            return head + [f"…[+{len(items) - _MAX_LIST} items]"]
        return items
    return scrub_secrets(str(value), _depth=_depth + 1)


def _compact(value: Any) -> Any:
    """A summary shape for tool results: keys and scalar values, lists truncated."""
    if isinstance(value, Mapping):
        return {str(key): scrub_secrets(item) for key, item in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return [scrub_secrets(item) for item in list(value)[:3]]
    return scrub_secrets(value)


# --------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------
class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    context_tokens: int = 0

    def add(self, *, context_tokens: int = 0, model_calls: int = 0,
            prompt_tokens: int = 0, completion_tokens: int = 0) -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + prompt_tokens,
            completion_tokens=self.completion_tokens + completion_tokens,
            total_tokens=self.total_tokens + prompt_tokens + completion_tokens + context_tokens,
            model_calls=self.model_calls + model_calls,
            context_tokens=self.context_tokens + context_tokens,
        )


class StepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    name: str = ""
    capability: str = ""
    status: Literal["completed", "failed", "skipped", "replayed", "pending"] = "pending"
    attempt: int = 1
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_ms: float = 0.0
    output_key: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    replanned_from: Optional[str] = None


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    capability: str
    transport: str = "unknown"  # office | registry | mcp
    tool_name: Optional[str] = None
    attempt: int = 1
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result_summary: Any = None
    status: Literal["ok", "error", "timeout", "denied"] = "ok"
    duration_ms: float = 0.0
    error_code: Optional[str] = None
    error: Optional[str] = None


class SkillSelectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    version: Optional[str] = None
    reason: str = ""
    loaded: bool = False
    allowed_capabilities: List[str] = Field(default_factory=list)


class HookExecutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    hook_id: str
    status: str
    failure_policy: Optional[str] = None
    duration_ms: float = 0.0
    error: Optional[str] = None
    reason: Optional[str] = None


class MCPExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str
    tool: str
    status: Literal["ok", "error", "denied"] = "ok"
    duration_ms: float = 0.0
    error: Optional[str] = None
    suspicious_output: bool = False


class RetryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    capability: str
    attempt: int
    error_code: str
    error: str = ""
    timeout: bool = False


class ReplanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_plan_id: str
    to_plan_id: str
    version: int
    failed_step_id: str
    error_code: str
    drop_completed: List[str] = Field(default_factory=list)
    note: str = ""


class FailureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: Optional[str] = None
    capability: Optional[str] = None
    code: str = "unknown"
    message: str = ""
    attempts: int = 0
    replanned: bool = False
    source: str = ""  # adapter | hook | planner | scope | approval | runtime
    cause_type: Optional[str] = None


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: Optional[str] = None
    kind: str = "file"
    path: Optional[str] = None
    sha256: Optional[str] = None
    bytes: Optional[int] = None
    produced_by_step: Optional[str] = None


# --------------------------------------------------------------------------------------
# Trajectory
# --------------------------------------------------------------------------------------
class WorkflowTrajectory(BaseModel):
    """The WP-09 trajectory record."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    plan_summary: Dict[str, Any] = Field(default_factory=dict)
    step_records: List[StepRecord] = Field(default_factory=list)
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    skill_selections: List[SkillSelectionRecord] = Field(default_factory=list)
    applied_rules: List[Dict[str, Any]] = Field(default_factory=list)
    hook_executions: List[HookExecutionSummary] = Field(default_factory=list)
    mcp_executions: List[MCPExecutionRecord] = Field(default_factory=list)
    retries: List[RetryRecord] = Field(default_factory=list)
    replans: List[ReplanRecord] = Field(default_factory=list)
    failures: List[FailureRecord] = Field(default_factory=list)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    execution_latency_ms: float = 0.0
    artifacts: List[ArtifactRecord] = Field(default_factory=list)
    status: str = "pending"
    created_at: datetime = Field(default_factory=_utcnow)
    completed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # -- recording --------------------------------------------------------------------
    def set_plan(self, summary: Mapping[str, Any]) -> None:
        self.plan_summary = {str(key): scrub_secrets(value) for key, value in summary.items()}

    def add_step(self, record: StepRecord) -> StepRecord:
        self.step_records.append(record)
        return record

    def close_step(self, step_id: str, *, status: str, duration_ms: float, **updates: Any) -> None:
        for index in range(len(self.step_records) - 1, -1, -1):
            record = self.step_records[index]
            if record.step_id == step_id and record.status == "pending":
                payload = record.model_dump()
                payload.update({"status": status, "duration_ms": round(duration_ms, 3), "ended_at": _utcnow()})
                payload.update({key: value for key, value in updates.items() if key in payload})
                self.step_records[index] = StepRecord(**payload)
                return

    def add_tool_call(self, record: ToolCallRecord) -> ToolCallRecord:
        record = record.model_copy(
            update={"arguments": scrub_secrets(record.arguments), "result_summary": _compact(record.result_summary)}
        )
        self.tool_calls.append(record)
        return record

    def add_skill(self, record: SkillSelectionRecord) -> SkillSelectionRecord:
        if any(item.skill == record.skill and item.version == record.version for item in self.skill_selections):
            return record
        self.skill_selections.append(record)
        return record

    def add_rules(self, records: Iterable[Mapping[str, Any]]) -> None:
        for item in records:
            entry = {
                "key": str(item.get("key", "")),
                "rule_id": str(item.get("rule_id", "")),
                "layer": str(item.get("layer", "")),
                "decision": str(item.get("decision", "")),
                "reason": str(item.get("reason", "")),
            }
            if entry not in self.applied_rules:
                self.applied_rules.append(entry)

    def add_hook_records(self, event: str, records: Iterable[Any]) -> None:
        for record in records:
            self.hook_executions.append(
                HookExecutionSummary(
                    event=str(getattr(record, "event", event)),
                    hook_id=getattr(record, "hook_id", ""),
                    status=getattr(record, "status", "unknown"),
                    failure_policy=str(getattr(getattr(record, "failure_policy", None), "value", getattr(record, "failure_policy", None)))
                    if getattr(record, "failure_policy", None) is not None else None,
                    duration_ms=float(getattr(record, "duration_ms", 0.0)),
                    error=getattr(record, "error", None),
                    reason=getattr(record, "reason", None),
                )
            )

    def add_mcp(self, record: MCPExecutionRecord) -> MCPExecutionRecord:
        self.mcp_executions.append(record)
        return record

    def add_retry(self, record: RetryRecord) -> RetryRecord:
        self.retries.append(record)
        return record

    def add_replan(self, record: ReplanRecord) -> ReplanRecord:
        self.replans.append(record)
        return record

    def add_failure(self, record: FailureRecord) -> FailureRecord:
        self.failures.append(record)
        return record

    def add_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        self.artifacts.append(record)
        return record

    def set_token_usage(self, usage: TokenUsage) -> None:
        self.token_usage = usage

    def finish(self, *, status: str, latency_ms: float) -> "WorkflowTrajectory":
        self.status = status
        self.execution_latency_ms = round(float(latency_ms), 3)
        self.completed_at = _utcnow()
        return self

    # -- export -----------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self, *, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=indent)

    def to_telemetry(self) -> Dict[str, Any]:
        """Strict sanitized form for telemetry export (R1-FIX-13 allowlist)."""
        return sanitize_mapping(self.to_dict())

    def learning_payload(self) -> Dict[str, Any]:
        """Compact, typed features for a future AGPolicy learner (no free-form text)."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "plan": {
                "plan_id": self.plan_summary.get("plan_id"),
                "version": self.plan_summary.get("version"),
                "step_count": self.plan_summary.get("step_count"),
                "capabilities": list(self.plan_summary.get("capabilities") or ()),
                "parallel_groups": self.plan_summary.get("parallel_groups") or {},
            },
            "step_outcomes": [
                {
                    "step_id": record.step_id,
                    "capability": record.capability,
                    "status": record.status,
                    "attempt": record.attempt,
                    "duration_ms": record.duration_ms,
                    "error_code": record.error_code,
                }
                for record in self.step_records
            ],
            "skills": [record.skill for record in self.skill_selections],
            "rules": [entry["key"] for entry in self.applied_rules if entry.get("decision") in ("applied", "override")],
            "hook_failures": [
                {"event": record.event, "hook_id": record.hook_id, "status": record.status}
                for record in self.hook_executions
                if record.status in ("failed", "timeout")
            ],
            "retry_count": len(self.retries),
            "replan_count": len(self.replans),
            "failure_codes": [record.code for record in self.failures],
            "artifacts": [record.kind for record in self.artifacts],
            "token_usage": self.token_usage.model_dump(),
            "execution_latency_ms": self.execution_latency_ms,
        }

    @classmethod
    def restore(cls, payload: Optional[Mapping[str, Any]], *, run_id: str) -> "WorkflowTrajectory":
        if not payload:
            return cls(run_id=run_id)
        data = dict(payload)
        data["run_id"] = run_id
        return cls.model_validate(data)
