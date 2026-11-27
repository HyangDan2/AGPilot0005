"""CP-W02: ingestion, chunking and index lifecycle."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.workspace.chunking import CHUNKING_VERSION, chunk_text
from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import IndexState
from app.workspace.indexer import WorkspaceIndexer
from app.workspace.ingest import (
    AGToolsIngestor,
    CompositeIngestor,
    ExtractionError,
    ExtractionResult,
    LocalTextIngestor,
)
from app.workspace.manager import WorkspaceManager
from app.workspace.storage import WorkspaceStorage
from app.workspace.store import WorkspaceStore
from app.workspace.textutil import estimate_tokens, fts_body, fts_query


class Scope:
    tenant_id = "tenant_a"
    owner_id = "owner_a"
    service_id = "hd2ai-admin"


class StubIngestor:
    """Deterministic ingestor used instead of AGTools/LocalText."""

    def __init__(self, error: Exception | None = None, chunk_size: int = 1):
        self.error = error
        self.chunk_size = chunk_size
        self.calls = 0

    def for_scope(self, tenant_id, owner_id):
        return self

    def supports(self, name, mime_type):
        return True

    def extract(self, name, mime_type, data):
        self.calls += 1
        if self.error is not None:
            raise self.error
        from app.workspace.chunking import SourceChunk

        text = data.decode("utf-8", errors="replace")
        lines = [line for line in text.split("\n\n") if line.strip()] or [text]
        chunks = [
            SourceChunk(
                content=line,
                token_count=estimate_tokens(line),
                page_number=index + 1,
                section="section",
                offset_start=0,
                offset_end=len(line),
            )
            for index, line in enumerate(lines[:50])
        ]
        return ExtractionResult(
            chunks=chunks, parser_version="stub-v1", chunking_version="stub", extraction_source="stub"
        )


@pytest.fixture()
def env(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path, index_max_attempts=2)
    store = WorkspaceStore(settings.resolved_db_path())
    storage = WorkspaceStorage(settings.resolved_storage_root(), max_file_bytes=settings.max_file_bytes)
    manager = WorkspaceManager(store, storage, settings)
    stub = StubIngestor()
    indexer = WorkspaceIndexer(manager, settings, stub)
    scope = Scope()
    workspace_id = manager.create_workspace(scope, "indexing").workspace_id
    return SimpleNamespace(
        settings=settings,
        store=store,
        storage=storage,
        manager=manager,
        indexer=indexer,
        stub=stub,
        scope=scope,
        workspace_id=workspace_id,
    )


def _upload(env, name="doc.txt", content=b"hello world", **kwargs):
    return env.manager.upload_file(env.workspace_id, env.scope, name, [content], **kwargs)


# --------------------------------------------------------------------------- chunking
def test_chunking_respects_token_budget_and_overlap():
    text = "\n\n".join(
        f"{index}번째 문단입니다. R2 워크스페이스 문서 파싱 정책을 설명하는 내용입니다. " * 40
        for index in range(6)
    )
    chunks = chunk_text(text, target_tokens=768, overlap_tokens=96, max_tokens=1024)
    assert len(chunks) > 1
    assert all(chunk.token_count <= 1024 for chunk in chunks)
    assert all(chunk.content.strip() for chunk in chunks)
    # consecutive chunks share overlapping text (paragraph carried over)
    assert chunks[0].content[-50:] in (chunks[1].content[:200] + chunks[0].content[-50:])
    joined = "".join(chunk.content for chunk in chunks)
    assert "문서 파싱 정책" in joined


def test_chunking_offsets_and_empty_input():
    assert chunk_text("") == []
    paragraph = "첫 번째 문단의 충분히 긴 내용입니다. " * 12
    text = "\n\n".join([paragraph, paragraph.replace("첫", "둘"), paragraph.replace("첫", "셋")])
    chunks = chunk_text(text, target_tokens=64, overlap_tokens=0, max_tokens=96)
    assert len(chunks) >= 2
    assert chunks[0].offset_start == 0
    assert all(0 <= chunk.offset_start <= chunk.offset_end <= len(text) for chunk in chunks)


def test_korean_tokenizer_expands_bigrams_for_fts():
    body = fts_body("문서 파싱 정책은 중요합니다")
    assert "문서" in body and "파싱" in body
    query = fts_query("파싱 정책")
    assert '"파싱"' in query and '"정책"' in query
    assert " OR " in query


# --------------------------------------------------------------------------- ingestion
def test_local_text_ingestor_and_unsupported_type(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path, agtools_enabled=False)
    local = LocalTextIngestor(settings)
    assert local.supports("notes.md", "text/markdown")
    result = local.extract("notes.md", "text/markdown", "문서 내용입니다.\n\n두 번째 문단.".encode())
    assert result.chunks and result.extraction_source == "local_text"
    assert result.chunking_version == CHUNKING_VERSION

    composite = CompositeIngestor(settings)
    assert composite.extract("notes.md", "text/markdown", b"abc").chunks
    with pytest.raises(ExtractionError) as unsupported:
        composite.extract("scan.pdf", "application/pdf", b"%PDF-1.4")
    assert unsupported.value.code == "unsupported_media_type"


def test_agtools_ingestor_reuses_parser_chunks(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path, agtools_enabled=True)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/documents") and request.method == "POST":
            captured["upload_headers"] = dict(request.headers)
            return httpx.Response(202, json={"document_id": "doc_1", "status": "processing"})
        if path.endswith("/documents/doc_1/status"):
            return httpx.Response(200, json={"document_id": "doc_1", "status": "completed", "parser_version": "agtools-2.1"})
        if path.endswith("/documents/doc_1/outline"):
            return httpx.Response(
                200,
                json={
                    "document_id": "doc_1",
                    "status": "completed",
                    "sections": [
                        {"section_id": "s1", "title": "개요", "level": 1, "source": {"type": "pdf", "page": 1}},
                        {"section_id": "s2", "title": "정책", "level": 1, "source": {"type": "pdf", "page": 4}},
                    ],
                },
            )
        if path.endswith("/documents/doc_1/chunks"):
            return httpx.Response(
                200,
                json={
                    "document_id": "doc_1",
                    "total_chunks": 2,
                    "chunks": [
                        {"chunk_id": "chk_1", "section_id": "s1", "source": {"type": "pdf", "page": 1}, "token_count": 10},
                        {"chunk_id": "chk_2", "section_id": "s2", "source": {"type": "pdf", "page": 4}, "token_count": 12},
                    ],
                    "next_cursor": None,
                },
            )
        if path.endswith("/documents/doc_1/chunks/chk_1"):
            return httpx.Response(
                200,
                json={"chunk_id": "chk_1", "document_id": "doc_1", "parent_section_id": "s1", "content": "첫 번째 파서 청크", "source": {"type": "pdf", "page": 1}, "token_count": 10},
            )
        if path.endswith("/documents/doc_1/chunks/chk_2"):
            return httpx.Response(
                200,
                json={"chunk_id": "chk_2", "document_id": "doc_1", "parent_section_id": "s2", "content": "두 번째 파서 청크", "source": {"type": "pdf", "page": 4}, "token_count": 12},
            )
        return httpx.Response(404, json={"detail": "not found"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://agtools.local/api/v1")
    ingestor = AGToolsIngestor(settings, tenant_id="tenant_a", owner_id="owner_a", client=client)
    result = ingestor.extract("spec.pdf", "application/pdf", b"%PDF-1.4 data")

    assert result.chunking_version == "agtools"  # parser chunks reused, not re-split
    assert [chunk.content for chunk in result.chunks] == ["첫 번째 파서 청크", "두 번째 파서 청크"]
    assert [chunk.page_number for chunk in result.chunks] == [1, 4]
    assert [chunk.section for chunk in result.chunks] == ["개요", "정책"]
    assert captured["upload_headers"]["x-owner-id"] == "owner_a"


def test_agtools_unavailable_is_reported_not_crashed(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path, agtools_enabled=True)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://agtools.local/api/v1")
    ingestor = AGToolsIngestor(settings, client=client)
    with pytest.raises(ExtractionError) as failure:
        ingestor.extract("spec.pdf", "application/pdf", b"data")
    assert failure.value.code == "parser_unavailable"


# --------------------------------------------------------------------------- index lifecycle
def test_upload_triggers_index_job_and_readies(env):
    uploaded = _upload(env, content=b"first paragraph\n\nsecond paragraph")
    jobs = env.indexer.list_jobs(env.workspace_id, env.scope)
    assert len(jobs) == 1 and jobs[0].state is IndexState.PENDING

    assert env.indexer.run_pending(limit=5) == 1
    status = env.indexer.status(env.workspace_id, env.scope)
    assert status.ready == 1 and status.chunks_total == 2
    record = env.store.get_file(env.workspace_id, uploaded.file_id)
    assert record["index_status"] == IndexState.READY.value
    assert env.store.count_chunks_for_file(uploaded.file_id, 1) == 2


def test_stale_revision_job_cannot_overwrite_newer_index(env):
    uploaded = _upload(env, content=b"revision one")
    env.indexer.run_pending(limit=5)
    assert env.store.count_chunks_for_file(uploaded.file_id, 1) == 1

    # A new revision arrives; its job becomes the authoritative one.
    updated = _upload(env, content=b"revision two", overwrite=True)
    assert updated.revision == 2
    env.indexer.run_pending(limit=5)
    assert env.store.count_chunks_for_file(uploaded.file_id, 2) == 1
    assert env.store.count_chunks_for_file(uploaded.file_id, 1) == 0

    # Simulate a late-completing job for the old revision.
    stale_job = env.store.create_index_job(
        {
            "job_id": "wj_stale000001",
            "workspace_id": env.workspace_id,
            "file_id": uploaded.file_id,
            "file_revision": 1,
            "state": "INDEXING",
            "attempts": 1,
            "max_attempts": 2,
            "error_code": None,
            "error_detail": None,
            "content_hash": None,
            "force": 0,
            "created_at": env.indexer.clock(),
            "started_at": env.indexer.clock(),
            "finished_at": None,
        }
    )
    env.indexer.process_job(stale_job)
    assert env.store.get_job("wj_stale000001")["state"] == IndexState.CANCELLED.value
    assert env.store.get_job("wj_stale000001")["error_code"] == "stale_revision"
    assert env.store.count_chunks_for_file(uploaded.file_id, 2) == 1
    assert env.store.count_chunks_for_file(uploaded.file_id, 1) == 0


def test_deleted_file_during_indexing_never_reappears(env):
    uploaded = _upload(env, content=b"to be deleted")
    job = env.store.claim_next_job(env.indexer.clock())
    assert job is not None
    env.manager.delete_file(env.workspace_id, uploaded.file_id, env.scope)

    env.indexer.process_job(job)
    assert env.store.get_job(job["job_id"])["state"] == IndexState.CANCELLED.value
    assert env.store.count_chunks_for_file(uploaded.file_id) == 0
    assert env.store.search_keyword(env.workspace_id, fts_query("deleted"), limit=5) == []


def test_late_indexer_result_keeps_new_revision_pending(env, monkeypatch):
    uploaded = _upload(env, content=b"revision one")
    first_job = env.store.claim_next_job(env.indexer.clock())

    def replace_during_embedding(*_):
        new_revision = _upload(env, content=b"revision two", overwrite=True)
        assert new_revision.revision == 2
        return None

    monkeypatch.setattr(env.indexer, "embedder", object())
    monkeypatch.setattr(env.indexer, "_embed_chunks", replace_during_embedding)
    env.indexer.process_job(first_job)
    file = env.store.get_file(env.workspace_id, uploaded.file_id)
    assert file["revision"] == 2
    assert file["index_status"] == IndexState.PENDING.value
    assert env.store.get_job(first_job["job_id"])["state"] == IndexState.CANCELLED.value


def test_retry_then_failed_with_reason(env):
    env.stub.error = ExtractionError("parser_unavailable", "down")
    uploaded = _upload(env, content=b"retry me")
    env.indexer.run_pending(limit=1)  # attempt 1 -> requeued
    job = env.store.list_jobs(env.workspace_id, file_id=uploaded.file_id)[0]
    assert job["state"] == IndexState.PENDING.value and job["attempts"] == 1
    env.indexer.run_pending(limit=1)  # attempt 2 -> failed
    job = env.store.list_jobs(env.workspace_id, file_id=uploaded.file_id)[0]
    assert job["state"] == IndexState.FAILED.value and job["error_code"] == "parser_unavailable"
    assert env.store.get_file(env.workspace_id, uploaded.file_id)["index_status"] == IndexState.FAILED.value


def test_manual_reindex_cancel_and_dedupe(env):
    uploaded = _upload(env, content=b"dedupe content")
    env.indexer.run_pending(limit=5)

    # Same content, same revision: no duplicate work.
    assert env.indexer.run_pending(limit=5) == 0
    assert env.store.count_chunks_for_file(uploaded.file_id, 1) == 1

    jobs = env.indexer.reindex(env.workspace_id, env.scope, file_id=uploaded.file_id)
    assert jobs and jobs[0].state is IndexState.PENDING
    assert env.indexer.run_pending(limit=5) == 1
    assert env.store.count_chunks_for_file(uploaded.file_id, 1) == 1

    env.indexer.reindex(env.workspace_id, env.scope, file_id=uploaded.file_id)
    cancelled = env.indexer.cancel(env.workspace_id, env.scope, uploaded.file_id)
    assert cancelled.state is IndexState.CANCELLED
    assert env.indexer.run_pending(limit=5) == 0


def test_queue_cap_and_reconciliation(env):
    env.settings.index_max_pending = 1
    files = [_upload(env, f"f{index}.txt", f"content {index}".encode()) for index in range(3)]
    pending_jobs = env.store.count_jobs("PENDING")
    assert pending_jobs == 1
    waiting = env.store.files_awaiting_index(env.workspace_id)
    assert len(waiting) == 2

    env.indexer.run_pending(limit=1)
    env.indexer.reconcile(env.workspace_id)
    assert env.store.count_jobs("PENDING") == 1
    assert env.indexer.run_pending(limit=5) >= 1
    for file in files:
        env.indexer.reconcile(env.workspace_id)
        env.indexer.run_pending(limit=5)
    assert all(
        env.store.get_file(env.workspace_id, file.file_id)["index_status"] == IndexState.READY.value
        for file in files
    )


def test_restart_recovery_requeues_indexing_job(env):
    _upload(env, content=b"restart me")
    job = env.store.claim_next_job(env.indexer.clock())
    assert job["state"] == IndexState.INDEXING.value

    report = env.manager.recover()
    assert report["requeued_jobs"] == 1
    env.indexer.run_pending(limit=5)
    assert env.store.get_job(job["job_id"])["state"] == IndexState.READY.value


def test_index_status_summary_and_embedding_off(env):
    _upload(env, content=b"summary")
    env.indexer.run_pending(limit=5)
    summary = env.indexer.status(env.workspace_id, env.scope)
    assert summary.files_total == 1
    assert summary.ready == 1
    assert summary.embedder.available is False
    assert [mode.value for mode in summary.retrieval_modes] == ["keyword"]
