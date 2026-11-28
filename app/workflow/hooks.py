"""WP-05 Hooks Engine.

Eleven lifecycle events let a deployment extend R3 workflows without forking the
runtime:

``session_start``, ``session_end``, ``before_planning``, ``after_planning``,
``before_tool_call``, ``after_tool_call``, ``before_file_write``, ``after_file_write``,
``on_approval_required``, ``on_error``, ``workflow_completed``.

Safety rules enforced here, not by convention:

* **No shell** — ``allow_shell=True`` is refused at registration, and command-based
  hooks are refused outright.  A hook is a Python callable, never a subprocess.
* **Deduplicated** — a repeated ``event_id`` is ignored (``deduplicated=True``).
* **Non-recursive** — an active hook stack depth guard stops hook-triggered re-entry.
* **Timeboxed** — each hook runs under its own ``timeout_seconds``.
* **Failure policy** — ``fail_closed`` aborts the workflow at the hook
  (:class:`HookValidationError`) so a failed guard never lets the original action pass;
  ``fail_open`` records the failure and continues.
* **Audited** — every invocation yields a :class:`HookExecutionRecord`.
"""
from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "HookEvent",
    "HOOK_EVENTS",
    "FailurePolicy",
    "HookDefinition",
    "HookExecutionRecord",
    "HookDispatchResult",
    "HookError",
    "HookValidationError",
    "HookSecurityError",
    "HookRecursionError",
    "HookRegistry",
]

PERMISSION_RANK = {"read": 1, "write": 2, "admin": 3}


