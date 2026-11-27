"""SQLite metadata store for the R2-W workspace engine.

Design notes
------------
* One SQLite database holds workspace/file metadata, revisions, index jobs, chunks, keyword
  index (FTS5) and vectors. File bodies never go in the database (see `storage.py`).
* Connections are opened per operation; WAL + busy timeout matches the AGCore tracing store.
* Every access query carries tenant/owner/service; there is no "global" file lookup.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace (
    workspace_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspace_scope ON workspace(tenant_id, owner_id, service_id, status);

CREATE TABLE IF NOT EXISTS workspace_file (
    file_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    parent_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    index_status TEXT,
    index_error TEXT,
    index_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    FOREIGN KEY (workspace_id) REFERENCES workspace(workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_file_workspace ON workspace_file(workspace_id, parent_id, deleted_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_file_name
    ON workspace_file(workspace_id, IFNULL(parent_id, ''), name)
    WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_file_id_workspace ON workspace_file(file_id, workspace_id);

CREATE TABLE IF NOT EXISTS workspace_file_revision (
    file_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    blob_key TEXT NOT NULL,
    parser_version TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (file_id, revision),
    FOREIGN KEY (file_id) REFERENCES workspace_file(file_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workspace_conversation_ref (
    workspace_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, conversation_id),
    FOREIGN KEY (workspace_id) REFERENCES workspace(workspace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workspace_artifact_ref (
    artifact_ref_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'artifact',
    file_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, artifact_id, kind),
    FOREIGN KEY (workspace_id) REFERENCES workspace(workspace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workspace_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    file_id TEXT,
    type TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_event_workspace ON workspace_event(workspace_id, event_id);

CREATE TABLE IF NOT EXISTS workspace_index_job (
    job_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    file_revision INTEGER NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error_code TEXT,
    error_detail TEXT,
    content_hash TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_state ON workspace_index_job(state, created_at);
CREATE INDEX IF NOT EXISTS idx_job_file ON workspace_index_job(file_id, state);

CREATE TABLE IF NOT EXISTS workspace_chunk (
    chunk_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    file_revision INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    page_number INTEGER,
    section TEXT,
    offset_start INTEGER NOT NULL DEFAULT 0,
    offset_end INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT,
    chunking_version TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_workspace ON workspace_chunk(workspace_id);
CREATE INDEX IF NOT EXISTS idx_chunk_file ON workspace_chunk(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS workspace_chunk_fts USING fts5(
    chunk_id UNINDEXED,
    workspace_id UNINDEXED,
    file_id UNINDEXED,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS workspace_chunk_vector (
    chunk_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    index_key TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (chunk_id, index_key)
);
CREATE INDEX IF NOT EXISTS idx_vector_workspace ON workspace_chunk_vector(workspace_id, index_key);
"""


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


