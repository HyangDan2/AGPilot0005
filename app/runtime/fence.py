"""R1-FIX-04: Monotonic Hard Deadline & Atomic Terminal State Fence."""
from __future__ import annotations

import threading
from typing import Optional

from app.core.clock import Clock, system_clock


class HardDeadline:
    """Immutable run-wide monotonic deadline shared across Legacy, LangGraph, Gateway, and Tools."""

    def __init__(self, budget_seconds: float, clock: Clock = system_clock):
        self._clock = clock
        self.budget_seconds = max(0.0, float(budget_seconds))
        self.started_monotonic = clock.monotonic()
        self.deadline_monotonic = self.started_monotonic + self.budget_seconds

    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock.monotonic() - self.started_monotonic)

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - self._clock.monotonic())

    def is_expired(self) -> bool:
        return self.remaining_seconds() <= 0.0

    def cap_timeout(self, requested_timeout: float) -> float:
        """Caps any child model/tool timeout so it can never exceed the remaining run deadline."""
        rem = self.remaining_seconds()
        if rem <= 0.0:
            return 0.0
        return min(max(0.001, float(requested_timeout)), rem)


class TerminalStateFence:
    """Atomic terminal state latch blocking late results, state reversals, and duplicate terminals.

    Never holds its lock across external I/O (R1-FIX-04 B.9).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._terminal_state: Optional[str] = None
        self._late_result_count: int = 0

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self._terminal_state is not None

    @property
    def terminal_state(self) -> Optional[str]:
        with self._lock:
            return self._terminal_state

    @property
    def late_result_count(self) -> int:
        with self._lock:
            return self._late_result_count

    def try_finalize(self, state: str) -> bool:
        """Atomically transitions from non-terminal to terminal state. Returns True at most once."""
        with self._lock:
            if self._terminal_state is not None:
                return False
            self._terminal_state = state
            return True

    def record_late_result(self) -> int:
        with self._lock:
            self._late_result_count += 1
            return self._late_result_count
