"""R1-FIX-08: Async Bounded Trajectory Writer with Failure Isolation, Explicit Flush & Graceful Shutdown."""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple

from app.tracing.store import AnyTrajectoryEvent, DuplicateEventError, TrajectoryEventStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlushResult:
    completed: bool
    persisted_count: int
    failed_count: int
    dropped_count: int


class _FlushBarrier:
    def __init__(self) -> None:
        self.done = threading.Event()


class _ShutdownSignal:
    pass


class AsyncTrajectoryWriter:
    """Single-worker bounded-queue writer isolating SQLite I/O and failures from Agent Runtime."""

    def __init__(self, store: TrajectoryEventStore, *, queue_capacity: int = 1024):
        self.store = store
        self.queue_capacity = max(1, int(queue_capacity))
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self.queue_capacity)
        self._lock = threading.Lock()
        self._accepting: bool = True
        self._worker_status: str = "running"
        self.dropped_event_count: int = 0
        self.persistence_failure_count: int = 0
        self.persisted_event_count: int = 0
        self.incomplete_trajectories: Set[str] = set()
        self._thread = threading.Thread(
            target=self._run_worker,
            name="agcore-trajectory-writer",
            daemon=True,
        )
        self._thread.start()

    def enqueue(
        self,
        event: AnyTrajectoryEvent,
        *,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
    ) -> bool:
        """Non-blocking enqueue. Never raises or stalls Runtime on queue saturation."""
        with self._lock:
            if not self._accepting:
                self.dropped_event_count += 1
                self.incomplete_trajectories.add(event.trajectory_id)
                return False
        try:
            self._queue.put_nowait(("event", event, owner_id, service_id))
            return True
        except queue.Full:
            with self._lock:
                self.dropped_event_count += 1
                self.incomplete_trajectories.add(event.trajectory_id)
            return False

    def _run_worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if isinstance(item, _ShutdownSignal):
                    with self._lock:
                        self._worker_status = "stopped"
                    return
                if isinstance(item, _FlushBarrier):
                    item.done.set()
                    continue
                _, event, owner_id, service_id = item
                try:
                    self.store.append(event, owner_id=owner_id, service_id=service_id)
                    with self._lock:
                        self.persisted_event_count += 1
                except DuplicateEventError:
                    # Idempotent duplicate rejection — not a persistence failure
                    pass
                except Exception:
                    with self._lock:
                        self.persistence_failure_count += 1
                        self.incomplete_trajectories.add(event.trajectory_id)
                    logger.warning(
                        "Trajectory persistence failed for event %s (isolated from runtime)",
                        getattr(event, "event_id", "unknown"),
                    )
            finally:
                self._queue.task_done()

    def flush(self, timeout_seconds: float = 5.0) -> FlushResult:
        """Waits until all previously accepted events have finished persisting (or failed)."""
        barrier = _FlushBarrier()
        try:
            self._queue.put(barrier, timeout=max(0.05, float(timeout_seconds)))
            completed = barrier.done.wait(timeout=max(0.05, float(timeout_seconds)))
        except queue.Full:
            completed = False
        with self._lock:
            return FlushResult(
                completed=completed,
                persisted_count=self.persisted_event_count,
                failed_count=self.persistence_failure_count,
                dropped_count=self.dropped_event_count,
            )

    def shutdown(self, timeout_seconds: float = 5.0) -> Dict[str, Any]:
        """Stops accepting new events and flushes remaining items within timeout."""
        with self._lock:
            self._accepting = False
        flush_res = self.flush(timeout_seconds=timeout_seconds)
        try:
            self._queue.put(_ShutdownSignal(), timeout=0.5)
        except queue.Full:
            pass
        self._thread.join(timeout=max(0.1, float(timeout_seconds)))
        unsaved = self._queue.qsize()
        clean = flush_res.completed and not self._thread.is_alive() and unsaved == 0
        if not clean:
            with self._lock:
                self._worker_status = "shutdown_timeout"
        return {
            "clean": clean,
            "unsaved_events": unsaved,
            "persisted_count": flush_res.persisted_count,
            "failed_count": flush_res.failed_count,
            "dropped_count": flush_res.dropped_count,
        }

    def health(self) -> Dict[str, Any]:
        with self._lock:
            degraded = self.dropped_event_count > 0 or self.persistence_failure_count > 0 or self._worker_status != "running"
            return {
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self.queue_capacity,
                "dropped_event_count": self.dropped_event_count,
                "persistence_failure_count": self.persistence_failure_count,
                "worker_status": self._worker_status,
                "telemetry_health": "degraded" if degraded else "healthy",
                "incomplete_trajectory_count": len(self.incomplete_trajectories),
            }