class WorkspaceStore:
    def __init__(self, db_path: Path | str, busy_timeout_ms: int = 5000) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._init_schema()

    # -- connections --------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO workspace_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM workspace_meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    @staticmethod
    def _fts5_available(conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("SELECT count(*) FROM workspace_chunk_fts")
            return True
        except sqlite3.Error:
            return False

    # -- workspaces ---------------------------------------------------------------
    def create_workspace(self, record: Dict[str, Any]) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO workspace(workspace_id, tenant_id, owner_id, service_id, name, description,
                        status, revision, created_at, updated_at)
                   VALUES(:workspace_id, :tenant_id, :owner_id, :service_id, :name, :description,
                        :status, :revision, :created_at, :updated_at)""",
                record,
            )

    def get_workspace(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            return _row(conn.execute("SELECT * FROM workspace WHERE workspace_id=?", (workspace_id,)).fetchone())

    def list_workspaces(self, tenant_id: str, owner_id: str, service_id: Optional[str], include_deleted: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM workspace WHERE tenant_id=? AND owner_id=?"
        params: List[Any] = [tenant_id, owner_id]
        if service_id is not None:
            sql += " AND service_id=?"
            params.append(service_id)
        if not include_deleted:
            sql += " AND status != 'deleted'"
        sql += " ORDER BY updated_at DESC"
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def list_all_workspaces(self) -> List[Dict[str, Any]]:
        with self.read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM workspace").fetchall()]

    def count_workspaces(self, tenant_id: str, owner_id: str) -> int:
        with self.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM workspace WHERE tenant_id=? AND owner_id=? AND status != 'deleted'",
                (tenant_id, owner_id),
            ).fetchone()
        return int(row["c"])

    def update_workspace(self, workspace_id: str, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key}=:{key}" for key in fields)
        with self.write() as conn:
            conn.execute(f"UPDATE workspace SET {columns} WHERE workspace_id=:workspace_id", {**fields, "workspace_id": workspace_id})

    def mark_workspace_deleted(self, workspace_id: str, at: str) -> None:
        with self.write() as conn:
            conn.execute("UPDATE workspace SET status='deleted', updated_at=? WHERE workspace_id=?", (at, workspace_id))

    # -- files --------------------------------------------------------------------
    def insert_file(self, record: Dict[str, Any]) -> None:
        columns = (
            "file_id, workspace_id, parent_id, kind, name, mime_type, size_bytes, content_hash, revision, "
            "index_status, index_error, index_updated_at, created_at, updated_at, deleted_at"
        )
        with self.write() as conn:
            conn.execute(
                f"INSERT INTO workspace_file({columns}) VALUES("
                f"{', '.join(':' + c.strip() for c in columns.split(','))})",
                record,
            )

    def get_file(self, workspace_id: str, file_id: str, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM workspace_file WHERE workspace_id=? AND file_id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        with self.read() as conn:
            return _row(conn.execute(sql, (workspace_id, file_id)).fetchone())

    def find_file_by_name(self, workspace_id: str, parent_id: Optional[str], name: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM workspace_file WHERE workspace_id=? AND IFNULL(parent_id,'')=IFNULL(?,'') "
                    "AND name=? AND deleted_at IS NULL",
                    (workspace_id, parent_id, name),
                ).fetchone()
            )

    def list_files(
        self,
        workspace_id: str,
        parent_id: Optional[str] = None,
        recursive: bool = False,
        include_deleted: bool = False,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM workspace_file WHERE workspace_id=?"
        params: List[Any] = [workspace_id]
        if not recursive:
            sql += " AND IFNULL(parent_id,'')=IFNULL(?,'')"
            params.append(parent_id)
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY kind DESC, name COLLATE NOCASE ASC"
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def update_file(self, workspace_id: str, file_id: str, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key}=:{key}" for key in fields)
        with self.write() as conn:
            conn.execute(
                f"UPDATE workspace_file SET {columns} WHERE workspace_id=:workspace_id AND file_id=:file_id",
                {**fields, "workspace_id": workspace_id, "file_id": file_id},
            )

    def hard_delete_file(self, file_id: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM workspace_file_revision WHERE file_id=?", (file_id,))
            conn.execute("DELETE FROM workspace_file WHERE file_id=?", (file_id,))

    def soft_delete_file(self, workspace_id: str, file_id: str, at: str) -> None:
        with self.write() as conn:
            conn.execute(
                "UPDATE workspace_file SET deleted_at=?, updated_at=?, index_status=NULL WHERE workspace_id=? AND file_id=?",
                (at, at, workspace_id, file_id),
            )

    def list_file_ids_under(self, workspace_id: str, folder_id: str) -> List[str]:
        """All descendant file ids (folders included) of a folder, breadth first."""

        with self.read() as conn:
            rows = conn.execute(
                "SELECT file_id, parent_id, kind FROM workspace_file WHERE workspace_id=? AND deleted_at IS NULL",
                (workspace_id,),
            ).fetchall()
        children: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for row in rows:
            children.setdefault(row["parent_id"], []).append(dict(row))
        collected: List[str] = []
        frontier = [folder_id]
        seen = {folder_id}
        while frontier:
            current = frontier.pop()
            for child in children.get(current, []):
                if child["file_id"] in seen:
                    continue
                seen.add(child["file_id"])
                collected.append(child["file_id"])
                if child["kind"] == "folder":
                    frontier.append(child["file_id"])
        return collected

    def count_files(self, workspace_id: str, kind: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM workspace_file WHERE workspace_id=? AND deleted_at IS NULL"
        params: List[Any] = [workspace_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        with self.read() as conn:
            return int(conn.execute(sql, params).fetchone()["c"])

    def workspace_bytes(self, workspace_id: str) -> int:
        with self.read() as conn:
            row = conn.execute(
                "SELECT IFNULL(SUM(size_bytes), 0) AS total FROM workspace_file "
                "WHERE workspace_id=? AND deleted_at IS NULL AND kind='file'",
                (workspace_id,),
            ).fetchone()
        return int(row["total"])

    def mark_index_status(
        self,
        workspace_id: str,
        file_id: str,
        status: Optional[str],
        *,
        error: Optional[str] = None,
        at: Optional[str] = None,
    ) -> None:
        fields: Dict[str, Any] = {"index_status": status, "index_error": error}
        if at:
            fields["index_updated_at"] = at
        self.update_file(workspace_id, file_id, fields)

    # -- revisions ----------------------------------------------------------------
    def add_revision(self, record: Dict[str, Any]) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO workspace_file_revision(file_id, workspace_id, revision, content_hash, size_bytes,
                        blob_key, parser_version, created_at)
                   VALUES(:file_id, :workspace_id, :revision, :content_hash, :size_bytes, :blob_key,
                        :parser_version, :created_at)""",
                record,
            )

    def get_revision(self, file_id: str, revision: int) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM workspace_file_revision WHERE file_id=? AND revision=?", (file_id, revision)
                ).fetchone()
            )

    def list_revisions(self, file_id: str) -> List[Dict[str, Any]]:
        with self.read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM workspace_file_revision WHERE file_id=? ORDER BY revision DESC", (file_id,)
                ).fetchall()
            ]

    def delete_revisions_except(self, file_id: str, keep_revision: int) -> List[Dict[str, Any]]:
        with self.write() as conn:
            rows = conn.execute(
                "SELECT * FROM workspace_file_revision WHERE file_id=? AND revision != ?", (file_id, keep_revision)
            ).fetchall()
            conn.execute("DELETE FROM workspace_file_revision WHERE file_id=? AND revision != ?", (file_id, keep_revision))
        return [dict(r) for r in rows]

    def delete_revision(self, file_id: str, revision: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM workspace_file_revision WHERE file_id=? AND revision=?", (file_id, revision))

    def delete_revisions(self, file_id: str) -> List[Dict[str, Any]]:
        with self.write() as conn:
            rows = conn.execute("SELECT * FROM workspace_file_revision WHERE file_id=?", (file_id,)).fetchall()
            conn.execute("DELETE FROM workspace_file_revision WHERE file_id=?", (file_id,))
        return [dict(r) for r in rows]

    # -- events -------------------------------------------------------------------
    def append_event(self, workspace_id: str, file_id: Optional[str], event_type: str, revision: int, at: str, payload: Optional[Dict[str, Any]] = None) -> int:
        with self.write() as conn:
            cursor = conn.execute(
                "INSERT INTO workspace_event(workspace_id, file_id, type, revision, at, payload) VALUES(?,?,?,?,?,?)",
                (workspace_id, file_id, event_type, revision, at, json.dumps(payload or {}, ensure_ascii=False)),
            )
            return int(cursor.lastrowid or 0)

    def list_events(self, workspace_id: str, since: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        with self.read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM workspace_event WHERE workspace_id=? AND event_id>? ORDER BY event_id ASC LIMIT ?",
                    (workspace_id, since, max(1, min(limit, 500))),
                ).fetchall()
            ]

    # -- conversation / artifact references ---------------------------------------
    def add_conversation_ref(self, workspace_id: str, conversation_id: str, at: str) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO workspace_conversation_ref(workspace_id, conversation_id, created_at) VALUES(?,?,?)",
                (workspace_id, conversation_id, at),
            )

    def list_conversation_refs(self, workspace_id: str) -> List[Dict[str, Any]]:
        with self.read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM workspace_conversation_ref WHERE workspace_id=? ORDER BY created_at DESC",
                    (workspace_id,),
                ).fetchall()
            ]

    def add_artifact_ref(self, record: Dict[str, Any]) -> None:
        with self.write() as conn:
            conn.execute(
                """INSERT INTO workspace_artifact_ref(artifact_ref_id, workspace_id, artifact_id, kind, file_id,
                        metadata, created_at)
                   VALUES(:artifact_ref_id, :workspace_id, :artifact_id, :kind, :file_id, :metadata, :created_at)""",
                record,
            )

    def list_artifact_refs(self, workspace_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM workspace_artifact_ref WHERE workspace_id=?"
        params: List[Any] = [workspace_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC"
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_artifact_ref(self, workspace_id: str, artifact_ref_id: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM workspace_artifact_ref WHERE workspace_id=? AND artifact_ref_id=?",
                    (workspace_id, artifact_ref_id),
                ).fetchone()
            )

    def delete_artifact_ref(self, workspace_id: str, artifact_ref_id: str) -> bool:
        with self.write() as conn:
            cursor = conn.execute(
                "DELETE FROM workspace_artifact_ref WHERE workspace_id=? AND artifact_ref_id=?",
                (workspace_id, artifact_ref_id),
            )
            return cursor.rowcount > 0

    # -- index jobs ---------------------------------------------------------------
    def create_index_job(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a PENDING job unless an equivalent pending/running job exists."""

        with self.write() as conn:
            existing = conn.execute(
                "SELECT * FROM workspace_index_job WHERE file_id=? AND file_revision=? "
                "AND state IN ('PENDING','INDEXING') LIMIT 1",
                (record["file_id"], record["file_revision"]),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            conn.execute(
                """INSERT INTO workspace_index_job(job_id, workspace_id, file_id, file_revision, state, attempts,
                        max_attempts, error_code, error_detail, content_hash, created_at, started_at, finished_at)
                   VALUES(:job_id, :workspace_id, :file_id, :file_revision, :state, :attempts, :max_attempts,
                        :error_code, :error_detail, :content_hash, :created_at, :started_at, :finished_at)""",
                record,
            )
            return dict(conn.execute("SELECT * FROM workspace_index_job WHERE job_id=?", (record["job_id"],)).fetchone())

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            return _row(conn.execute("SELECT * FROM workspace_index_job WHERE job_id=?", (job_id,)).fetchone())

    def list_jobs(
        self, workspace_id: str, state: Optional[str] = None, file_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM workspace_index_job WHERE workspace_id=?"
        params: List[Any] = [workspace_id]
        if state:
            sql += " AND state=?"
            params.append(state)
        if file_id:
            sql += " AND file_id=?"
            params.append(file_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def count_jobs(self, state: str, workspace_id: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM workspace_index_job WHERE state=?"
        params: List[Any] = [state]
        if workspace_id:
            sql += " AND workspace_id=?"
            params.append(workspace_id)
        with self.read() as conn:
            return int(conn.execute(sql, params).fetchone()["c"])

    def claim_next_job(self, at: str) -> Optional[Dict[str, Any]]:
        """Move the oldest PENDING job to INDEXING (atomic within one transaction)."""

        with self.write() as conn:
            row = conn.execute(
                "SELECT * FROM workspace_index_job WHERE state='PENDING' ORDER BY created_at ASC, job_id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE workspace_index_job SET state='INDEXING', started_at=?, attempts=attempts+1 WHERE job_id=?",
                (at, row["job_id"]),
            )
            return dict(conn.execute("SELECT * FROM workspace_index_job WHERE job_id=?", (row["job_id"],)).fetchone())

    def finish_job(
        self,
        job_id: str,
        state: str,
        *,
        at: str,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> None:
        with self.write() as conn:
            conn.execute(
                "UPDATE workspace_index_job SET state=?, finished_at=?, error_code=?, error_detail=? WHERE job_id=?",
                (state, at, error_code, error_detail, job_id),
            )

    def requeue_job(self, job_id: str, at: str, error_code: Optional[str] = None) -> None:
        with self.write() as conn:
            conn.execute(
                "UPDATE workspace_index_job SET state='PENDING', started_at=NULL, finished_at=NULL, "
                "error_code=?, error_detail=NULL WHERE job_id=?",
                (error_code, job_id),
            )

    def cancel_jobs_for_file(self, file_id: str, at: str, reason: str = "file_changed") -> int:
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE workspace_index_job SET state='CANCELLED', finished_at=?, error_code=? "
                "WHERE file_id=? AND state IN ('PENDING','INDEXING')",
                (at, reason, file_id),
            )
            return cursor.rowcount

    def cancel_job(self, job_id: str, at: str, reason: str = "cancelled") -> bool:
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE workspace_index_job SET state='CANCELLED', finished_at=?, error_code=? "
                "WHERE job_id=? AND state IN ('PENDING','INDEXING')",
                (at, reason, job_id),
            )
            return cursor.rowcount > 0

    def stale_indexing_jobs(self, at: str) -> int:
        """Restart recovery: jobs left in INDEXING (process died) go back to PENDING."""

        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE workspace_index_job SET state='PENDING', started_at=NULL WHERE state='INDEXING'"
            )
            return cursor.rowcount

    def jobs_for_workspace_states(self, workspace_id: str) -> Dict[str, int]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS c FROM workspace_index_job WHERE workspace_id=? GROUP BY state",
                (workspace_id,),
            ).fetchall()
        return {row["state"]: int(row["c"]) for row in rows}

    # -- chunks / FTS / vectors ---------------------------------------------------
    def replace_chunks(self, file_id: str, revision: int, chunks: Sequence[Dict[str, Any]]) -> None:
        with self.write() as conn:
            old = conn.execute("SELECT chunk_id FROM workspace_chunk WHERE file_id=?", (file_id,)).fetchall()
            for row in old:
                conn.execute("DELETE FROM workspace_chunk_fts WHERE chunk_id=?", (row["chunk_id"],))
            conn.execute("DELETE FROM workspace_chunk_vector WHERE chunk_id IN "
                         "(SELECT chunk_id FROM workspace_chunk WHERE file_id=?)", (file_id,))
            conn.execute("DELETE FROM workspace_chunk WHERE file_id=?", (file_id,))
            for chunk in chunks:
                conn.execute(
                    """INSERT INTO workspace_chunk(chunk_id, workspace_id, file_id, file_revision, chunk_index, content,
                            content_hash, token_count, page_number, section, offset_start, offset_end, parser_version,
                            chunking_version, created_at)
                       VALUES(:chunk_id, :workspace_id, :file_id, :file_revision, :chunk_index, :content,
                            :content_hash, :token_count, :page_number, :section, :offset_start, :offset_end,
                            :parser_version, :chunking_version, :created_at)""",
                    chunk,
                )
                conn.execute(
                    "INSERT INTO workspace_chunk_fts(chunk_id, workspace_id, file_id, body) VALUES(?,?,?,?)",
                    (chunk["chunk_id"], chunk["workspace_id"], chunk["file_id"], chunk.get("fts_body") or ""),
                )

    def delete_chunks(self, file_id: str) -> None:
        with self.write() as conn:
            old = conn.execute("SELECT chunk_id FROM workspace_chunk WHERE file_id=?", (file_id,)).fetchall()
            for row in old:
                conn.execute("DELETE FROM workspace_chunk_fts WHERE chunk_id=?", (row["chunk_id"],))
            conn.execute("DELETE FROM workspace_chunk_vector WHERE chunk_id IN "
                         "(SELECT chunk_id FROM workspace_chunk WHERE file_id=?)", (file_id,))
            conn.execute("DELETE FROM workspace_chunk WHERE file_id=?", (file_id,))

    def delete_chunks_revision(self, file_id: str, revision: int) -> None:
        with self.write() as conn:
            old = conn.execute(
                "SELECT chunk_id FROM workspace_chunk WHERE file_id=? AND file_revision=?", (file_id, revision)
            ).fetchall()
            for row in old:
                conn.execute("DELETE FROM workspace_chunk_fts WHERE chunk_id=?", (row["chunk_id"],))
            conn.execute("DELETE FROM workspace_chunk_vector WHERE chunk_id IN "
                         "(SELECT chunk_id FROM workspace_chunk WHERE file_id=? AND file_revision=?)",
                         (file_id, revision))
            conn.execute("DELETE FROM workspace_chunk WHERE file_id=? AND file_revision=?", (file_id, revision))

    def count_chunks(self, workspace_id: str) -> int:
        with self.read() as conn:
            return int(
                conn.execute("SELECT COUNT(*) AS c FROM workspace_chunk WHERE workspace_id=?", (workspace_id,)).fetchone()["c"]
            )

    def count_chunks_for_file(self, file_id: str, revision: Optional[int] = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM workspace_chunk WHERE file_id=?"
        params: List[Any] = [file_id]
        if revision is not None:
            sql += " AND file_revision=?"
            params.append(revision)
        with self.read() as conn:
            return int(conn.execute(sql, params).fetchone()["c"])

    def get_chunk(self, chunk_id: str) -> Optional[Dict[str, Any]]:
        with self.read() as conn:
            return _row(conn.execute("SELECT * FROM workspace_chunk WHERE chunk_id=?", (chunk_id,)).fetchone())

    def chunks_for_file(self, file_id: str) -> List[Dict[str, Any]]:
        with self.read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM workspace_chunk WHERE file_id=? ORDER BY chunk_index ASC", (file_id,)
                ).fetchall()
            ]

    def search_keyword(
        self,
        workspace_id: str,
        match_query: str,
        limit: int,
        file_ids: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        if not match_query.strip():
            return []
        sql = (
            "SELECT c.chunk_id, c.workspace_id, c.file_id, c.file_revision, c.chunk_index, c.content, "
            "c.token_count, c.page_number, c.section, bm25(workspace_chunk_fts) AS rank "
            "FROM workspace_chunk_fts JOIN workspace_chunk c ON c.chunk_id = workspace_chunk_fts.chunk_id "
            "WHERE workspace_chunk_fts MATCH ? AND c.workspace_id = ? AND c.file_id IN "
            "(SELECT file_id FROM workspace_file WHERE workspace_id=? AND deleted_at IS NULL)"
        )
        params: List[Any] = [match_query, workspace_id, workspace_id]
        if file_ids:
            placeholders = ",".join("?" for _ in file_ids)
            sql += f" AND c.file_id IN ({placeholders})"
            params.extend(list(file_ids))
        sql += " ORDER BY rank ASC LIMIT ?"
        params.append(max(1, limit))
        with self.read() as conn:
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            except sqlite3.OperationalError:
                # Malformed FTS expression must not break search; fall back to LIKE scanning.
                return self._like_search(workspace_id, match_query, limit, file_ids)

    def _like_search(self, workspace_id: str, match_query: str, limit: int, file_ids: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
        tokens = [t for t in match_query.replace('"', " ").split() if t and t not in {"OR", "AND"}][:8]
        if not tokens:
            return []
        where = " AND ".join("(c.content LIKE ?)" for _ in tokens)
        params: List[Any] = [workspace_id] + [f"%{t}%" for t in tokens]
        sql = (
            "SELECT c.chunk_id, c.workspace_id, c.file_id, c.file_revision, c.chunk_index, c.content, "
            "c.token_count, c.page_number, c.section, 0.0 AS rank FROM workspace_chunk c "
            f"WHERE c.workspace_id=? AND {where} "
            "AND c.file_id IN (SELECT file_id FROM workspace_file WHERE workspace_id=? AND deleted_at IS NULL)"
        )
        params.append(workspace_id)
        if file_ids:
            placeholders = ",".join("?" for _ in file_ids)
            sql += f" AND c.file_id IN ({placeholders})"
            params.extend(list(file_ids))
        sql += " ORDER BY c.chunk_index ASC LIMIT ?"
        params.append(max(1, limit))
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def upsert_vector(self, chunk_id: str, workspace_id: str, index_key: str, dim: int, vector: bytes) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO workspace_chunk_vector(chunk_id, workspace_id, index_key, dim, vector) "
                "VALUES(?,?,?,?,?) ON CONFLICT(chunk_id, index_key) DO UPDATE SET vector=excluded.vector, dim=excluded.dim",
                (chunk_id, workspace_id, index_key, dim, vector),
            )

    def upsert_vectors(self, rows: Sequence[Tuple[str, str, str, int, bytes]]) -> None:
        if not rows:
            return
        with self.write() as conn:
            conn.executemany(
                "INSERT INTO workspace_chunk_vector(chunk_id, workspace_id, index_key, dim, vector) "
                "VALUES(?,?,?,?,?) ON CONFLICT(chunk_id, index_key) DO UPDATE SET vector=excluded.vector, dim=excluded.dim",
                rows,
            )

    def delete_vectors_for_file(self, file_id: str) -> None:
        with self.write() as conn:
            conn.execute(
                "DELETE FROM workspace_chunk_vector WHERE chunk_id IN (SELECT chunk_id FROM workspace_chunk WHERE file_id=?)",
                (file_id,),
            )

    def load_vectors(self, workspace_id: str, index_key: str) -> List[Dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                """SELECT v.chunk_id, v.dim, v.vector, c.file_id, c.file_revision, c.chunk_index, c.content,
                          c.token_count, c.page_number, c.section
                   FROM workspace_chunk_vector v JOIN workspace_chunk c ON c.chunk_id = v.chunk_id
                   WHERE v.workspace_id=? AND v.index_key=? AND c.workspace_id=?
                     AND c.file_id IN (SELECT file_id FROM workspace_file WHERE workspace_id=? AND deleted_at IS NULL)""",
                (workspace_id, index_key, workspace_id, workspace_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_vectors(self, workspace_id: str, index_key: str) -> int:
        with self.read() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM workspace_chunk_vector WHERE workspace_id=? AND index_key=?",
                    (workspace_id, index_key),
                ).fetchone()["c"]
            )

    # -- workspace purge ----------------------------------------------------------
    def purge_workspace_files(self, workspace_id: str) -> int:
        """Hard-delete every file/chunk/index row of a workspace (used on workspace delete)."""

        with self.write() as conn:
            chunk_ids = [r["chunk_id"] for r in conn.execute(
                "SELECT chunk_id FROM workspace_chunk WHERE workspace_id=?", (workspace_id,)
            ).fetchall()]
            for chunk_id in chunk_ids:
                conn.execute("DELETE FROM workspace_chunk_fts WHERE chunk_id=?", (chunk_id,))
            conn.execute("DELETE FROM workspace_chunk_vector WHERE workspace_id=?", (workspace_id,))
            conn.execute("DELETE FROM workspace_chunk WHERE workspace_id=?", (workspace_id,))
            conn.execute("DELETE FROM workspace_index_job WHERE workspace_id=?", (workspace_id,))
            conn.execute("DELETE FROM workspace_file_revision WHERE workspace_id=?", (workspace_id,))
            conn.execute("DELETE FROM workspace_artifact_ref WHERE workspace_id=?", (workspace_id,))
            conn.execute("DELETE FROM workspace_conversation_ref WHERE workspace_id=?", (workspace_id,))
            cursor = conn.execute("DELETE FROM workspace_file WHERE workspace_id=?", (workspace_id,))
            return cursor.rowcount

    def fts_healthy(self) -> bool:
        with self.read() as conn:
            return self._fts5_available(conn)
