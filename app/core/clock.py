"""Controllable Clock abstraction for production and deterministic Replay (FIX-R1-04)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


KST = timezone(timedelta(hours=9))


class Clock(Protocol):
    def now_epoch(self) -> float:
        """Wall-clock epoch seconds."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds for elapsed/budget/latency calculations."""
        ...

    def now_kst(self) -> datetime:
        """Current timezone-aware KST datetime."""
        ...


class SystemClock:
    """Default production clock backed by system time."""

    def now_epoch(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def now_kst(self) -> datetime:
        return datetime.now(KST)


class MockClock:
    """Deterministic controllable clock for Replay parity and budget testing."""

    def __init__(self, start_epoch: float = 1795600000.0, start_monotonic: float = 1000.0):
        self._epoch = float(start_epoch)
        self._monotonic = float(start_monotonic)

    def now_epoch(self) -> float:
        return self._epoch

    def monotonic(self) -> float:
        return self._monotonic

    def now_kst(self) -> datetime:
        return datetime.fromtimestamp(self._epoch, tz=KST)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("MockClock cannot move backwards")
        self._epoch += float(seconds)
        self._monotonic += float(seconds)


system_clock = SystemClock()
