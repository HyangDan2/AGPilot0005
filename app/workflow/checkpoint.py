"""WP-02 Checkpoint & Resume (with the WP-10 re-verification).

A checkpoint is the durable state of one workflow run: the plan it is executing, which
steps finished, what they produced, whether a human approval is outstanding, and the run
status.  Two properties make it safe to resume:

1. **Scope re-verification.**  Every load that is followed by execution passes the
   caller's :class:`~app.workflow.safety.ExecutionContext` and runs
   :func:`~app.workflow.safety.assert_scope_access` against the stored scope.  A run that
   belongs to another tenant/owner/service/session can never be read or resumed, even by
   someone who knows the ``run_id`` — the check is on the stored scope, not on the
   request.
2. **Idempotent replay.**  ``step_outputs``/``completed_steps`` are the record of what
   already happened.  The runtime skips those steps on resume and the
   :class:`~app.workflow.safety.IdempotencyLedger` blocks a repeated external side effect
   for the same ``step_id + idempotency_key`` (Acceptance Scenario B).

Storage backends are interchangeable: SQLite (default, ``data/workflow_checkpoints.db``),
a JSON directory, or in-process memory for tests.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.workflow.safety import ExecutionContext, assert_scope_access

__all__ = [
    "CheckpointStatus",
    "CheckpointError",
    "WorkflowCheckpoint",
    "WorkflowCheckpointStore",
    "default_checkpoint_path",
]

CheckpointStatus = Literal["pending", "running", "awaiting_approval", "completed", "failed", "cancelled"]
CheckpointBackend = Literal["sqlite", "file", "memory"]

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_checkpoint_path() -> Path:
    """``<repo>/data/workflow_checkpoints.db`` (``AGCORE_R3_CHECKPOINT_DB`` overrides)."""
    override = (os.environ.get("AGCORE_R3_CHECKPOINT_DB") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data" / "workflow_checkpoints.db"


class CheckpointError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


class WorkflowCheckpoint(BaseModel):
    """The resumable state of one run (WP-02 field set + additive bookkeeping)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    scope: Dict[str, Optional[str]] = Field(default_factory=dict)
    plan: Dict[str, Any] = Field(default_factory=dict)
    completed_steps: List[str] = Field(default_factory=list)
    step_outputs: Dict[str, Any] = Field(default_factory=dict)
    pending_approval_id: Optional[str] = None
    status: CheckpointStatus = "pending"
    updated_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)
    # -- additive bookkeeping (observability / debugging; not part of the resume contract)
    plan_version: int = 1
    replans: int = 0
    failures: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned or len(cleaned) > 128:
            raise ValueError("invalid_run_id")
        return cleaned

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def with_progress(
        self,
        *,
        status: Optional[CheckpointStatus] = None,
        completed_steps: Optional[Iterable[str]] = None,
        step_outputs: Optional[Dict[str, Any]] = None,
        pending_approval_id: Optional[str] = None,
        clear_pending_approval: bool = False,
        plan: Optional[Dict[str, Any]] = None,
        plan_version: Optional[int] = None,
        replans: Optional[int] = None,
        failures: Optional[Sequence[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "WorkflowCheckpoint":
        data = self.model_dump()
        if status is not None:
            data["status"] = status
        if completed_steps is not None:
            data["completed_steps"] = list(completed_steps)
        if step_outputs is not None:
            data["step_outputs"] = dict(step_outputs)
        if clear_pending_approval:
            data["pending_approval_id"] = None
        elif pending_approval_id is not None:
            data["pending_approval_id"] = pending_approval_id
        if plan is not None:
            data["plan"] = dict(plan)
        if plan_version is not None:
            data["plan_version"] = int(plan_version)
        if replans is not None:
            data["replans"] = int(replans)
        if failures is not None:
            data["failures"] = [dict(item) for item in failures]
        if metadata:
            merged = dict(data.get("metadata") or {})
            merged.update(metadata)
            data["metadata"] = merged
        data["updated_at"] = _utcnow()
        return WorkflowCheckpoint(**data)

    def public_view(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "scope": dict(self.scope),
            "plan_id": (self.plan or {}).get("plan_id"),
            "plan_version": self.plan_version,
            "completed_steps": list(self.completed_steps),
            "step_count": len((self.plan or {}).get("steps") or ()),
            "pending_approval_id": self.pending_approval_id,
            "failures": list(self.failures),
            "replans": self.replans,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _encode(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _decode(raw: str) -> Dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise CheckpointError("corrupt_checkpoint_payload")
    return value


class WorkflowCheckpointStore:
    """Durable checkpoint storage.

    ``context`` is optional on reads for pure inspection (``GET`` of one's own run); any
    read that will be *resumed* passes it so the scope is re-verified.
    """

    def __init__(
        self,
        path: Optional[str | Path] = None,
        *,
        backend: CheckpointBackend = "sqlite",
        fsync: bool = False,
    ) -> None:
        if backend not in ("sqlite", "file", "memory"):
            raise CheckpointError("unknown_backend", str(backend))
        self.backend: CheckpointBackend = backend
        self.fsync = fsync
        self._lock = threading.RLock()
        self._memory: Dict[str, Dict[str, Any]] = {}
        self._conn: Optional[sqlite3.Connection] = None
        self._file_root: Optional[Path] = None

        if backend == "memory":
            self.path: Optional[Path] = None
        elif backend == "file":
            self._file_root = Path(path or default_checkpoint_path().with_suffix(""))
            self._file_root.mkdir(parents=True, exist_ok=True)
            self.path = self._file_root
        else:
            self.path = Path(path or default_checkpoint_path())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._init_sqlite()

    # -- sqlite ------------------------------------------------------------------------
    def _init_sqlite(self) -> None:
        assert self.path is not None
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                run_id      TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL,
                owner_id    TEXT NOT NULL,
                service_id  TEXT,
                session_id  TEXT,
                status      TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                payload     TEXT NOT NULL,
                trajectory  TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_checkpoints_scope "
            "ON workflow_checkpoints (tenant_id, owner_id, status)"
        )
        conn.commit()
        self._conn = conn

    # -- persistence primitives --------------------------------------------------------
    def _write(self, checkpoint: WorkflowCheckpoint, trajectory: Optional[Dict[str, Any]]) -> None:
        payload = checkpoint.model_dump(mode="json")
        scope = checkpoint.scope or {}
        blob = _encode(payload)
        traj = _encode(trajectory) if trajectory is not None else None
        with self._lock:
            if self.backend == "memory":
                self._memory[checkpoint.run_id] = {"checkpoint": payload, "trajectory": trajectory}
                return
            if self.backend == "file":
                assert self._file_root is not None
                target = self._file_root / f"{checkpoint.run_id}.json"
                tmp = target.with_suffix(".json.tmp")
                tmp.write_text(_encode({"checkpoint": payload, "trajectory": trajectory}), encoding="utf-8")
                tmp.replace(target)
                return
            assert self._conn is not None
            self._conn.execute(
                """
                INSERT INTO workflow_checkpoints
                    (run_id, tenant_id, owner_id, service_id, session_id, status,
                     created_at, updated_at, payload, trajectory)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload,
                    trajectory=excluded.trajectory
                """,
                (
                    checkpoint.run_id,
                    str(scope.get("tenant_id") or ""),
                    str(scope.get("owner_id") or ""),
                    scope.get("service_id"),
                    scope.get("session_id"),
                    checkpoint.status,
                    checkpoint.created_at.isoformat(),
                    checkpoint.updated_at.isoformat(),
                    blob,
                    traj,
                ),
            )
            self._conn.commit()

    def _read(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self.backend == "memory":
                return self._memory.get(run_id)
            if self.backend == "file":
                assert self._file_root is not None
                target = self._file_root / f"{run_id}.json"
                if not target.is_file():
                    return None
                return json.loads(target.read_text(encoding="utf-8"))
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT payload, trajectory FROM workflow_checkpoints WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            return {
                "checkpoint": _decode(row[0]),
                "trajectory": _decode(row[1]) if row[1] else None,
            }

    # -- public API --------------------------------------------------------------------
    def save(
        self,
        checkpoint: WorkflowCheckpoint,
        *,
        trajectory: Optional[Dict[str, Any]] = None,
    ) -> WorkflowCheckpoint:
        if not isinstance(checkpoint, WorkflowCheckpoint):
            raise CheckpointError("checkpoint_type", type(checkpoint).__name__)
        if not isinstance(checkpoint.scope, dict) or not checkpoint.scope.get("tenant_id"):
            raise CheckpointError("unscoped_checkpoint", checkpoint.run_id)
        self._write(checkpoint, trajectory)
        return checkpoint

    def load(
        self,
        run_id: str,
        *,
        context: Optional[ExecutionContext] = None,
        require: Sequence[str] = ("tenant_id", "owner_id"),
    ) -> WorkflowCheckpoint:
        """Load one checkpoint, re-verifying its scope when ``context`` is given."""
        checkpoint = self.load_raw(run_id)
        if checkpoint is None:
            raise CheckpointError("workflow_run_not_found", run_id)
        if context is not None:
            self.verify_scope(checkpoint, context, require=require)
        return checkpoint

    def load_raw(self, run_id: str) -> Optional[WorkflowCheckpoint]:
        record = self._read(run_id)
        if record is None:
            return None
        return WorkflowCheckpoint.model_validate(record.get("checkpoint") or {})

    def load_trajectory(self, run_id: str) -> Optional[Dict[str, Any]]:
        record = self._read(run_id)
        if record is None:
            return None
        return record.get("trajectory")

    @staticmethod
    def verify_scope(
        checkpoint: WorkflowCheckpoint,
        context: ExecutionContext,
        *,
        require: Sequence[str] = ("tenant_id", "owner_id"),
    ) -> bool:
        """WP-10: the stored scope must cover the caller's scope (deny by default)."""
        return assert_scope_access(context, checkpoint.scope, require=require)

    def exists(self, run_id: str) -> bool:
        return self._read(run_id) is not None

    def record_step(
        self,
        run_id: str,
        step_id: str,
        output: Any,
        *,
        status: CheckpointStatus = "running",
        output_key: Optional[str] = None,
        context: Optional[ExecutionContext] = None,
    ) -> WorkflowCheckpoint:
        """Mark one step complete (idempotent: a step is never recorded twice)."""
        checkpoint = self.load(run_id, context=context)
        if step_id in checkpoint.completed_steps:
            return checkpoint
        completed = list(checkpoint.completed_steps)
        completed.append(step_id)
        outputs = dict(checkpoint.step_outputs)
        key = output_key
        if key is None:
            step = next(
                (item for item in (checkpoint.plan or {}).get("steps") or () if item.get("step_id") == step_id),
                None,
            )
            key = (step or {}).get("output_key") or step_id
        outputs[key] = output
        updated = checkpoint.with_progress(
            status=status, completed_steps=completed, step_outputs=outputs, clear_pending_approval=True,
        )
        self._write(updated, self.load_trajectory(run_id))
        return updated

    def set_status(
        self,
        run_id: str,
        status: CheckpointStatus,
        *,
        pending_approval_id: Optional[str] = None,
        clear_pending_approval: bool = False,
        context: Optional[ExecutionContext] = None,
        failures: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> WorkflowCheckpoint:
        checkpoint = self.load(run_id, context=context)
        updated = checkpoint.with_progress(
            status=status,
            pending_approval_id=pending_approval_id,
            clear_pending_approval=clear_pending_approval,
            failures=failures,
        )
        self._write(updated, self.load_trajectory(run_id))
        return updated

    def list_runs(
        self,
        *,
        tenant_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        status: Optional[CheckpointStatus] = None,
        limit: int = 100,
    ) -> List[WorkflowCheckpoint]:
        records: List[Dict[str, Any]] = []
        with self._lock:
            if self.backend == "memory":
                records = list(self._memory.values())
            elif self.backend == "file":
                assert self._file_root is not None
                for path in sorted(self._file_root.glob("*.json")):
                    try:
                        records.append(json.loads(path.read_text(encoding="utf-8")))
                    except (OSError, json.JSONDecodeError):
                        continue
            else:
                assert self._conn is not None
                rows = self._conn.execute(
                    "SELECT payload, trajectory FROM workflow_checkpoints ORDER BY updated_at DESC"
                ).fetchall()
                records = [{"checkpoint": _decode(row[0])} for row in rows]
        checkpoints: List[WorkflowCheckpoint] = []
        for record in records:
            try:
                checkpoint = WorkflowCheckpoint.model_validate(record.get("checkpoint") or {})
            except Exception:  # noqa: BLE001 - skip corrupt rows instead of failing the listing
                continue
            if tenant_id is not None and checkpoint.scope.get("tenant_id") != tenant_id:
                continue
            if owner_id is not None and checkpoint.scope.get("owner_id") != owner_id:
                continue
            if status is not None and checkpoint.status != status:
                continue
            checkpoints.append(checkpoint)
        checkpoints.sort(key=lambda item: item.updated_at, reverse=True)
        return checkpoints[: max(0, int(limit))]

    def delete(self, run_id: str) -> bool:
        with self._lock:
            if self.backend == "memory":
                return self._memory.pop(run_id, None) is not None
            if self.backend == "file":
                assert self._file_root is not None
                target = self._file_root / f"{run_id}.json"
                if target.is_file():
                    target.unlink()
                    return True
                return False
            assert self._conn is not None
            cursor = self._conn.execute("DELETE FROM workflow_checkpoints WHERE run_id = ?", (run_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def count(self) -> int:
        with self._lock:
            if self.backend == "memory":
                return len(self._memory)
            if self.backend == "file":
                assert self._file_root is not None
                return len(list(self._file_root.glob("*.json")))
            assert self._conn is not None
            return int(self._conn.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
