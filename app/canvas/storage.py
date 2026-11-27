"""Artifact persistence (WO-C03).

Design notes
------------
* **Independent development database.** Default location is ``<repo>/data/canvas/canvas.db`` +
  ``data/canvas/blobs/``. It is not the operating AGCore store, so no production migration happens
  in R2.
* **Swappable storage adapter.** ``CanvasStorage`` is the protocol the engine uses; the SQLite
  implementation ships here. A Postgres/S3 implementation only has to satisfy the same protocol.
* **Separate responsibilities.** Canvas stores *artifact revisions*; workspace file storage stays
  with the Workspace/RAG engineer. Canvas never writes a workspace blob.
* **Atomicity.** Creating an artifact and appending a revision happen inside one
  ``BEGIN IMMEDIATE`` transaction together with the head pointer update, so metadata and content can
  never disagree. Blob content is content-addressed (sha256) and immutable, therefore a rollback
  only leaves an unreferenced blob, never a corrupt pointer.
* **No false success.** Every write path raises ``storage_failure`` instead of returning a success
  payload when the transaction or blob write fails.
* **Bounded resources.** Content size and per-artifact revision count are checked inside the write
  transaction.
* **Restart recovery.** Nothing is cached in memory; a new process reading the same database and
  blob root returns exactly the persisted artifacts and revisions.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Protocol, Tuple, Union

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.models import Artifact, EditProposal, PatchRequest, Revision
from app.canvas.patching import PatchComputation, compute_patch

SCHEMA_VERSION = 1


def iso_now(epoch: Optional[float] = None) -> str:
    moment = datetime.fromtimestamp(epoch if epoch is not None else datetime.now(timezone.utc).timestamp(), tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def scope_key(tenant_id: str, owner_id: str, service_id: str) -> str:
    return f"{tenant_id}::{owner_id}::{service_id}"


def new_artifact_id() -> str:
    return "art_" + uuid.uuid4().hex[:12]


def new_proposal_id() -> str:
    return "prp_" + uuid.uuid4().hex[:12]


class CanvasStorage(Protocol):
    """Persistence contract used by the Canvas engine."""

    def create_artifact(
        self, artifact: Artifact, revision: Revision, content: str, *,
        event_key: Optional[str] = None,
    ) -> Tuple[Artifact, Revision, bool]: ...

    def get_artifact(self, artifact_id: str, *, include_deleted: bool = False) -> Optional[Artifact]: ...

    def list_artifacts(
        self, *, tenant_id: str, owner_id: str, service_id: str,
        conversation_id: Optional[str] = None, workspace_id: Optional[str] = None,
        artifact_type: Optional[str] = None, limit: int = 25, offset: int = 0,
    ) -> Tuple[List[Artifact], int]: ...

    def update_title(self, artifact_id: str, title: str, updated_at: str) -> Artifact: ...

    def mark_deleted(self, artifact_id: str, deleted_at: str) -> None: ...

    def purge_artifact(self, artifact_id: str) -> None: ...

    def set_links(self, artifact_id: str, *, workspace_id: Any = ..., conversation_id: Any = ...) -> Artifact: ...

    def read_current(self, artifact_id: str) -> Tuple[Artifact, Revision, str]: ...

    def apply_patch(
        self, request: PatchRequest, *, actor: str, actor_id: Optional[str] = None,
        event_key: Optional[str] = None, proposal_id: Optional[str] = None,
    ) -> Tuple[Artifact, Revision, PatchComputation]: ...

    def list_revisions(self, artifact_id: str, *, limit: int = 50, offset: int = 0) -> Tuple[List[Revision], int]: ...

    def get_revision(self, artifact_id: str, revision: int) -> Optional[Revision]: ...

    def read_revision_content(self, artifact_id: str, revision: int) -> Tuple[Revision, str]: ...

    def count_revisions(self, artifact_id: str) -> int: ...

    def get_event_result(self, key_scope: str, idempotency_key: str) -> Optional[Dict[str, Any]]: ...

    def create_proposal(self, proposal: EditProposal, *, tenant_id: str, owner_id: str, service_id: str) -> EditProposal: ...

    def get_proposal(self, proposal_id: str) -> Optional[EditProposal]: ...

    def save_proposal(self, proposal: EditProposal) -> EditProposal: ...

    def list_proposals(
        self, artifact_id: str, *, status: Optional[str] = None, limit: int = 50, offset: int = 0,
    ) -> Tuple[List[EditProposal], int]: ...


class SqliteCanvasStorage:
    """SQLite metadata store + content-addressed blob store."""

    def __init__(
        self,
        db_path: Union[str, Path],
        blob_root: Union[str, Path],
        *,
        max_content_bytes: int = 512 * 1024,
        max_revisions: int = 100,
        busy_timeout_ms: int = 5000,
        clock=None,
    ) -> None:
        self.db_path = Path(db_path)
        self.blob_root = Path(blob_root)
        self.max_content_bytes = int(max_content_bytes)
        self.max_revisions = int(max_revisions)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._clock = clock
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ infrastructure

    def _now(self) -> float:
        if self._clock is not None:
            return float(self._clock.now_epoch())
        return datetime.now(timezone.utc).timestamp()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=self.busy_timeout_ms / 1000.0, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms};")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS canvas_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS canvas_artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        workspace_id TEXT,
                        conversation_id TEXT,
                        tenant_id TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        service_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        artifact_type TEXT NOT NULL,
                        language TEXT,
                        current_revision INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        creation_source TEXT NOT NULL,
                        deleted_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_canvas_artifacts_owner
                        ON canvas_artifacts(tenant_id, owner_id, service_id, deleted_at, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_canvas_artifacts_conversation
                        ON canvas_artifacts(tenant_id, owner_id, conversation_id);
                    CREATE INDEX IF NOT EXISTS idx_canvas_artifacts_workspace
                        ON canvas_artifacts(tenant_id, owner_id, workspace_id);

                    CREATE TABLE IF NOT EXISTS canvas_revisions (
                        artifact_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        actor TEXT NOT NULL,
                        change_reason TEXT,
                        operation TEXT NOT NULL,
                        base_revision INTEGER,
                        created_at REAL NOT NULL,
                        created_by TEXT,
                        proposal_id TEXT,
                        PRIMARY KEY (artifact_id, revision),
                        FOREIGN KEY (artifact_id) REFERENCES canvas_artifacts(artifact_id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS canvas_events (
                        scope_key TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        artifact_id TEXT NOT NULL,
                        revision INTEGER,
                        created_at REAL NOT NULL,
                        PRIMARY KEY (scope_key, idempotency_key)
                    );

                    CREATE TABLE IF NOT EXISTS canvas_edit_proposals (
                        proposal_id TEXT PRIMARY KEY,
                        artifact_id TEXT NOT NULL,
                        tenant_id TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        service_id TEXT NOT NULL,
                        base_revision INTEGER NOT NULL,
                        operation TEXT NOT NULL,
                        selection_json TEXT,
                        expected_original_text TEXT NOT NULL,
                        replacement_text TEXT NOT NULL,
                        change_reason TEXT,
                        status TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        adapter TEXT NOT NULL,
                        model_label TEXT,
                        ai_integration_verified INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        applied_revision INTEGER,
                        request_json TEXT,
                        context_budget_json TEXT,
                        preview_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_canvas_proposals_artifact
                        ON canvas_edit_proposals(artifact_id, status, created_at DESC);
                    """
                )
                conn.execute(
                    "INSERT OR REPLACE INTO canvas_meta(key, value) VALUES('schema_version', ?);",
                    (str(SCHEMA_VERSION),),
                )
            finally:
                conn.close()

    # ------------------------------------------------------------------ blobs

    def _blob_path(self, content_hash: str) -> Path:
        digest = content_hash.split(":", 1)[-1]
        if len(digest) < 4 or any(ch not in "0123456789abcdef" for ch in digest):
            raise CanvasError(CanvasErrorCode.STORAGE_FAILURE, "invalid content hash")
        return self.blob_root / digest[:2] / digest[2:4] / f"{digest}.txt"

    def _write_blob(self, content: str) -> Tuple[str, int]:
        data = content.encode("utf-8")
        if len(data) > self.max_content_bytes:
            raise CanvasError(
                CanvasErrorCode.CONTENT_TOO_LARGE,
                "artifact content exceeds the configured limit",
                context={"size_bytes": len(data), "limit_bytes": self.max_content_bytes},
            )
        digest = sha256(data).hexdigest()
        content_hash = f"sha256:{digest}"
        path = self._blob_path(content_hash)
        if path.exists():
            return content_hash, len(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise CanvasError(
                CanvasErrorCode.STORAGE_FAILURE,
                "content write failed",
                context={"reason": exc.__class__.__name__},
            ) from exc
        return content_hash, len(data)

    def _read_blob(self, content_hash: str) -> str:
        path = self._blob_path(content_hash)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CanvasError(
                CanvasErrorCode.STORAGE_FAILURE,
                "content read failed",
                context={"reason": exc.__class__.__name__},
            ) from exc
        digest = sha256(data).hexdigest()
        if f"sha256:{digest}" != content_hash:
            raise CanvasError(
                CanvasErrorCode.STORAGE_FAILURE,
                "content hash mismatch; refusing to serve corrupt content",
                context={"content_hash": content_hash},
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanvasError(CanvasErrorCode.STORAGE_FAILURE, "stored content is not valid UTF-8") from exc

    # ------------------------------------------------------------------ row mapping

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            artifact_id=row["artifact_id"],
            workspace_id=row["workspace_id"],
            conversation_id=row["conversation_id"],
            tenant_id=row["tenant_id"],
            owner_id=row["owner_id"],
            service_id=row["service_id"],
            title=row["title"],
            artifact_type=row["artifact_type"],
            language=row["language"],
            current_revision=row["current_revision"],
            content_hash=row["content_hash"],
            created_at=iso_now(row["created_at"]),
            updated_at=iso_now(row["updated_at"]),
            creation_source=row["creation_source"],
            deleted_at=iso_now(row["deleted_at"]) if row["deleted_at"] is not None else None,
        )

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> Revision:
        return Revision(
            artifact_id=row["artifact_id"],
            revision=row["revision"],
            content_hash=row["content_hash"],
            size_bytes=row["size_bytes"],
            actor=row["actor"],
            change_reason=row["change_reason"],
            operation=row["operation"],
            base_revision=row["base_revision"],
            created_at=iso_now(row["created_at"]),
            created_by=row["created_by"],
            proposal_id=row["proposal_id"],
        )

    @staticmethod
    def _select_artifact(conn: sqlite3.Connection, artifact_id: str, *, include_deleted: bool) -> Optional[sqlite3.Row]:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM canvas_artifacts WHERE artifact_id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        return conn.execute(sql, (artifact_id,)).fetchone()

    # ------------------------------------------------------------------ artifacts

    def create_artifact(
        self, artifact: Artifact, revision: Revision, content: str, *,
        event_key: Optional[str] = None,
    ) -> Tuple[Artifact, Revision, bool]:
        content_hash, size_bytes = self._write_blob(content)
        now = self._now()
        created_epoch = now
        try:
            with self._tx() as conn:
                if event_key:
                    existing = self._lookup_event(conn, artifact.tenant_id, artifact.owner_id,
                                                  artifact.service_id, event_key)
                    if existing is not None:
                        return self._duplicate_result(conn, existing)
                conn.execute(
                    """
                    INSERT INTO canvas_artifacts (
                        artifact_id, workspace_id, conversation_id, tenant_id, owner_id, service_id,
                        title, artifact_type, language, current_revision, content_hash,
                        created_at, updated_at, creation_source, deleted_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                    """,
                    (
                        artifact.artifact_id, artifact.workspace_id, artifact.conversation_id,
                        artifact.tenant_id, artifact.owner_id, artifact.service_id,
                        artifact.title, artifact.artifact_type, artifact.language,
                        revision.revision, content_hash, created_epoch, created_epoch,
                        artifact.creation_source,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO canvas_revisions (
                        artifact_id, revision, content_hash, size_bytes, actor, change_reason,
                        operation, base_revision, created_at, created_by, proposal_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        artifact.artifact_id, revision.revision, content_hash, size_bytes,
                        revision.actor, revision.change_reason, revision.operation, None,
                        created_epoch, revision.created_by, revision.proposal_id,
                    ),
                )
                if event_key:
                    conn.execute(
                        "INSERT INTO canvas_events(scope_key, idempotency_key, event_type, artifact_id, revision, created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            scope_key(artifact.tenant_id, artifact.owner_id, artifact.service_id),
                            event_key, "artifact.created", artifact.artifact_id, revision.revision,
                            created_epoch,
                        ),
                    )
                row = self._select_artifact(conn, artifact.artifact_id, include_deleted=True)
                rev_row = conn.execute(
                    "SELECT * FROM canvas_revisions WHERE artifact_id=? AND revision=?",
                    (artifact.artifact_id, revision.revision),
                ).fetchone()
                return self._row_to_artifact(row), self._row_to_revision(rev_row), False
        except sqlite3.IntegrityError as exc:
            # A concurrent request with the same idempotency key won the race.
            if event_key:
                with self._connect() as probe:
                    probe.row_factory = sqlite3.Row
                    row = self._lookup_event(probe, artifact.tenant_id, artifact.owner_id,
                                             artifact.service_id, event_key)
                    if row is not None:
                        return self._duplicate_result(probe, row)
            raise CanvasError(
                CanvasErrorCode.STORAGE_FAILURE,
                "artifact insert failed",
                context={"reason": "integrity_error"},
            ) from exc
        except sqlite3.Error as exc:
            raise CanvasError(
                CanvasErrorCode.STORAGE_FAILURE,
                "artifact insert failed",
                context={"reason": exc.__class__.__name__},
            ) from exc

    @staticmethod
    def _lookup_event(conn: sqlite3.Connection, tenant_id: str, owner_id: str, service_id: str,
                      idempotency_key: str) -> Optional[sqlite3.Row]:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM canvas_events WHERE scope_key=? AND idempotency_key=?",
            (scope_key(tenant_id, owner_id, service_id), idempotency_key),
        ).fetchone()

    def _duplicate_result(self, conn: sqlite3.Connection, event_row: sqlite3.Row) -> Tuple[Artifact, Revision, bool]:
        artifact_row = self._select_artifact(conn, event_row["artifact_id"], include_deleted=True)
        if artifact_row is None:
            raise CanvasError(CanvasErrorCode.STORAGE_FAILURE, "idempotency record points at a missing artifact")
        rev_row = conn.execute(
            "SELECT * FROM canvas_revisions WHERE artifact_id=? AND revision=?",
            (event_row["artifact_id"], event_row["revision"] or 1),
        ).fetchone()
        return self._row_to_artifact(artifact_row), self._row_to_revision(rev_row), True

    def get_artifact(self, artifact_id: str, *, include_deleted: bool = False) -> Optional[Artifact]:
        conn = self._connect()
        try:
            row = self._select_artifact(conn, artifact_id, include_deleted=include_deleted)
            return self._row_to_artifact(row) if row else None
        finally:
            conn.close()

    def list_artifacts(
        self, *, tenant_id: str, owner_id: str, service_id: str,
        conversation_id: Optional[str] = None, workspace_id: Optional[str] = None,
        artifact_type: Optional[str] = None, limit: int = 25, offset: int = 0,
    ) -> Tuple[List[Artifact], int]:
        clauses = ["tenant_id=? AND owner_id=? AND service_id=? AND deleted_at IS NULL"]
        params: List[Any] = [tenant_id, owner_id, service_id]
        if conversation_id is not None:
            clauses.append("conversation_id=?")
            params.append(conversation_id)
        if workspace_id is not None:
            clauses.append("workspace_id=?")
            params.append(workspace_id)
        if artifact_type is not None:
            clauses.append("artifact_type=?")
            params.append(artifact_type)
        where = " AND ".join(clauses)
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            total = conn.execute(f"SELECT COUNT(*) AS c FROM canvas_artifacts WHERE {where}", params).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM canvas_artifacts WHERE {where} ORDER BY updated_at DESC, artifact_id ASC LIMIT ? OFFSET ?",
                params + [int(limit), int(offset)],
            ).fetchall()
            return [self._row_to_artifact(row) for row in rows], int(total)
        finally:
            conn.close()

    def update_title(self, artifact_id: str, title: str, updated_at: str) -> Artifact:
        epoch = self._now()
        with self._tx() as conn:
            row = self._select_artifact(conn, artifact_id, include_deleted=False)
            if row is None:
                raise CanvasError(CanvasErrorCode.ARTIFACT_NOT_FOUND, context={"artifact_id": artifact_id})
            conn.execute(
                "UPDATE canvas_artifacts SET title=?, updated_at=? WHERE artifact_id=?",
                (title, epoch, artifact_id),
            )
            return self._row_to_artifact(self._select_artifact(conn, artifact_id, include_deleted=False))

    def mark_deleted(self, artifact_id: str, deleted_at: str) -> None:
        epoch = self._now()
        with self._tx() as conn:
            row = self._select_artifact(conn, artifact_id, include_deleted=False)
            if row is None:
                raise CanvasError(CanvasErrorCode.ARTIFACT_NOT_FOUND, context={"artifact_id": artifact_id})
            conn.execute(
                "UPDATE canvas_artifacts SET deleted_at=?, updated_at=? WHERE artifact_id=?",
                (epoch, epoch, artifact_id),
            )

    def purge_artifact(self, artifact_id: str) -> None:
        with self._tx() as conn:
            row = self._select_artifact(conn, artifact_id, include_deleted=True)
            if row is None:
                raise CanvasError(CanvasErrorCode.ARTIFACT_NOT_FOUND, context={"artifact_id": artifact_id})
            conn.execute("DELETE FROM canvas_edit_proposals WHERE artifact_id=?", (artifact_id,))
            conn.execute("DELETE FROM canvas_revisions WHERE artifact_id=?", (artifact_id,))
            conn.execute("DELETE FROM canvas_events WHERE artifact_id=?", (artifact_id,))
            conn.execute("DELETE FROM canvas_artifacts WHERE artifact_id=?", (artifact_id,))

    def set_links(self, artifact_id: str, *, workspace_id: Any = ..., conversation_id: Any = ...) -> Artifact:
        """Update nullable links; pass ``None`` to clear and omit to keep."""
        updates: Dict[str, Any] = {}
        if workspace_id is not ...:
            updates["workspace_id"] = workspace_id
        if conversation_id is not ...:
            updates["conversation_id"] = conversation_id
        if not updates:
            artifact = self.get_artifact(artifact_id)
            if artifact is None:
                raise CanvasError(CanvasErrorCode.ARTIFACT_NOT_FOUND, context={"artifact_id": artifact_id})
            return artifact
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._tx() as conn:
            row = self._select_artifact(conn, artifact_id, include_deleted=False)
            if row is None:
                raise CanvasError(CanvasErrorCode.ARTIFACT_NOT_FOUND, context={"artifact_id": artifact_id})
            conn.execute(
                f"UPDATE canvas_artifacts SET {assignments}, updated_at=? WHERE artifact_id=?",
                list(updates.values()) + [self._now(), artifact_id],
            )
            return self._row_to_artifact(self._select_artifact(conn, artifact_id, include_deleted=False))

    # ------------------------------------------------------------------ revisions

    def read_current(self, artifact_id: str) -> Tuple[Artifact, Revision, str]:
        """Current artifact row, its current revision row and the content (one consistent read)."""
        conn = self._connect()
        try:
            row = self._select_artifact(conn, artifact_id, include_deleted=False)
            if row is None:
                raise CanvasError(CanvasErrorCode.ARTIFACT_NOT_FOUND, context={"artifact_id": artifact_id})
            artifact = self._row_to_artifact(row)
            revision_row = conn.execute(
                "SELECT * FROM canvas_revisions WHERE artifact_id=? AND revision=?",
                (artifact_id, artifact.current_revision),
            ).fetchone()
            if revision_row is None:
                raise CanvasError(CanvasErrorCode.STORAGE_FAILURE, "current revision row is missing")
            revision = self._row_to_revision(revision_row)
        finally:
            conn.close()
        return artifact, revision, self._read_blob(artifact.content_hash)

    def apply_patch(
        self, request: PatchRequest, *, actor: str, actor_id: Optional[str] = None,
        event_key: Optional[str] = None, proposal_id: Optional[str] = None,
    ) -> Tuple[Artifact, Revision, PatchComputation]:
        now = self._now()
        with self._tx() as conn:
            row = self._select_artifact(conn, request.artifact_id, include_deleted=False)
            if row is None:
                raise CanvasError(CanvasErrorCode.ARTIFACT_NOT_FOUND, context={"artifact_id": request.artifact_id})
            artifact = self._row_to_artifact(row)
            if artifact.current_revision != request.base_revision:
                raise CanvasError(
                    CanvasErrorCode.REVISION_CONFLICT,
                    "base_revision is not the current revision",
                    retryable=True,
                    context={
                        "base_revision": request.base_revision,
                        "current_revision": artifact.current_revision,
                    },
                )
            current = conn.execute(
                "SELECT * FROM canvas_revisions WHERE artifact_id=? AND revision=?",
                (artifact.artifact_id, artifact.current_revision),
            ).fetchone()
            if current is None:
                raise CanvasError(CanvasErrorCode.STORAGE_FAILURE, "current revision row is missing")
            content = self._read_blob(current["content_hash"])
            computation = compute_patch(content, request)

            if not computation.changed:
                return artifact, self._row_to_revision(current), computation

            revision_total = conn.execute(
                "SELECT COUNT(*) AS c FROM canvas_revisions WHERE artifact_id=?",
                (artifact.artifact_id,),
            ).fetchone()["c"]
            if revision_total >= self.max_revisions:
                raise CanvasError(
                    CanvasErrorCode.REVISION_LIMIT_REACHED,
                    "artifact reached its revision retention limit",
                    context={"max_revisions": self.max_revisions, "current_revision": artifact.current_revision},
                )

            content_hash, size_bytes = self._write_blob(computation.content)
            new_revision = artifact.current_revision + 1
            conn.execute(
                """
                INSERT INTO canvas_revisions (
                    artifact_id, revision, content_hash, size_bytes, actor, change_reason,
                    operation, base_revision, created_at, created_by, proposal_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    artifact.artifact_id, new_revision, content_hash, size_bytes, actor,
                    request.change_reason, request.operation, artifact.current_revision,
                    now, actor_id, proposal_id,
                ),
            )
            updated = conn.execute(
                "UPDATE canvas_artifacts SET current_revision=?, content_hash=?, updated_at=? "
                "WHERE artifact_id=? AND current_revision=?",
                (new_revision, content_hash, now, artifact.artifact_id, artifact.current_revision),
            )
            if updated.rowcount != 1:
                raise CanvasError(
                    CanvasErrorCode.REVISION_CONFLICT,
                    "concurrent write detected while committing the revision",
                    retryable=True,
                    context={"base_revision": request.base_revision},
                )
            if event_key:
                conn.execute(
                    "INSERT INTO canvas_events(scope_key, idempotency_key, event_type, artifact_id, revision, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        scope_key(artifact.tenant_id, artifact.owner_id, artifact.service_id),
                        event_key, "artifact.updated", artifact.artifact_id, new_revision, now,
                    ),
                )
            artifact_row = self._select_artifact(conn, artifact.artifact_id, include_deleted=False)
            revision_row = conn.execute(
                "SELECT * FROM canvas_revisions WHERE artifact_id=? AND revision=?",
                (artifact.artifact_id, new_revision),
            ).fetchone()
            return self._row_to_artifact(artifact_row), self._row_to_revision(revision_row), computation

    def list_revisions(self, artifact_id: str, *, limit: int = 50, offset: int = 0) -> Tuple[List[Revision], int]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM canvas_revisions WHERE artifact_id=?", (artifact_id,)
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM canvas_revisions WHERE artifact_id=? ORDER BY revision DESC LIMIT ? OFFSET ?",
                (artifact_id, int(limit), int(offset)),
            ).fetchall()
            return [self._row_to_revision(row) for row in rows], int(total)
        finally:
            conn.close()

    def get_revision(self, artifact_id: str, revision: int) -> Optional[Revision]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM canvas_revisions WHERE artifact_id=? AND revision=?",
                (artifact_id, int(revision)),
            ).fetchone()
            return self._row_to_revision(row) if row else None
        finally:
            conn.close()

    def read_revision_content(self, artifact_id: str, revision: int) -> Tuple[Revision, str]:
        revision_row = self.get_revision(artifact_id, revision)
        if revision_row is None:
            raise CanvasError(
                CanvasErrorCode.REVISION_NOT_FOUND,
                context={"artifact_id": artifact_id, "revision": revision},
            )
        return revision_row, self._read_blob(revision_row.content_hash)

    def count_revisions(self, artifact_id: str) -> int:
        conn = self._connect()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) AS c FROM canvas_revisions WHERE artifact_id=?", (artifact_id,)
            ).fetchone()[0])
        finally:
            conn.close()

    # ------------------------------------------------------------------ events / idempotency

    def get_event_result(self, key_scope: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM canvas_events WHERE scope_key=? AND idempotency_key=?",
                (key_scope, idempotency_key),
            ).fetchone()
            if row is None:
                return None
            return {"artifact_id": row["artifact_id"], "revision": row["revision"], "event_type": row["event_type"]}
        finally:
            conn.close()

    def record_event(
        self, key_scope: str, idempotency_key: str, event_type: str, artifact_id: str,
        revision: Optional[int] = None,
    ) -> bool:
        """Record an event id; returns False when it was already recorded (idempotent replay)."""
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO canvas_events(scope_key, idempotency_key, event_type, artifact_id, revision, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (key_scope, idempotency_key, event_type, artifact_id, revision, self._now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    # ------------------------------------------------------------------ proposals

    def create_proposal(self, proposal: EditProposal, *, tenant_id: str, owner_id: str, service_id: str) -> EditProposal:
        created = self._now()
        try:
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO canvas_edit_proposals (
                        proposal_id, artifact_id, tenant_id, owner_id, service_id, base_revision,
                        operation, selection_json, expected_original_text, replacement_text,
                        change_reason, status, actor, adapter, model_label, ai_integration_verified,
                        created_at, updated_at, applied_revision, request_json, context_budget_json, preview_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        proposal.proposal_id, proposal.artifact_id, tenant_id, owner_id, service_id,
                        proposal.base_revision, proposal.operation,
                        proposal.selection.model_dump_json() if proposal.selection else None,
                        proposal.expected_original_text, proposal.replacement_text,
                        proposal.change_reason, proposal.status, proposal.actor, proposal.adapter,
                        proposal.model_label, 1 if proposal.ai_integration_verified else 0,
                        created, created, proposal.applied_revision,
                        json.dumps(proposal.request, ensure_ascii=False),
                        json.dumps(proposal.context_budget, ensure_ascii=False),
                        json.dumps(proposal.preview, ensure_ascii=False) if proposal.preview else None,
                    ),
                )
        except sqlite3.Error as exc:
            raise CanvasError(
                CanvasErrorCode.STORAGE_FAILURE, "proposal insert failed",
                context={"reason": exc.__class__.__name__},
            ) from exc
        row = self.get_proposal(proposal.proposal_id)
        assert row is not None
        return row

    def _row_to_proposal(self, row: sqlite3.Row) -> EditProposal:
        from app.canvas.offsets import Selection

        selection = Selection(**json.loads(row["selection_json"])) if row["selection_json"] else None
        return EditProposal(
            proposal_id=row["proposal_id"],
            artifact_id=row["artifact_id"],
            base_revision=row["base_revision"],
            operation=row["operation"],
            selection=selection,
            expected_original_text=row["expected_original_text"],
            replacement_text=row["replacement_text"],
            change_reason=row["change_reason"],
            status=row["status"],
            actor=row["actor"],
            adapter=row["adapter"],
            model_label=row["model_label"],
            ai_integration_verified=bool(row["ai_integration_verified"]),
            created_at=iso_now(row["created_at"]),
            updated_at=iso_now(row["updated_at"]),
            applied_revision=row["applied_revision"],
            request=json.loads(row["request_json"] or "{}"),
            context_budget=json.loads(row["context_budget_json"] or "{}"),
            preview=json.loads(row["preview_json"]) if row["preview_json"] else None,
        )

    def get_proposal(self, proposal_id: str) -> Optional[EditProposal]:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM canvas_edit_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            return self._row_to_proposal(row) if row else None
        finally:
            conn.close()

    def save_proposal(self, proposal: EditProposal) -> EditProposal:
        now = self._now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT proposal_id FROM canvas_edit_proposals WHERE proposal_id=?", (proposal.proposal_id,)
            ).fetchone()
            if row is None:
                raise CanvasError(
                    CanvasErrorCode.PROPOSAL_NOT_FOUND, context={"proposal_id": proposal.proposal_id}
                )
            conn.execute(
                """
                UPDATE canvas_edit_proposals
                   SET status=?, replacement_text=?, expected_original_text=?, change_reason=?,
                       operation=?, selection_json=?, applied_revision=?, updated_at=?, preview_json=?
                 WHERE proposal_id=?
                """,
                (
                    proposal.status, proposal.replacement_text, proposal.expected_original_text,
                    proposal.change_reason, proposal.operation,
                    proposal.selection.model_dump_json() if proposal.selection else None,
                    proposal.applied_revision, now,
                    json.dumps(proposal.preview, ensure_ascii=False) if proposal.preview else None,
                    proposal.proposal_id,
                ),
            )
        result = self.get_proposal(proposal.proposal_id)
        assert result is not None
        return result

    def list_proposals(
        self, artifact_id: str, *, status: Optional[str] = None, limit: int = 50, offset: int = 0,
    ) -> Tuple[List[EditProposal], int]:
        where = "artifact_id=?"
        params: List[Any] = [artifact_id]
        if status is not None:
            where += " AND status=?"
            params.append(status)
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM canvas_edit_proposals WHERE {where}", params
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM canvas_edit_proposals WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [int(limit), int(offset)],
            ).fetchall()
            return [self._row_to_proposal(row) for row in rows], int(total)
        finally:
            conn.close()
