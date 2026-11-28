"""FIX-02 Durable execution ledger and run-level concurrency guard.

The R3 runtime must never repeat an external side effect because the process was
restarted, a retry fired, or two workers picked up the same checkpoint.  Two primitives
enforce that:

* :class:`ExecutionLedger` — a durable, atomically claimed record per
  ``(run_id, step_id, idempotency_key, scope_key)``.  Before a ``write`` / ``external`` /
  ``destructive`` capability runs, the runtime *claims* the key.  Only one claim can win;
  a finished claim replays its stored result, an ``uncertain`` claim (the transport answer
  was lost) is never retried automatically but reported as ``recovery_required``.
  An **expired claim is not a licence to run again** (FIX-02 P0): the process that held it
  may have dispatched the side effect and died, so an expired lease is turned into an
  ``uncertain`` record and the run holds for ``recovery_required`` until a human resolves it.
* :class:`RunExecutionGuard` — one in-process lease per run so two concurrent
  ``start``/``resume`` calls cannot execute the same checkpoint at once, backed by the
  store's durable lease when that backend supports one.

The ledger is intentionally small and dependency-light (stdlib + pydantic) so it can be
wired into the runtime, the API and the tests without booting FastAPI or a database
server.  SQLite is the default durable backend (``data/workflow_ledger.db``).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LedgerStatus",
    "LedgerClaim",
    "StepLedgerRecord",
    "ExecutionLedger",
    "default_ledger_path",
    "RunBusy",
    "RunExecutionGuard",
    "SIDE_EFFECT_STEPS",
]

LedgerStatus = Literal["claimed", "succeeded", "failed", "uncertain"]

#: Capability side effects that must be claimed in the durable ledger before execution.
SIDE_EFFECT_STEPS: Tuple[str, ...] = ("write", "external", "destructive")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_ledger_path() -> Path:
    """``<repo>/data/workflow_ledger.db`` (``AGCORE_R3_LEDGER_DB`` overrides)."""
    override = (os.environ.get("AGCORE_R3_LEDGER_DB") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data" / "workflow_ledger.db"


class StepLedgerRecord(BaseModel):
    """Durable record of one claimed execution."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    step_id: str
    idempotency_key: str
    scope_key: str = "-"
    attempt: int = 1
    status: LedgerStatus = "claimed"
    side_effect: str = "write"
    claimed_by: Optional[str] = None
    result: Any = None
    error: Optional[str] = None
    claimed_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @property
    def replayable(self) -> bool:
        return self.status == "succeeded"


class LedgerClaim(BaseModel):
    """Outcome of :meth:`ExecutionLedger.claim`."""

    model_config = ConfigDict(extra="forbid")

    granted: bool
    status: str
    record: Optional[StepLedgerRecord] = None
    attempt: int = 1
    detail: str = ""

    @property
    def replay_result(self) -> Any:
        return self.record.result if self.record is not None else None


