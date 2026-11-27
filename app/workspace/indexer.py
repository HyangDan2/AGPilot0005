"""Document indexing: queue, lifecycle states, revision guards and recovery."""
from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.workspace.chunking import SourceChunk
from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import (
    EmbedderStatus,
    IndexJob,
    IndexState,
    IndexSummary,
    SearchMode,
)
from app.workspace.ingest import CompositeIngestor, ExtractionError, ParserUnavailable
from app.workspace.manager import WorkspaceFailure, WorkspaceManager
from app.workspace.textutil import estimate_tokens, fts_body

_RETRYABLE_CODES = {"parser_unavailable", "parser_timeout", "parser_failed", "internal_error", "storage_unavailable"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _chunk_id(file_id: str, revision: int, index: int, content_hash: str) -> str:
    digest = hashlib.sha256(f"{file_id}|{revision}|{index}|{content_hash}".encode("utf-8")).hexdigest()
    return f"wc_{digest[:12]}"


def _citation_id(workspace_id: str, file_id: str, revision: int, chunk_id: str) -> str:
    digest = hashlib.sha256(f"{workspace_id}|{file_id}|{revision}|{chunk_id}".encode("utf-8")).hexdigest()
    return f"cit_{digest[:12]}"


class WorkspaceIndexer:
    def __init__(
        self,
        manager: WorkspaceManager,
        settings: WorkspaceSettings,
        ingestor: CompositeIngestor,
        *,
        embedder: Optional[Any] = None,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.manager = manager
        self.store = manager.store
        self.storage = manager.storage
        self.settings = settings
        self.ingestor = ingestor
        self.embedder = embedder
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        manager.add_file_listener(self._on_file_changed)

    # ------------------------------------------------------------------ queue
    def _on_file_changed(self, workspace_id: str, record: Optional[Dict[str, Any]]) -> None:
        if not record or record.get("kind") != "file":
            return
        try:
            self.enqueue(
                workspace_id,
                record["file_id"],
                int(record["revision"]),
                content_hash=record.get("content_hash"),
            )
        except Exception:
            # Reconciliation will pick the file up on the next run.
            pass

    def enqueue(
        self,
        workspace_id: str,
        file_id: str,
        revision: int,
        *,
        content_hash: Optional[str] = None,
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not force and self.store.count_jobs("PENDING") >= self.settings.index_max_pending:
                return None
            record = {
                "job_id": "wj_" + hashlib.sha256(
                    f"{file_id}|{revision}|{self.clock()}".encode("utf-8")
                ).hexdigest()[:12],
                "workspace_id": workspace_id,
                "file_id": file_id,
                "file_revision": int(revision),
                "state": IndexState.PENDING.value,
                "attempts": 0,
                "max_attempts": int(self.settings.index_max_attempts),
                "error_code": None,
                "error_detail": None,
                "content_hash": content_hash,
                "force": 1 if force else 0,
                "created_at": self.clock(),
                "started_at": None,
                "finished_at": None,
            }
            job = self.store.create_index_job(record)
            if job["state"] in {IndexState.PENDING.value, IndexState.INDEXING.value}:
                self.store.mark_index_status(
                    workspace_id,
                    file_id,
                    IndexState.PENDING.value,
                    error=None,
                    at=self.clock(),
                )
            return job

    def reconcile(self, workspace_id: Optional[str] = None, limit: int = 25) -> int:
        """Create jobs for files left PENDING/STALE without an active job (queue cap, restart)."""

        created = 0
        for record in self.store.files_awaiting_index(workspace_id, limit=limit):
            if self.store.count_jobs("PENDING") >= self.settings.index_max_pending:
                break
            self.enqueue(
                record["workspace_id"],
                record["file_id"],
                int(record["revision"]),
                content_hash=record.get("content_hash"),
            )
            created += 1
        return created

    # ------------------------------------------------------------------ processing
    def run_pending(self, limit: int = 10, workspace_id: Optional[str] = None) -> int:
        """Drain up to `limit` jobs synchronously (tests, CLI and the preview worker)."""

        self.reconcile(workspace_id)
        processed = 0
        while processed < max(1, limit):
            job = self.store.claim_next_job(self.clock())
            if job is None:
                break
            # The queue is process-wide; a claimed job is always processed so no job is lost.
            self._process(job)
            processed += 1
        return processed

    def process_job(self, job: Dict[str, Any]) -> None:
        self._process(job)

    def _fail(
        self,
        job: Dict[str, Any],
        code: str,
        detail: str,
        *,
        retryable: bool,
    ) -> None:
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or self.settings.index_max_attempts)
        at = self.clock()
        if retryable and attempts < max_attempts:
            self.store.requeue_job(job["job_id"], at, error_code=code)
            self.store.mark_index_status(job["workspace_id"], job["file_id"], IndexState.PENDING.value, error=code, at=at)
            return
        self.store.finish_job(job["job_id"], IndexState.FAILED.value, at=at, error_code=code, error_detail=detail)
        self.store.mark_index_status(job["workspace_id"], job["file_id"], IndexState.FAILED.value, error=code, at=at)
        self.manager._emit(job["workspace_id"], job["file_id"], "index.status_changed", int(job["file_revision"]), {"state": "FAILED", "code": code})

    def _process(self, job: Dict[str, Any]) -> None:
        workspace_id = job["workspace_id"]
        file_id = job["file_id"]
        revision = int(job["file_revision"])
        at = self.clock()

        file_record = self.store.get_file(workspace_id, file_id, include_deleted=True)
        if file_record is None or file_record.get("deleted_at"):
            self.store.finish_job(job["job_id"], IndexState.CANCELLED.value, at=at, error_code="file_deleted")
            return
        if int(file_record["revision"]) != revision:
            self.store.finish_job(job["job_id"], IndexState.CANCELLED.value, at=at, error_code="stale_revision")
            return

        # Content-hash dedup: identical revision already indexed.
        existing_chunks = self.store.count_chunks_for_file(file_id, revision)
        if (
            existing_chunks > 0
            and not int(job.get("force") or 0)
            and file_record.get("content_hash")
            and file_record.get("content_hash") == job.get("content_hash", file_record.get("content_hash"))
        ):
            self.store.finish_job(job["job_id"], IndexState.READY.value, at=at)
            self.store.mark_index_status(workspace_id, file_id, IndexState.READY.value, error=None, at=at)
            return

        self.store.mark_index_status(workspace_id, file_id, IndexState.INDEXING.value, error=None, at=at)
        self.manager._emit(workspace_id, file_id, "index.status_changed", revision, {"state": "INDEXING"})

        try:
            workspace = self.store.get_workspace(workspace_id)
            tenant_id = workspace["tenant_id"] if workspace else ""
            try:
                data = self.storage.read_bytes(tenant_id, workspace_id, file_id, revision)
            except Exception as exc:  # blob missing/unreadable
                raise ExtractionError("storage_unavailable", str(exc)) from exc

            ingestor = self.ingestor.for_scope(tenant_id, file_record.get("owner_id") or "")
            result = ingestor.extract(file_record["name"], file_record.get("mime_type"), data)

            chunk_rows = self._build_chunk_rows(
                workspace_id=workspace_id,
                file_id=file_id,
                revision=revision,
                chunks=result.chunks,
                parser_version=result.parser_version,
                chunking_version=result.chunking_version,
            )
            written = self.store.replace_chunks_if_current(workspace_id, file_id, revision, chunk_rows)
            if not written:
                self.store.finish_job(job["job_id"], IndexState.CANCELLED.value, at=self.clock(), error_code="stale_revision")
                return

            embedding_error: Optional[str] = None
            if self.embedder is not None:
                embedding_error = self._embed_chunks(workspace_id, file_id, revision, chunk_rows)

            finished = self.clock()
            self.store.finish_job(job["job_id"], IndexState.READY.value, at=finished, error_code=embedding_error)
            self.store.mark_index_status(workspace_id, file_id, IndexState.READY.value, error=embedding_error, at=finished)
            self.manager._emit(
                workspace_id,
                file_id,
                "index.status_changed",
                revision,
                {"state": "READY", "chunks": len(chunk_rows), "embedding_error": embedding_error},
            )
        except ExtractionError as exc:
            self._fail(job, exc.code, exc.detail or exc.code, retryable=exc.code in _RETRYABLE_CODES)
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(job, "internal_error", str(exc)[:500], retryable=True)

    def _build_chunk_rows(
        self,
        *,
        workspace_id: str,
        file_id: str,
        revision: int,
        chunks: List[SourceChunk],
        parser_version: str,
        chunking_version: str,
    ) -> List[Dict[str, Any]]:
        now = self.clock()
        limited = chunks[: self.settings.index_max_chunks_per_file]
        rows: List[Dict[str, Any]] = []
        for index, chunk in enumerate(limited):
            content = chunk.content
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            chunk_id = _chunk_id(file_id, revision, index, digest)
            rows.append(
                {
                    "chunk_id": chunk_id,
                    "citation_id": _citation_id(workspace_id, file_id, revision, chunk_id),
                    "workspace_id": workspace_id,
                    "file_id": file_id,
                    "file_revision": revision,
                    "chunk_index": index,
                    "content": content,
                    "content_hash": "sha256:" + digest,
                    "token_count": int(chunk.token_count or estimate_tokens(content)),
                    "page_number": chunk.page_number,
                    "section": chunk.section,
                    "offset_start": int(chunk.offset_start),
                    "offset_end": int(chunk.offset_end),
                    "parser_version": parser_version,
                    "chunking_version": chunking_version,
                    "created_at": now,
                    "fts_body": fts_body(content),
                }
            )
        return rows

    def _embed_chunks(
        self, workspace_id: str, file_id: str, revision: int, rows: List[Dict[str, Any]]
    ) -> Optional[str]:
        embedder = self.embedder
        if embedder is None:
            return None
        if not getattr(embedder, "available", False):
            return "embedding_unavailable"
        index_key = getattr(embedder, "index_key", None) or f"{embedder.model_id}:{embedder.dimension}"
        dim = int(embedder.dimension)
        import numpy as np

        try:
            texts = [row["content"] for row in rows]
            vectors: List[List[float]] = []
            batch_size = max(1, int(getattr(embedder, "batch_size", 32)))
            for start in range(0, len(texts), batch_size):
                vectors.extend(embedder.embed_documents(texts[start : start + batch_size]))
            if len(vectors) != len(rows):
                return "embedding_dimension_mismatch"
            prepared = []
            for row, vector in zip(rows, vectors):
                array = np.asarray(vector, dtype=np.float32)
                if array.shape[0] != dim:
                    return "embedding_dimension_mismatch"
                norm = float(np.linalg.norm(array))
                if norm > 0:
                    array = array / norm
                prepared.append((row["chunk_id"], workspace_id, index_key, dim, array.tobytes()))
            self.store.upsert_vectors_if_current(file_id, revision, prepared)
            return None
        except Exception:
            return "embedding_failed"

    # ------------------------------------------------------------------ public API
    def reindex(self, workspace_id: str, scope: Any, file_id: Optional[str] = None) -> List[IndexJob]:
        manager = self.manager
        manager.get_workspace(workspace_id, scope)
        if file_id is not None:
            targets = [manager.get_file_metadata(workspace_id, file_id, scope)]
        else:
            targets = [
                item
                for item in manager.list_files(workspace_id, scope, recursive=True)
                if item.kind.value == "file"
            ]
        jobs: List[IndexJob] = []
        at = self.clock()
        for record in targets:
            self.store.mark_index_status(workspace_id, record.file_id, IndexState.PENDING.value, error=None, at=at)
            job = self.enqueue(
                workspace_id,
                record.file_id,
                int(record.revision),
                content_hash=record.content_hash,
                force=True,
            )
            if job is not None:
                jobs.append(self._job_view(job))
        return jobs

    def cancel(self, workspace_id: str, scope: Any, file_id: str) -> Optional[IndexJob]:
        self.manager.get_workspace(workspace_id, scope)
        self.manager.get_file_metadata(workspace_id, file_id, scope)
        at = self.clock()
        self.store.cancel_jobs_for_file(file_id, at, reason="cancelled")
        self.store.mark_index_status(workspace_id, file_id, IndexState.CANCELLED.value, error="cancelled", at=at)
        jobs = self.store.list_jobs(workspace_id, file_id=file_id, limit=1)
        return self._job_view(jobs[0]) if jobs else None

    def list_jobs(self, workspace_id: str, scope: Any, state: Optional[str] = None, file_id: Optional[str] = None) -> List[IndexJob]:
        self.manager.get_workspace(workspace_id, scope)
        return [self._job_view(row) for row in self.store.list_jobs(workspace_id, state=state, file_id=file_id)]

    def status(self, workspace_id: str, scope: Any) -> IndexSummary:
        self.manager.get_workspace(workspace_id, scope)
        counts = self.store.status_counts(workspace_id)
        embedder = self.embedder
        embedder_status = EmbedderStatus(
            provider=getattr(embedder, "provider_name", "none") if embedder else "none",
            model_id=getattr(embedder, "model_id", "none") if embedder else "none",
            dimension=int(getattr(embedder, "dimension", 0)) if embedder else 0,
            available=bool(getattr(embedder, "available", False)) if embedder else False,
            degraded_reason=None if embedder and getattr(embedder, "available", False) else "embedding_unavailable",
        )
        modes = [SearchMode.KEYWORD]
        if embedder_status.available:
            modes.append(SearchMode.VECTOR)
            modes.append(SearchMode.HYBRID)
        return IndexSummary(
            workspace_id=workspace_id,
            files_total=sum(counts.values()),
            ready=counts.get(IndexState.READY.value, 0),
            indexing=counts.get(IndexState.INDEXING.value, 0),
            pending=counts.get(IndexState.PENDING.value, 0),
            failed=counts.get(IndexState.FAILED.value, 0),
            stale=counts.get(IndexState.STALE.value, 0),
            cancelled=counts.get(IndexState.CANCELLED.value, 0),
            chunks_total=self.store.count_chunks(workspace_id),
            embedder=embedder_status,
            retrieval_modes=modes,
        )

    @staticmethod
    def _job_view(row: Dict[str, Any]) -> IndexJob:
        return IndexJob(
            job_id=row["job_id"],
            workspace_id=row["workspace_id"],
            file_id=row["file_id"],
            file_revision=int(row["file_revision"]),
            state=IndexState(row["state"]),
            attempts=int(row.get("attempts") or 0),
            max_attempts=int(row.get("max_attempts") or 3),
            error_code=row.get("error_code"),
            error_detail=row.get("error_detail"),
            created_at=row["created_at"],
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
        )

    # ------------------------------------------------------------------ worker
    def start(self, interval: float = 0.5) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, args=(interval,), name="workspace-indexer", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=timeout)
        self._worker = None

    def _loop(self, interval: float) -> None:
        concurrency = max(1, int(self.settings.index_max_concurrency))
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ws-index") as pool:
            futures: List[Future] = []
            while not self._stop.is_set():
                try:
                    futures = [future for future in futures if not future.done()]
                    self.reconcile()
                    while len(futures) < concurrency:
                        job = self.store.claim_next_job(self.clock())
                        if job is None:
                            break
                        futures.append(pool.submit(self._process, job))
                    if futures:
                        wait(futures, timeout=interval, return_when=FIRST_COMPLETED)
                    else:
                        self._stop.wait(interval)
                except Exception:  # pragma: no cover - worker must survive
                    self._stop.wait(interval)
