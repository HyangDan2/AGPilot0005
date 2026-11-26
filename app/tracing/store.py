"""FIX-R1-03: SQLite WAL Append-only Trajectory Event Store & Reconstructor."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from app.config import settings
from app.core.clock import Clock, system_clock
from app.tracing.events import (
    ActionEvent,
    DecisionEvent,
    FeedbackEvent,
    OutcomeEvent,
    TRAJECTORY_SCHEMA_VERSION,
)
from app.tracing.sanitizer import sanitize_mapping

logger = logging.getLogger(__name__)

AnyTrajectoryEvent = Union[DecisionEvent, ActionEvent, OutcomeEvent, FeedbackEvent]


class DuplicateEventError(ValueError):
    """Raised when attempting to append an event with an already-recorded event_id."""


class DuplicateFeedbackError(ValueError):
    """FIX-208: Raised atomically by SQLite UNIQUE index on duplicate/conflicting feedback."""


class FeedbackIntegrityError(RuntimeError):
    """NG-05: Raised when feedback dedup UNIQUE index or NULL-key integrity is unverified (HTTP 503)."""


def _feedback_category(feedback_type: str) -> str:
    if feedback_type in ("thumbs_up", "thumbs_down"):
        return "user_rating"
    return feedback_type


def make_feedback_dedup_key(
    *,
    run_id: str,
    owner_id: Optional[str],
    decision_id: Optional[str],
    feedback_type: str,
) -> str:
    norm_dec = decision_id if decision_id else "__RUN__"
    return f"{run_id}|{owner_id or ''}|{norm_dec}|{_feedback_category(feedback_type)}"


class TrajectoryEventStore:
    """Append-only SQLite WAL event store with engine-level immutability triggers.

    Guarantees:
    - Unique event_id (REG-POL-15)
    - No UPDATE/DELETE allowed via SQLite BEFORE triggers (REG-POL-15)
    - Immediate commit per event so Crash/Kill preserves prior events (REG-POL-16)
    - ActionEvent without OutcomeEvent reconstructed as UNKNOWN without fabricated rewards (REG-POL-17)
    - Delayed FeedbackEvent linked by trajectory_id / run_id / decision_id (REG-POL-18)
    """

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.append_failures: int = 0
        self.feedback_dedup_integrity_ok: bool = False
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        busy_ms = max(100, int(settings.SQLITE_BUSY_TIMEOUT_MS))
        conn = sqlite3.connect(str(self.db_path), timeout=busy_ms / 1000.0, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={busy_ms};")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS trajectory_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL,
                        schema_version TEXT NOT NULL,
                        trajectory_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        decision_id TEXT,
                        action_id TEXT,
                        owner_id TEXT,
                        service_id TEXT,
                        tenant_id TEXT,
                        timestamp REAL NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_traj_events_trajectory
                        ON trajectory_events(trajectory_id, seq);
                    CREATE INDEX IF NOT EXISTS idx_traj_events_run
                        ON trajectory_events(run_id, seq);
                    CREATE TRIGGER IF NOT EXISTS trg_trajectory_events_no_update
                        BEFORE UPDATE ON trajectory_events
                    BEGIN
                        SELECT RAISE(ABORT, 'trajectory_events is append-only: UPDATE forbidden');
                    END;
                    CREATE TRIGGER IF NOT EXISTS trg_trajectory_events_no_delete
                        BEFORE DELETE ON trajectory_events
                    BEGIN
                        SELECT RAISE(ABORT, 'trajectory_events is append-only: DELETE forbidden');
                    END;
                    """
                )
                # Backward-compatible idempotent schema migration (FIX-208 D / HF-02)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(trajectory_events)").fetchall()}
                if "owner_id" not in cols:
                    conn.execute("ALTER TABLE trajectory_events ADD COLUMN owner_id TEXT;")
                if "service_id" not in cols:
                    conn.execute("ALTER TABLE trajectory_events ADD COLUMN service_id TEXT;")
                if "tenant_id" not in cols:
                    conn.execute("ALTER TABLE trajectory_events ADD COLUMN tenant_id TEXT;")
                if "feedback_dedup_key" not in cols:
                    conn.execute("ALTER TABLE trajectory_events ADD COLUMN feedback_dedup_key TEXT;")
                try:
                    conn.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS uq_trajectory_feedback_dedup
                            ON trajectory_events(feedback_dedup_key)
                            WHERE event_type = 'FeedbackEvent' AND feedback_dedup_key IS NOT NULL;
                        """
                    )
                    self.feedback_dedup_integrity_ok = self._verify_feedback_dedup_integrity(conn)
                except sqlite3.IntegrityError:
                    self.feedback_dedup_integrity_ok = False
                    logger.error(
                        "Cannot create unique feedback index due to pre-existing duplicates; "
                        "feedback writes fail-closed (HTTP 503) while existing rows are preserved."
                    )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS incomplete_trajectories (
                        trajectory_id TEXT PRIMARY KEY,
                        reason TEXT NOT NULL,
                        recorded_at REAL NOT NULL
                    );
                    """
                )
            finally:
                conn.close()

    @staticmethod
    def _verify_feedback_dedup_integrity(conn: sqlite3.Connection) -> bool:
        """CR-05 / HF-04: Verifies unique/partial attributes, exact column, exact WHERE predicate,
        0 NULL dedup keys, and active SAVEPOINT runtime dedup enforcement."""
        idx_list = conn.execute("PRAGMA index_list('trajectory_events')").fetchall()
        # PRAGMA index_list columns: (seq, name, unique, origin, partial)
        match = next((r for r in idx_list if r[1] == "uq_trajectory_feedback_dedup"), None)
        if not match or int(match[2]) != 1 or int(match[4]) != 1:
            return False
        cols = [r[2] for r in conn.execute("PRAGMA index_info('uq_trajectory_feedback_dedup')").fetchall()]
        if cols != ["feedback_dedup_key"]:
            return False
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_trajectory_feedback_dedup'"
        ).fetchone()
        raw_sql = (sql_row[0] if sql_row and sql_row[0] else "").strip()
        # HF-04: Strictly normalize and verify the exact WHERE predicate (rejects tampered clauses like AND 1 = 0)
        import re as _re

        m_where = _re.search(r"\bwhere\b(.*)$", raw_sql, flags=_re.IGNORECASE | _re.DOTALL)
        if not m_where:
            return False
        norm_where = _re.sub(r"\s+", " ", m_where.group(1)).strip().rstrip(";").strip().lower()
        norm_where = norm_where.strip("()")
        allowed_predicates = {
            "event_type = 'feedbackevent' and feedback_dedup_key is not null",
            "feedback_dedup_key is not null and event_type = 'feedbackevent'",
        }
        if norm_where not in allowed_predicates:
            return False
        null_fb = conn.execute(
            "SELECT COUNT(*) FROM trajectory_events WHERE event_type = 'FeedbackEvent' AND feedback_dedup_key IS NULL"
        ).fetchone()[0]
        if int(null_fb) != 0:
            return False

        # HF-04: Active SAVEPOINT probe proving duplicate FeedbackEvent raises sqlite3.IntegrityError (rolled back)
        probe_key = f"__dedup_probe_{uuid.uuid4().hex}__"
        probe_ev1 = f"__probe_ev1_{uuid.uuid4().hex}__"
        probe_ev2 = f"__probe_ev2_{uuid.uuid4().hex}__"
        dedup_enforced = False
        try:
            conn.execute("SAVEPOINT sp_dedup_verify;")
            conn.execute(
                """
                INSERT INTO trajectory_events (
                    event_id, event_type, schema_version, trajectory_id, run_id,
                    feedback_dedup_key, timestamp, payload_json
                ) VALUES (?, 'FeedbackEvent', '1.0', '__probe__', '__probe__', ?, 0.0, '{}')
                """,
                (probe_ev1, probe_key),
            )
            try:
                conn.execute(
                    """
                    INSERT INTO trajectory_events (
                        event_id, event_type, schema_version, trajectory_id, run_id,
                        feedback_dedup_key, timestamp, payload_json
                    ) VALUES (?, 'FeedbackEvent', '1.0', '__probe__', '__probe__', ?, 0.0, '{}')
                    """,
                    (probe_ev2, probe_key),
                )
            except sqlite3.IntegrityError:
                dedup_enforced = True
        except Exception:
            dedup_enforced = False
        finally:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT sp_dedup_verify;")
                conn.execute("RELEASE SAVEPOINT sp_dedup_verify;")
            except Exception:
                pass
        return dedup_enforced

    def mark_incomplete(self, trajectory_id: str, reason: str, recorded_at: Optional[float] = None) -> None:
        """CR-08: Persistently records incomplete trajectory; propagates DB failures so Dirty Set is preserved."""
        ts = float(recorded_at if recorded_at is not None else system_clock.now_epoch())
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO incomplete_trajectories (trajectory_id, reason, recorded_at)
                    VALUES (?, ?, ?)
                    """,
                    (trajectory_id, reason, ts),
                )
                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def list_incomplete_trajectories(self) -> set[str]:
        """NG-08: Returns persistently recorded incomplete trajectory IDs."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT trajectory_id FROM incomplete_trajectories").fetchall()
                return {r[0] for r in rows}
            finally:
                conn.close()

    def append_feedback_atomic(
        self,
        event: FeedbackEvent,
        *,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> str:
        """FIX-208 / NG-05 / HF-02: Direct atomic DB commit; fails closed (HTTP 503) if dedup integrity is unverified."""
        if not self.feedback_dedup_integrity_ok:
            raise FeedbackIntegrityError("feedback_dedup_integrity_unverified")
        payload_json = json.dumps(event.model_dump(), ensure_ascii=False, sort_keys=True)
        dedup_key = make_feedback_dedup_key(
            run_id=event.run_id,
            owner_id=owner_id,
            decision_id=event.decision_id,
            feedback_type=event.feedback_type,
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    """
                    INSERT INTO trajectory_events (
                        event_id, event_type, schema_version,
                        trajectory_id, run_id, decision_id, action_id,
                        owner_id, service_id, tenant_id, feedback_dedup_key, timestamp, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.schema_version,
                        event.trajectory_id,
                        event.run_id,
                        event.decision_id,
                        owner_id,
                        service_id,
                        tenant_id,
                        dedup_key,
                        float(event.timestamp),
                        payload_json,
                    ),
                )
                conn.execute("COMMIT;")
                return event.event_id
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK;")
                if "feedback_dedup_key" in str(exc) or "uq_trajectory_feedback_dedup" in str(exc):
                    raise DuplicateFeedbackError(f"Duplicate feedback for key: {dedup_key}") from exc
                raise DuplicateEventError(f"Duplicate trajectory event_id: {event.event_id}") from exc
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                self.append_failures += 1
                raise
            finally:
                conn.close()

    def append(
        self,
        event: AnyTrajectoryEvent,
        *,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> str:
        payload = event.model_dump()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        decision_id = getattr(event, "decision_id", None)
        action_id = getattr(event, "action_id", None)

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    """
                    INSERT INTO trajectory_events (
                        event_id, event_type, schema_version,
                        trajectory_id, run_id, decision_id, action_id,
                        owner_id, service_id, tenant_id, timestamp, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.schema_version,
                        event.trajectory_id,
                        event.run_id,
                        decision_id,
                        action_id,
                        owner_id,
                        service_id,
                        tenant_id,
                        float(event.timestamp),
                        payload_json,
                    ),
                )
                conn.execute("COMMIT;")
                return event.event_id
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK;")
                raise DuplicateEventError(f"Duplicate trajectory event_id: {event.event_id}") from exc
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                self.append_failures += 1
                logger.exception("Failed to append trajectory event %s", event.event_id)
                raise
            finally:
                conn.close()

    def get_run_metadata(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Returns authoritative ownership & tenant metadata for a stored run (R1-FIX-02 / HF-02)."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT trajectory_id, owner_id, service_id, tenant_id, event_type, decision_id
                    FROM trajectory_events
                    WHERE run_id = ?
                    ORDER BY seq ASC
                    """,
                    (run_id,),
                ).fetchall()
            finally:
                conn.close()
        if not rows:
            return None
        trajectory_id = rows[0][0]
        owner_id = next((r[1] for r in rows if r[1]), None)
        service_id = next((r[2] for r in rows if r[2]), None)
        tenant_id = next((r[3] for r in rows if r[3]), None)
        decision_ids = {r[5] for r in rows if r[4] == "DecisionEvent" and r[5]}
        return {
            "run_id": run_id,
            "trajectory_id": trajectory_id,
            "owner_id": owner_id,
            "service_id": service_id,
            "tenant_id": tenant_id,
            "decision_ids": decision_ids,
        }

    def has_duplicate_feedback(
        self,
        *,
        run_id: str,
        owner_id: str,
        decision_id: Optional[str],
        feedback_type: str,
    ) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT owner_id, decision_id, payload_json
                    FROM trajectory_events
                    WHERE run_id = ? AND event_type = 'FeedbackEvent'
                    """,
                    (run_id,),
                ).fetchall()
            finally:
                conn.close()
        for r_owner, r_dec, pjson in rows:
            try:
                p = json.loads(pjson)
            except Exception:
                continue
            if (r_owner or "") == (owner_id or "") and r_dec == decision_id and p.get("feedback_type") == feedback_type:
                return True
        return False

    def list_events(
        self,
        *,
        trajectory_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not trajectory_id and not run_id:
            raise ValueError("Either trajectory_id or run_id must be provided")
        with self._lock:
            conn = self._connect()
            try:
                if trajectory_id:
                    rows = conn.execute(
                        "SELECT payload_json FROM trajectory_events WHERE trajectory_id = ? ORDER BY seq ASC",
                        (trajectory_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT payload_json FROM trajectory_events WHERE run_id = ? ORDER BY seq ASC",
                        (run_id,),
                    ).fetchall()
                return [json.loads(r[0]) for r in rows]
            finally:
                conn.close()

    def reconstruct_trajectory(
        self,
        *,
        trajectory_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reconstructs a unified offline-evaluation trajectory from append-only events.

        - Preserves distinction between policy chosen_action, validator_result, and fallback executed_action.
        - Marks any ActionEvent missing an OutcomeEvent as outcome='UNKNOWN' without fabricating rewards.
        - Attaches delayed FeedbackEvents cleanly.
        """
        events = self.list_events(trajectory_id=trajectory_id, run_id=run_id)
        if not events:
            return {
                "trajectory_id": trajectory_id,
                "run_id": run_id,
                "steps": [],
                "feedback": [],
                "terminal_status": "UNKNOWN",
                "integrity": {"missing_outcomes": 0, "event_count": 0},
            }

        resolved_tid = trajectory_id or events[0].get("trajectory_id")
        resolved_rid = run_id or events[0].get("run_id")

        decisions: Dict[str, Dict[str, Any]] = {}
        decision_order: List[str] = []
        actions_by_decision: Dict[str, List[Dict[str, Any]]] = {}
        outcomes_by_action: Dict[str, Dict[str, Any]] = {}
        run_outcomes: List[Dict[str, Any]] = []
        feedback_events: List[Dict[str, Any]] = []

        for ev in events:
            etype = ev.get("event_type")
            if etype == "DecisionEvent":
                did = ev["decision_id"]
                decisions[did] = ev
                decision_order.append(did)
                actions_by_decision.setdefault(did, [])
            elif etype == "ActionEvent":
                actions_by_decision.setdefault(ev["decision_id"], []).append(ev)
            elif etype == "OutcomeEvent":
                aid = ev.get("action_id")
                if aid:
                    outcomes_by_action[aid] = ev
                if ev.get("decision_id") is None:
                    run_outcomes.append(ev)
            elif etype == "FeedbackEvent":
                feedback_events.append(ev)

        steps: List[Dict[str, Any]] = []
        missing_outcomes = 0
        last_terminal_status: Optional[str] = None

        for did in decision_order:
            dec = decisions[did]
            acts = actions_by_decision.get(did, [])
            step_actions: List[Dict[str, Any]] = []
            for act in acts:
                aid = act["action_id"]
                out = outcomes_by_action.get(aid)
                if out is None:
                    missing_outcomes += 1
                    outcome_record = {
                        "outcome": "UNKNOWN",
                        "reward_signals": {},
                        "terminal_status": "UNKNOWN",
                        "inferred_missing": True,
                    }
                else:
                    outcome_record = out
                    if out.get("terminal_status"):
                        last_terminal_status = out["terminal_status"]
                step_actions.append({"action": act, "outcome": outcome_record})

            steps.append(
                {
                    "decision_id": did,
                    "step_index": dec.get("step_index"),
                    "decision": dec,
                    "chosen_action": dec.get("chosen_action"),
                    "action_probability": dec.get("action_probability"),
                    "validator_result": dec.get("validator_result"),
                    "executions": step_actions,
                    "feedback": [fb for fb in feedback_events if fb.get("decision_id") == did],
                }
            )

        for ro in run_outcomes:
            if ro.get("terminal_status"):
                last_terminal_status = ro["terminal_status"]

        terminal_status = last_terminal_status or "UNKNOWN"
        incomplete_set = self.list_incomplete_trajectories()
        incomplete_reasons: List[str] = []
        if resolved_tid and resolved_tid in incomplete_set:
            incomplete_reasons.append("persisted_incomplete_marker")
        if missing_outcomes > 0:
            incomplete_reasons.append("missing_action_outcomes")
        if terminal_status == "UNKNOWN":
            incomplete_reasons.append("missing_terminal_outcome")

        # CR-09: Referential integrity (orphan actions/outcomes, duplicate decisions, missing actions)
        known_action_ids = {
            act["action_id"]
            for acts in actions_by_decision.values()
            for act in acts
            if act.get("action_id")
        }
        for did_key in actions_by_decision:
            if did_key not in decisions:
                incomplete_reasons.append("orphan_action_without_decision")
                break
        for ev in events:
            if ev.get("event_type") == "OutcomeEvent":
                aid = ev.get("action_id")
                if aid and aid not in known_action_ids:
                    incomplete_reasons.append("orphan_outcome_without_action")
                    break
        if len(decision_order) != len(set(decision_order)):
            incomplete_reasons.append("duplicate_decision_id")
        for did in decision_order:
            if not actions_by_decision.get(did):
                incomplete_reasons.append("decision_missing_action")
                break

        step_indices = [s.get("step_index") for s in steps if s.get("step_index") is not None]
        if step_indices and step_indices != list(range(1, len(step_indices) + 1)):
            incomplete_reasons.append("non_contiguous_step_sequence")

        # CR-09.9: Completion Metadata (expected_event_count) check catches whole-bundle drops
        dao_event_count = sum(1 for e in events if e.get("event_type") in ("DecisionEvent", "ActionEvent", "OutcomeEvent"))
        terminal_outcomes = [
            e for e in events if e.get("event_type") == "OutcomeEvent" and e.get("terminal_status")
        ]
        expected_counts = [
            (e.get("next_state_features") or {}).get("expected_event_count")
            for e in terminal_outcomes
            if isinstance((e.get("next_state_features") or {}).get("expected_event_count"), int)
        ]
        if not expected_counts:
            incomplete_reasons.append("missing_completion_metadata")
        elif expected_counts[-1] != dao_event_count:
            incomplete_reasons.append("event_count_mismatch")

        eligible_for_training = len(incomplete_reasons) == 0 and len(steps) > 0

        return {
            "trajectory_id": resolved_tid,
            "run_id": resolved_rid,
            "steps": steps,
            "run_outcomes": run_outcomes,
            "feedback": feedback_events,
            "terminal_status": terminal_status,
            "eligible_for_training": eligible_for_training,
            "incomplete_reasons": incomplete_reasons,
            "integrity": {
                "missing_outcomes": missing_outcomes,
                "event_count": len(events),
                "has_unconfirmed_actions": missing_outcomes > 0,
            },
        }

    def export_training_jsonl(
        self,
        output_path: Union[str, Path],
        *,
        dirty_trajectories: Optional[set[str]] = None,
        dirty_overflow: bool = False,
        allow_offline_db_only: bool = False,
    ) -> int:
        """NG-08 C / CR-09.13: Training-safe export strictly excluding incomplete, dirty, or overflow-tainted trajectories.
        Fails closed when dirty_trajectories is omitted unless allow_offline_db_only=True is explicitly asserted.
        """
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if dirty_overflow or (dirty_trajectories is None and not allow_offline_db_only):
            out.write_text("", encoding="utf-8")
            return 0
        dirty = set(dirty_trajectories or set())
        with self._lock:
            conn = self._connect()
            try:
                tids = [r[0] for r in conn.execute("SELECT DISTINCT trajectory_id FROM trajectory_events ORDER BY trajectory_id ASC").fetchall()]
            finally:
                conn.close()
        exported = 0
        with out.open("w", encoding="utf-8") as fh:
            for tid in tids:
                if tid in dirty:
                    continue
                rec = self.reconstruct_trajectory(trajectory_id=tid)
                if not rec.get("eligible_for_training"):
                    continue
                fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                exported += 1
        return exported

    def export_jsonl(self, output_path: Union[str, Path], *, trajectory_id: Optional[str] = None) -> int:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                if trajectory_id:
                    rows = conn.execute(
                        "SELECT payload_json FROM trajectory_events WHERE trajectory_id = ? ORDER BY seq ASC",
                        (trajectory_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT payload_json FROM trajectory_events ORDER BY seq ASC"
                    ).fetchall()
            finally:
                conn.close()
        with out.open("w", encoding="utf-8") as fh:
            for (payload_json,) in rows:
                fh.write(payload_json + "\n")
        return len(rows)


class TrajectoryLogger:
    """High-level sanitized logger writing Decision/Action/Outcome/Feedback events."""

    def __init__(self, store: TrajectoryEventStore, clock: Clock = system_clock, async_writer: Optional[Any] = None):
        self.store = store
        self.clock = clock
        self.async_writer = async_writer

    def _emit_event(
        self,
        event: AnyTrajectoryEvent,
        *,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> None:
        if self.async_writer is not None:
            self.async_writer.enqueue(event, owner_id=owner_id, service_id=service_id, tenant_id=tenant_id)
        else:
            self.store.append(event, owner_id=owner_id, service_id=service_id, tenant_id=tenant_id)

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:16]}"

    def log_decision(
        self,
        *,
        trajectory_id: str,
        run_id: str,
        decision_id: str,
        step_index: int,
        decision_type: str,
        state_features: Mapping[str, Any],
        allowed_actions: List[str],
        constraints: Mapping[str, Any],
        chosen_action: str,
        proposed_parameters: Mapping[str, Any],
        behavior_policy_version: str,
        config_digest: str,
        action_probability: Optional[float],
        validator_result: Mapping[str, Any],
        event_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> DecisionEvent:
        ev = DecisionEvent(
            event_id=event_id or self.new_id("dec-evt"),
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            trajectory_id=trajectory_id,
            run_id=run_id,
            decision_id=decision_id,
            step_index=step_index,
            decision_type=decision_type,
            state_features=sanitize_mapping(state_features),
            allowed_actions=list(allowed_actions),
            constraints=sanitize_mapping(constraints),
            chosen_action=chosen_action,
            proposed_parameters=sanitize_mapping(proposed_parameters),
            behavior_policy_version=behavior_policy_version,
            config_digest=config_digest,
            action_probability=action_probability,
            validator_result=sanitize_mapping(validator_result),
            timestamp=self.clock.now_epoch(),
        )
        self._emit_event(ev, owner_id=owner_id, service_id=service_id, tenant_id=tenant_id)
        return ev

    def log_action(
        self,
        *,
        trajectory_id: str,
        run_id: str,
        decision_id: str,
        action_id: str,
        executed_action: str,
        executed_parameters: Mapping[str, Any],
        fallback_applied: bool,
        event_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> ActionEvent:
        ev = ActionEvent(
            event_id=event_id or self.new_id("act-evt"),
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            trajectory_id=trajectory_id,
            run_id=run_id,
            decision_id=decision_id,
            action_id=action_id,
            executed_action=executed_action,
            executed_parameters=sanitize_mapping(executed_parameters),
            fallback_applied=bool(fallback_applied),
            timestamp=self.clock.now_epoch(),
        )
        self._emit_event(ev, owner_id=owner_id, service_id=service_id, tenant_id=tenant_id)
        return ev

    def log_outcome(
        self,
        *,
        trajectory_id: str,
        run_id: str,
        action_id: str,
        outcome: str,
        next_state_features: Mapping[str, Any],
        decision_id: Optional[str] = None,
        reward_signals: Optional[Dict[str, float]] = None,
        execution_latency: float = 0.0,
        token_usage: Optional[Dict[str, int]] = None,
        terminal_status: Optional[str] = None,
        event_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> OutcomeEvent:
        ev = OutcomeEvent(
            event_id=event_id or self.new_id("out-evt"),
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            trajectory_id=trajectory_id,
            run_id=run_id,
            decision_id=decision_id,
            action_id=action_id,
            next_state_features=sanitize_mapping(next_state_features),
            outcome=outcome,
            reward_signals=dict(reward_signals or {}),
            execution_latency=max(0.0, float(execution_latency)),
            token_usage=dict(token_usage or {}),
            terminal_status=terminal_status,
            timestamp=self.clock.now_epoch(),
        )
        self._emit_event(ev, owner_id=owner_id, service_id=service_id, tenant_id=tenant_id)
        return ev

    def log_feedback(
        self,
        *,
        trajectory_id: str,
        run_id: str,
        feedback_type: str,
        reward_signal: float,
        decision_id: Optional[str] = None,
        event_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> FeedbackEvent:
        ev = FeedbackEvent(
            event_id=event_id or self.new_id("fb-evt"),
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            trajectory_id=trajectory_id,
            run_id=run_id,
            decision_id=decision_id,
            feedback_type=feedback_type,
            reward_signal=float(reward_signal),
            timestamp=self.clock.now_epoch(),
        )
        self._emit_event(ev, owner_id=owner_id, service_id=service_id, tenant_id=tenant_id)
        return ev


_default_logger: Optional[TrajectoryLogger] = None
_default_logger_lock = threading.Lock()


def get_trajectory_logger() -> TrajectoryLogger:
    global _default_logger
    with _default_logger_lock:
        if _default_logger is None:
            store = TrajectoryEventStore(settings.TRAJECTORY_DB_PATH)
            _default_logger = TrajectoryLogger(store, clock=system_clock)
        return _default_logger


def shutdown_default_trajectory_logger(timeout_seconds: float = 5.0) -> Optional[Dict[str, Any]]:
    global _default_logger
    with _default_logger_lock:
        if _default_logger is not None and _default_logger.async_writer is not None:
            return _default_logger.async_writer.shutdown(timeout_seconds=timeout_seconds)
    return None