class HookEvent(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    BEFORE_PLANNING = "before_planning"
    AFTER_PLANNING = "after_planning"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    BEFORE_FILE_WRITE = "before_file_write"
    AFTER_FILE_WRITE = "after_file_write"
    ON_APPROVAL_REQUIRED = "on_approval_required"
    ON_ERROR = "on_error"
    WORKFLOW_COMPLETED = "workflow_completed"


HOOK_EVENTS: Tuple[HookEvent, ...] = tuple(HookEvent)


class FailurePolicy(str, Enum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


class HookError(RuntimeError):
    pass


class HookValidationError(HookError):
    """A hook failed closed (or the definition is invalid)."""

    def __init__(self, code: str, *, hook_id: str = "", detail: str = "") -> None:
        self.code = code
        self.hook_id = hook_id
        message = code + (f" hook={hook_id}" if hook_id else "") + (f": {detail}" if detail else "")
        super().__init__(message)


class HookSecurityError(HookError, PermissionError):
    def __init__(self, code: str, *, hook_id: str = "") -> None:
        self.code = code
        self.hook_id = hook_id
        super().__init__(f"{code}" + (f" hook={hook_id}" if hook_id else ""))


class HookRecursionError(HookError):
    pass


class HookDefinition(BaseModel):
    """One registered hook.

    ``handler`` receives the event payload (a dict) and may return a dict of payload
    patches, or ``{"blocked": True}`` to stop the event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    hook_id: str
    event: HookEvent
    handler: Optional[Callable[..., Any]] = None
    timeout_seconds: float = 5.0
    failure_policy: FailurePolicy = FailurePolicy.FAIL_CLOSED
    allow_shell: bool = False
    required_permission: Optional[Literal["read", "write", "admin"]] = None
    priority: int = 100
    description: str = ""
    command: Optional[str] = None

    @field_validator("hook_id")
    @classmethod
    def _hook_id(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned or len(cleaned) > 128:
            raise ValueError("invalid_hook_id")
        return cleaned

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout(cls, value: float) -> float:
        if not (0 < float(value) <= 120):
            raise ValueError("invalid_hook_timeout")
        return float(value)

    @model_validator(mode="after")
    def _handler_present(self) -> "HookDefinition":
        # Shell/command hooks are rejected by the registry (HookSecurityError) rather
        # than by validation, so the refusal is an explicit, testable security event.
        if self.handler is None and self.command is None:
            raise ValueError("hook_handler_required")
        if self.handler is not None and not callable(self.handler):
            raise ValueError("hook_handler_not_callable")
        return self


class HookExecutionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    hook_id: str
    event: HookEvent
    event_id: str
    status: Literal["success", "failed", "timeout", "skipped"]
    failure_policy: FailurePolicy
    duration_ms: float = 0.0
    error: Optional[str] = None
    reason: Optional[str] = None


class HookDispatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event: HookEvent
    event_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    records: List[HookExecutionRecord] = Field(default_factory=list)
    blocked: bool = False
    deduplicated: bool = False

    @property
    def failures(self) -> List[HookExecutionRecord]:
        return [record for record in self.records if record.status in ("failed", "timeout")]


class HookRegistry:
    """Registry and dispatcher for lifecycle hooks."""

    def __init__(self, *, max_depth: int = 4, dedupe_window: int = 4096) -> None:
        self._hooks: Dict[str, HookDefinition] = {}
        self._order: List[str] = []
        self._seen_event_ids: Dict[str, None] = {}
        self._dedupe_window = max(16, int(dedupe_window))
        self._history: List[HookExecutionRecord] = []
        self._active: List[str] = []
        self.max_depth = max(1, int(max_depth))
        self.rejected_registrations: List[Dict[str, Any]] = []

    # -- registration ------------------------------------------------------------------
    def register(self, hook: HookDefinition) -> HookDefinition:
        if not isinstance(hook, HookDefinition):
            raise HookValidationError("hook_must_be_HookDefinition")
        if hook.allow_shell:
            self.rejected_registrations.append({"hook_id": hook.hook_id, "reason": "allow_shell_forbidden"})
            raise HookSecurityError("allow_shell_forbidden", hook_id=hook.hook_id)
        if hook.command is not None:
            self.rejected_registrations.append({"hook_id": hook.hook_id, "reason": "command_hook_forbidden"})
            raise HookSecurityError("command_hook_forbidden", hook_id=hook.hook_id)
        if hook.hook_id in self._hooks:
            raise HookValidationError("duplicate_hook_id", hook_id=hook.hook_id)
        self._hooks[hook.hook_id] = hook
        self._order.append(hook.hook_id)
        return hook

    def unregister(self, hook_id: str) -> None:
        self._hooks.pop(hook_id, None)
        if hook_id in self._order:
            self._order.remove(hook_id)

    def hooks_for(self, event: HookEvent | str) -> List[HookDefinition]:
        target = HookEvent(event)
        hooks = [self._hooks[hook_id] for hook_id in self._order if self._hooks[hook_id].event == target]
        return sorted(hooks, key=lambda hook: (-hook.priority, self._order.index(hook.hook_id)))

    def __len__(self) -> int:
        return len(self._hooks)

    def __contains__(self, hook_id: object) -> bool:
        return str(hook_id) in self._hooks

    # -- dispatch ----------------------------------------------------------------------
    def _remember_event(self, event_id: str) -> bool:
        """Return True when the event id was already dispatched."""
        if event_id in self._seen_event_ids:
            return True
        self._seen_event_ids[event_id] = None
        while len(self._seen_event_ids) > self._dedupe_window:
            oldest = next(iter(self._seen_event_ids))
            self._seen_event_ids.pop(oldest, None)
        return False

    async def dispatch(
        self,
        event: HookEvent | str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        event_id: Optional[str] = None,
        permissions: Sequence[str] = ("read",),
        granted_permission: Optional[str] = None,
    ) -> HookDispatchResult:
        target = HookEvent(event)
        resolved_event_id = event_id or f"{target.value}:{uuid.uuid4().hex}"
        if event_id is not None and self._remember_event(resolved_event_id):
            return HookDispatchResult(
                event=target, event_id=resolved_event_id, payload=dict(payload or {}), deduplicated=True,
            )
        if event_id is None:
            self._remember_event(resolved_event_id)

        if len(self._active) >= self.max_depth:
            raise HookRecursionError(f"hook_recursion_depth_exceeded: {len(self._active)}")
        if resolved_event_id in self._active:
            raise HookRecursionError(f"hook_recursion_event: {resolved_event_id}")

        granted = granted_permission or max(permissions, key=lambda perm: PERMISSION_RANK.get(perm, 0), default="read")
        current_payload: Dict[str, Any] = dict(payload or {})
        records: List[HookExecutionRecord] = []
        blocked = False

        self._active.append(resolved_event_id)
        try:
            for hook in self.hooks_for(target):
                needed = hook.required_permission
                if needed and PERMISSION_RANK.get(needed, 99) > PERMISSION_RANK.get(granted, 0):
                    records.append(HookExecutionRecord(
                        hook_id=hook.hook_id, event=target, event_id=resolved_event_id,
                        status="skipped", failure_policy=hook.failure_policy,
                        reason=f"insufficient_permission:{needed}",
                    ))
                    continue

                started = time.perf_counter()
                try:
                    result = await self._invoke(hook, dict(current_payload))
                except asyncio.TimeoutError:
                    duration = (time.perf_counter() - started) * 1000
                    record = HookExecutionRecord(
                        hook_id=hook.hook_id, event=target, event_id=resolved_event_id,
                        status="timeout", failure_policy=hook.failure_policy,
                        duration_ms=round(duration, 3), error="timeout",
                    )
                    records.append(record)
                    self._history.append(record)
                    if hook.failure_policy == FailurePolicy.FAIL_CLOSED:
                        raise HookValidationError("hook_timeout_fail_closed", hook_id=hook.hook_id)
                    continue
                except HookRecursionError:
                    raise
                except Exception as exc:  # noqa: BLE001 - failure policy decides
                    duration = (time.perf_counter() - started) * 1000
                    record = HookExecutionRecord(
                        hook_id=hook.hook_id, event=target, event_id=resolved_event_id,
                        status="failed", failure_policy=hook.failure_policy,
                        duration_ms=round(duration, 3), error=f"{type(exc).__name__}: {exc}",
                    )
                    records.append(record)
                    self._history.append(record)
                    if hook.failure_policy == FailurePolicy.FAIL_CLOSED:
                        raise HookValidationError("hook_failed_closed", hook_id=hook.hook_id) from exc
                    continue

                duration = (time.perf_counter() - started) * 1000
                record = HookExecutionRecord(
                    hook_id=hook.hook_id, event=target, event_id=resolved_event_id,
                    status="success", failure_policy=hook.failure_policy, duration_ms=round(duration, 3),
                )
                records.append(record)
                self._history.append(record)

                if isinstance(result, Mapping):
                    if result.get("blocked") is True:
                        blocked = True
                        record = record.model_copy(update={"reason": "hook_blocked_event"})
                        records[-1] = record
                        self._history[-1] = record
                        break
                    patches = {key: value for key, value in result.items() if key != "blocked"}
                    current_payload.update(patches)
        finally:
            self._active.pop()

        return HookDispatchResult(
            event=target, event_id=resolved_event_id, payload=current_payload,
            records=records, blocked=blocked,
        )

    async def _invoke(self, hook: HookDefinition, payload: Dict[str, Any]) -> Any:
        assert hook.handler is not None
        handler = hook.handler
        if inspect.iscoroutinefunction(handler):
            return await asyncio.wait_for(handler(payload), timeout=hook.timeout_seconds)
        result = await asyncio.wait_for(asyncio.to_thread(handler, payload), timeout=hook.timeout_seconds)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, timeout=hook.timeout_seconds)
        return result

    # -- diagnostics -------------------------------------------------------------------
    def records(self, *, event: Optional[HookEvent | str] = None) -> List[HookExecutionRecord]:
        if event is None:
            return list(self._history)
        target = HookEvent(event)
        return [record for record in self._history if record.event == target]

    def describe(self) -> Dict[str, Any]:
        by_event: Dict[str, int] = {event.value: 0 for event in HOOK_EVENTS}
        for hook in self._hooks.values():
            by_event[hook.event.value] += 1
        return {
            "hooks": len(self._hooks),
            "by_event": by_event,
            "support_shell": False,
            "max_depth": self.max_depth,
            "rejected": list(self.rejected_registrations),
        }
