"""NG-08 & R1-FIX-08: Async Bounded Trajectory Writer with Dirty Set Persistence (No New Worker/Table)."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

from app.tracing.store import AnyTrajectoryEvent, DuplicateEventError, TrajectoryEventStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlushResult:
    completed: bool
    persisted_count: int
    failed_count: int
    dropped_count: int
    pending_dirty_count: int = 0


class _FlushBarrier:
    def __init__(self) -> None:
        self.done = threading.Event()


class _ShutdownSignal:
    pass


class AsyncTrajectoryWriter:
    """Single-worker bounded-queue writer with Dirty Set persistence into existing incomplete_trajectories table."""

    def __init__(
        self,
        store: TrajectoryEventStore,
        *,
        queue_capacity: int = 1024,
        max_dirty_trajectories: int = 4096,
    ):
        self.store = store
        self.queue_capacity = max(1, int(queue_capacity))
        self.max_dirty_trajectories = max(1, int(max_dirty_trajectories))
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self.queue_capacity)
        self._lock = threading.Lock()
        self._accepting: bool = True
        self._worker_status: str = "running"
        self.dropped_event_count: int = 0
        self.persistence_failure_count: int = 0
        self.persisted_event_count: int = 0
        self.incomplete_trajectories: Set[str] = set()
        self.dirty_trajectories: Dict[str, str] = {}
        self.dirty_overflow: bool = False
        self._thread = threading.Thread(
            target=self._run_worker,
            name="agcore-trajectory-writer",
            daemon=True,
        )
        self._thread.start()

    def mark_dirty(self, trajectory_id: str, reason: str) -> None:
        """NG-08 A / CR-08.7: Marks trajectory dirty directly with bounded memory cap."""
        with self._lock:
            self._mark_dirty_locked(trajectory_id, reason)

    def _drain_dirty_set(self) -> None:
        """Persists dirty markers into existing incomplete_trajectories table; clears ONLY after DB commit."""
        with self._lock:
            snapshot = list(self.dirty_trajectories.items())
        for tid, reason in snapshot:
            try:
                self.store.mark_incomplete(tid, reason)
                with self._lock:
                    self.dirty_trajectories.pop(tid, None)
            except Exception:
                # Retain dirty marker on failure; avoid busy loop
                time.sleep(0.02)
                break

    def enqueue(
        self,
        event: AnyTrajectoryEvent,
        *,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """Non-blocking enqueue. Never raises or stalls Runtime on queue saturation."""
        with self._lock:
            if not self._accepting:
                self.dropped_event_count += 1
                self._mark_dirty_locked(event.trajectory_id, "shutdown_rejected")
                return False
        try:
            self._queue.put_nowait(("event", event, owner_id, service_id, tenant_id))
            return True
        except queue.Full:
            with self._lock:
                self.dropped_event_count += 1
                self._mark_dirty_locked(event.trajectory_id, "queue_overflow")
            return False

    def _mark_dirty_locked(self, trajectory_id: str, reason: str) -> None:
        if trajectory_id not in self.incomplete_trajectories:
            if len(self.incomplete_trajectories) >= self.max_dirty_trajectories:
                self.dirty_overflow = True
            else:
                self.incomplete_trajectories.add(trajectory_id)
        if trajectory_id not in self.dirty_trajectories:
            if len(self.dirty_trajectories) >= self.max_dirty_trajectories:
                self.dirty_overflow = True
                return
            self.dirty_trajectories[trajectory_id] = reason

    def _run_worker(self) -> None:
        while True:
            self._drain_dirty_set()
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if isinstance(item, _ShutdownSignal):
                    self._drain_dirty_set()
                    with self._lock:
                        self._worker_status = "stopped"
                    return
                if isinstance(item, _FlushBarrier):
                    self._drain_dirty_set()
                    item.done.set()
                    continue
                _, event, owner_id, service_id, tenant_id = item
                try:
                    self.store.append(event, owner_id=owner_id, service_id=service_id, tenant_id=tenant_id)
                    with self._lock:
                        self.persisted_event_count += 1
                except DuplicateEventError:
                    pass
                except Exception:
                    with self._lock:
                        self.persistence_failure_count += 1
                        self._mark_dirty_locked(event.trajectory_id, "persistence_failure")
                    logger.warning(
                        "Trajectory persistence failed for event %s (isolated from runtime)",
                        getattr(event, "event_id", "unknown"),
                    )
            finally:
                self._queue.task_done()

    def flush(self, timeout_seconds: float = 5.0, *, _deadline: Optional[float] = None) -> FlushResult:
        """HF-07: Waits under ONE single monotonic deadline covering both barrier enqueue and drain wait."""
        deadline = _deadline if _deadline is not None else (time.monotonic() + max(0.0, float(timeout_seconds)))
        barrier = _FlushBarrier()
        completed = False
        put_rem = max(0.0, deadline - time.monotonic())
        if put_rem > 0.0:
            try:
                self._queue.put(barrier, timeout=put_rem)
                wait_rem = max(0.0, deadline - time.monotonic())
                completed = barrier.done.wait(timeout=wait_rem)
            except queue.Full:
                completed = False
        else:
            try:
                self._queue.put_nowait(barrier)
                completed = barrier.done.wait(timeout=0.0)
            except queue.Full:
                completed = False
        with self._lock:
            return FlushResult(
                completed=completed,
                persisted_count=self.persisted_event_count,
                failed_count=self.persistence_failure_count,
                dropped_count=self.dropped_event_count,
                pending_dirty_count=len(self.dirty_trajectories),
            )

    def shutdown(self, timeout_seconds: float = 5.0) -> Dict[str, Any]:
        """HF-07: Single overall monotonic deadline covering flush, shutdown signal, and thread join."""
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._lock:
            self._accepting = False
        flush_res = self.flush(timeout_seconds=timeout_seconds, _deadline=deadline)
        rem_sig = max(0.0, deadline - time.monotonic())
        try:
            if rem_sig > 0.0:
                self._queue.put(_ShutdownSignal(), timeout=min(0.5, rem_sig))
            else:
                self._queue.put_nowait(_ShutdownSignal())
        except queue.Full:
            pass
        rem_join = max(0.0, deadline - time.monotonic())
        self._thread.join(timeout=rem_join)
        unsaved = self._queue.qsize()
        with self._lock:
            pending_dirty = len(self.dirty_trajectories)
        clean = flush_res.completed and not self._thread.is_alive() and unsaved == 0 and pending_dirty == 0
        if not clean:
            with self._lock:
                self._worker_status = "shutdown_timeout"
        return {
            "clean": clean,
            "unsaved_events": unsaved,
            "unsaved_dirty_markers": pending_dirty,
            "persisted_count": flush_res.persisted_count,
            "failed_count": flush_res.failed_count,
            "dropped_count": flush_res.dropped_count,
        }

    def health(self) -> Dict[str, Any]:
        with self._lock:
            degraded = (
                self.dropped_event_count > 0
                or self.persistence_failure_count > 0
                or self.dirty_overflow
                or self._worker_status != "running"
            )
            return {
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self.queue_capacity,
                "dropped_event_count": self.dropped_event_count,
                "persistence_failure_count": self.persistence_failure_count,
                "pending_dirty_count": len(self.dirty_trajectories),
                "dirty_overflow": self.dirty_overflow,
                "worker_status": self._worker_status,
                "telemetry_health": "degraded" if degraded else "healthy",
                "incomplete_trajectory_count": len(self.incomplete_trajectories),
            }