class ExecutionLedger:
    """Atomic, durable claim ledger for side-effecting workflow steps."""

    def __init__(
        self,
        path: Optional[str | Path] = None,
        *,
        backend: Literal["sqlite", "memory"] = "memory",
        lease_seconds: float = 900.0,
    ) -> None:
        if backend not in ("sqlite", "memory"):
            raise ValueError(f"unknown_ledger_backend: {backend}")
        self.backend = backend
        self.lease_seconds = max(1.0, float(lease_seconds))
        self._lock = threading.RLock()
        self._rows: Dict[Tuple[str, str, str, str], StepLedgerRecord] = {}
        self._conn: Optional[sqlite3.Connection] = None
        self.path: Optional[Path] = None
        if backend == "sqlite":
            self.path = Path(path or default_ledger_path())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_step_ledger (
                    run_id          TEXT NOT NULL,
                    step_id         TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    scope_key       TEXT NOT NULL,
                    attempt         INTEGER NOT NULL,
                    status          TEXT NOT NULL,
                    side_effect     TEXT NOT NULL,
                    claimed_by      TEXT,
                    payload         TEXT NOT NULL,
                    claimed_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    PRIMARY KEY (run_id, step_id, idempotency_key, scope_key)
                )
                """
            )
            conn.commit()
            self._conn = conn

    # -- persistence helpers -----------------------------------------------------------
    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._conn is None:
            with self._lock:
                yield
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    @staticmethod
    def _key(run_id: str, step_id: str, idempotency_key: str, scope_key: str) -> Tuple[str, str, str, str]:
        return (str(run_id), str(step_id), str(idempotency_key), str(scope_key or "-"))

    def _read_row(self, key: Tuple[str, str, str, str]) -> Optional[StepLedgerRecord]:
        if self._conn is None:
            return self._rows.get(key)
        row = self._conn.execute(
            "SELECT payload FROM workflow_step_ledger "
            "WHERE run_id = ? AND step_id = ? AND idempotency_key = ? AND scope_key = ?",
            key,
        ).fetchone()
        if row is None:
            return None
        return StepLedgerRecord.model_validate(json.loads(row[0]))

    def _write_row(self, key: Tuple[str, str, str, str], record: StepLedgerRecord) -> None:
        if self._conn is None:
            self._rows[key] = record
            return
        self._conn.execute(
            """
            INSERT INTO workflow_step_ledger
                (run_id, step_id, idempotency_key, scope_key, attempt, status,
                 side_effect, claimed_by, payload, claimed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, step_id, idempotency_key, scope_key) DO UPDATE SET
                attempt=excluded.attempt,
                status=excluded.status,
                side_effect=excluded.side_effect,
                claimed_by=excluded.claimed_by,
                payload=excluded.payload,
                claimed_at=excluded.claimed_at,
                updated_at=excluded.updated_at
            """,
            (
                key[0], key[1], key[2], key[3],
                record.attempt, record.status, record.side_effect, record.claimed_by,
                json.dumps(record.model_dump(mode="json"), ensure_ascii=False, default=str),
                record.claimed_at.isoformat(), record.updated_at.isoformat(),
            ),
        )

    # -- public API --------------------------------------------------------------------
    def lookup(
        self,
        run_id: str,
        step_id: str,
        *,
        idempotency_key: Optional[str] = None,
        scope_key: str = "-",
    ) -> Optional[StepLedgerRecord]:
        key = self._key(run_id, step_id, idempotency_key or f"{run_id}:{step_id}", scope_key)
        with self._lock:
            return self._read_row(key)

    def claim(
        self,
        run_id: str,
        step_id: str,
        *,
        idempotency_key: Optional[str] = None,
        scope_key: str = "-",
        side_effect: str = "write",
        attempt: int = 1,
        claimed_by: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> LedgerClaim:
        """Reserve the right to execute one side effect.

        ``granted=True``  — this call owns the execution (first attempt, or a retry of a
        recorded failure the operator confirmed).
        ``granted=False`` — ``status`` says why: ``replay`` (already succeeded, result
        attached), ``in_flight`` (another attempt is running), ``recovery_required``
        (a previous attempt's outcome is unknown — an ``uncertain`` record, or a lease
        that expired while the side effect may already have been dispatched — and it must
        not be retried automatically).
        """
        key = self._key(run_id, step_id, idempotency_key or f"{run_id}:{step_id}", scope_key)
        token = claimed_by or uuid.uuid4().hex
        now = now or _utcnow()
        with self._transaction():
            existing = self._read_row(key)
            if existing is None:
                record = StepLedgerRecord(
                    run_id=key[0], step_id=key[1], idempotency_key=key[2], scope_key=key[3],
                    attempt=max(1, int(attempt)), status="claimed", side_effect=side_effect,
                    claimed_by=token, claimed_at=now, updated_at=now,
                )
                self._write_row(key, record)
                return LedgerClaim(granted=True, status="claimed", record=record, attempt=record.attempt)

            if existing.status == "succeeded":
                return LedgerClaim(granted=False, status="replay", record=existing, attempt=existing.attempt)
            if existing.status == "uncertain":
                return LedgerClaim(
                    granted=False, status="recovery_required", record=existing, attempt=existing.attempt,
                    detail=existing.error or "previous_attempt_outcome_unknown",
                )
            if existing.status == "claimed":
                if existing.claimed_by == token:
                    return LedgerClaim(granted=True, status="claimed", record=existing, attempt=existing.attempt)
                if self._lease_expired(existing, now):
                    # FIX-02 P0: an expired lease proves nothing about the side effect.  The
                    # previous executor may have dispatched it and died, so the claim is
                    # *not* handed over: it becomes ``uncertain`` and the run holds for an
                    # explicit ``/recovery`` decision instead of executing a second time.
                    record = existing.model_copy(update={
                        "status": "uncertain",
                        "error": "lease_expired_outcome_unknown",
                        "updated_at": now,
                    })
                    self._write_row(key, record)
                    return LedgerClaim(
                        granted=False, status="recovery_required", record=record,
                        attempt=existing.attempt, detail="lease_expired",
                    )
                return LedgerClaim(
                    granted=False, status="in_flight", record=existing, attempt=existing.attempt,
                    detail="another_attempt_is_running",
                )
            # failed -> a retry may proceed
            record = existing.model_copy(update={
                "status": "claimed", "attempt": existing.attempt + 1, "claimed_by": token,
                "error": None, "claimed_at": now, "updated_at": now, "side_effect": side_effect,
            })
            self._write_row(key, record)
            return LedgerClaim(granted=True, status="claimed", record=record, attempt=record.attempt)

    def _lease_expired(self, record: StepLedgerRecord, now: datetime) -> bool:
        try:
            age = (now - record.claimed_at).total_seconds()
        except TypeError:  # pragma: no cover - defensive for naive datetimes
            return False
        return age > self.lease_seconds

    def succeed(self, run_id: str, step_id: str, result: Any = None, *, idempotency_key: Optional[str] = None,
                scope_key: str = "-") -> Optional[StepLedgerRecord]:
        key = self._key(run_id, step_id, idempotency_key or f"{run_id}:{step_id}", scope_key)
        with self._transaction():
            existing = self._read_row(key)
            if existing is None:
                return None
            record = existing.model_copy(update={
                "status": "succeeded", "result": result, "error": None, "updated_at": _utcnow(),
            })
            self._write_row(key, record)
            return record

    def fail(self, run_id: str, step_id: str, error: str, *, idempotency_key: Optional[str] = None,
             scope_key: str = "-") -> Optional[StepLedgerRecord]:
        key = self._key(run_id, step_id, idempotency_key or f"{run_id}:{step_id}", scope_key)
        with self._transaction():
            existing = self._read_row(key)
            if existing is None:
                return None
            record = existing.model_copy(update={
                "status": "failed", "error": str(error)[:1_000], "updated_at": _utcnow(),
            })
            self._write_row(key, record)
            return record

    def mark_uncertain(self, run_id: str, step_id: str, error: str, *,
                       idempotency_key: Optional[str] = None, scope_key: str = "-") -> Optional[StepLedgerRecord]:
        key = self._key(run_id, step_id, idempotency_key or f"{run_id}:{step_id}", scope_key)
        with self._transaction():
            existing = self._read_row(key)
            if existing is None:
                record = StepLedgerRecord(
                    run_id=key[0], step_id=key[1], idempotency_key=key[2], scope_key=key[3],
                    status="uncertain", error=str(error)[:1_000],
                )
                self._write_row(key, record)
                return record
            record = existing.model_copy(update={
                "status": "uncertain", "error": str(error)[:1_000], "updated_at": _utcnow(),
            })
            self._write_row(key, record)
            return record

    def release(self, run_id: str, step_id: str, *, idempotency_key: Optional[str] = None,
                scope_key: str = "-") -> bool:
        """Drop a claim that never reached the transport (safe: nothing happened yet)."""
        key = self._key(run_id, step_id, idempotency_key or f"{run_id}:{step_id}", scope_key)
        with self._transaction():
            existing = self._read_row(key)
            if existing is None or existing.status != "claimed":
                return False
            if self._conn is None:
                self._rows.pop(key, None)
            else:
                self._conn.execute(
                    "DELETE FROM workflow_step_ledger WHERE run_id = ? AND step_id = ? "
                    "AND idempotency_key = ? AND scope_key = ?",
                    key,
                )
            return True

    def records(self, *, run_id: Optional[str] = None) -> List[StepLedgerRecord]:
        with self._lock:
            if self._conn is None:
                return [r for r in self._rows.values() if run_id is None or r.run_id == run_id]
            if run_id is None:
                rows = self._conn.execute(
                    "SELECT payload FROM workflow_step_ledger ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT payload FROM workflow_step_ledger WHERE run_id = ? ORDER BY updated_at DESC",
                    (run_id,),
                ).fetchall()
            return [StepLedgerRecord.model_validate(json.loads(row[0])) for row in rows]

    def reset(self) -> None:
        with self._transaction():
            if self._conn is None:
                self._rows.clear()
            else:
                self._conn.execute("DELETE FROM workflow_step_ledger")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __len__(self) -> int:
        with self._lock:
            if self._conn is None:
                return len(self._rows)
            return int(self._conn.execute("SELECT COUNT(*) FROM workflow_step_ledger").fetchone()[0])


# --------------------------------------------------------------------------------------
# Run-level execution guard
# --------------------------------------------------------------------------------------
class RunBusy(RuntimeError):
    """Another start/resume already owns this run."""

    def __init__(self, run_id: str, detail: str = "run_in_progress") -> None:
        self.run_id = run_id
        self.code = detail
        super().__init__(f"{detail}: {run_id}")


class RunExecutionGuard:
    """One in-process lease per run, optionally mirrored into the durable store.

    In-process leases stop two ``asyncio`` tasks in the same worker from resuming the same
    checkpoint; ``durable_claim``/``durable_release`` (provided by the checkpoint store)
    extend that to other processes.
    """

    def __init__(self, *, lease_seconds: float = 900.0) -> None:
        self.lease_seconds = max(1.0, float(lease_seconds))
        self._leases: Dict[str, Tuple[str, float]] = {}
        self._lock = threading.RLock()

    def acquire(self, run_id: str, *, holder: Optional[str] = None) -> str:
        token = holder or uuid.uuid4().hex
        now = time.monotonic()
        with self._lock:
            existing = self._leases.get(run_id)
            if existing is not None and existing[1] > now:
                raise RunBusy(run_id)
            self._leases[run_id] = (token, now + self.lease_seconds)
        return token

    def release(self, run_id: str, token: str) -> None:
        with self._lock:
            existing = self._leases.get(run_id)
            if existing is not None and existing[0] == token:
                self._leases.pop(run_id, None)

    def is_held(self, run_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            existing = self._leases.get(run_id)
            return bool(existing and existing[1] > now)
