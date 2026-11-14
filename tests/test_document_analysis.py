import asyncio
import hashlib

import pytest
import httpx

from app.runtime import document_analysis as module


class FakeDocuments:
    def __init__(self, count=26, size=100):
        self.chunks = {f"chk_{index:04d}": f"Section {index} " + "근거 " * size for index in range(count)}

    async def get_outline(self, doc_id):
        return {"status": "completed", "outline": []}

    async def list_chunks(self, doc_id, cursor=None, limit=50):
        offset = int(cursor.split(":")[1]) if cursor else 0
        keys = list(self.chunks)
        end = min(offset + limit, len(keys))
        return {
            "snapshot_id": "snap", "total_chunks": len(keys),
            "chunks": [{"chunk_id": key, "token_count": 50, "section_id": "sec_001",
                        "source": {"type": "pdf", "page": index + 1},
                        "content_hash": hashlib.sha256(self.chunks[key].encode()).hexdigest()}
                       for index, key in enumerate(keys[offset:end], offset)],
            "next_cursor": f"snap:{end}" if end < len(keys) else None,
        }

    async def read_chunk(self, doc_id, chunk_id):
        return {"chunk_id": chunk_id, "content": self.chunks[chunk_id]}


class FakeModel:
    def __init__(self):
        self.fail_once = False
        self.options = []
        self.messages = []

    async def execute(self, messages, **kwargs):
        self.messages.append(messages)
        self.options.append(kwargs)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated provider outage")
        return "근거를 확인했습니다. [doc_test/chk_0000]", [], "stop"


async def finished(manager, job_id):
    for _ in range(200):
        task = manager.tasks[job_id]
        if task.done():
            await task
            return manager.store.read(job_id)
        await asyncio.sleep(0.01)
    raise AssertionError("analysis did not finish")


@pytest.mark.asyncio
@pytest.mark.parametrize("count,size", [(26, 100), (17, 100), (120, 100), (1, 10000)])
async def test_complete_coverage_and_persistence(tmp_path, monkeypatch, count, size):
    monkeypatch.setattr(module, "document_adapter", FakeDocuments(count, size))
    monkeypatch.setattr(module, "gateway_client", FakeModel())
    manager = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    created = await manager.create("alice", "hd2ai-admin", "전체 분석", ["doc_test"])
    job = await finished(manager, created["job_id"])
    assert job["status"] == "completed"
    assert len(job["processed_chunk_ids"]) == count
    assert len(set(job["processed_chunk_ids"])) == count
    assert "분석 범위" in job["answer"]
    reloaded = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    assert reloaded.get(created["job_id"], "alice", "hd2ai-admin")["status"] == "completed"
    assert reloaded.get(created["job_id"], "bob", "hd2ai-admin") is None


@pytest.mark.asyncio
async def test_resume_after_provider_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "document_adapter", FakeDocuments(26))
    model = FakeModel()
    model.fail_once = True
    monkeypatch.setattr(module, "gateway_client", model)
    manager = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    created = await manager.create("alice", "hd2ai-public", "전체 분석", ["doc_test"])
    failed = await finished(manager, created["job_id"])
    assert failed["status"] == "failed"
    assert manager.resume(created["job_id"], "bob", "hd2ai-public") is None
    manager.resume(created["job_id"], "alice", "hd2ai-public")
    completed = await finished(manager, created["job_id"])
    assert completed["status"] == "completed"
    assert len(completed["processed_chunk_ids"]) == 26


@pytest.mark.asyncio
async def test_cancel_and_resume_without_duplicate_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "document_adapter", FakeDocuments(17))
    ready = asyncio.Event()
    class SlowModel(FakeModel):
        async def execute(self, messages, **kwargs):
            ready.set()
            await asyncio.sleep(10)
            return await super().execute(messages, **kwargs)
    monkeypatch.setattr(module, "gateway_client", SlowModel())
    manager = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    created = await manager.create("alice", "hd2ai-public", "전체 분석", ["doc_test"])
    await asyncio.wait_for(ready.wait(), 1)
    manager.cancel(created["job_id"], "alice", "hd2ai-public")
    cancelled = await finished(manager, created["job_id"])
    assert cancelled["status"] == "cancelled"
    monkeypatch.setattr(module, "gateway_client", FakeModel())
    manager.resume(created["job_id"], "alice", "hd2ai-public")
    complete = await finished(manager, created["job_id"])
    assert complete["status"] == "completed"
    assert len(complete["processed_chunk_ids"]) == len(set(complete["processed_chunk_ids"])) == 17


@pytest.mark.asyncio
async def test_snapshot_change_never_claims_completion(tmp_path, monkeypatch):
    docs = FakeDocuments(17)
    monkeypatch.setattr(module, "document_adapter", docs)
    monkeypatch.setattr(module, "gateway_client", FakeModel())
    manager = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    created = await manager.create("alice", "hd2ai-admin", "전체 분석", ["doc_test"])
    completed = await finished(manager, created["job_id"])
    assert completed["status"] == "completed"
    docs.chunks["chk_0000"] = "changed source"
    job = manager.store.read(created["job_id"])
    job["status"] = "partial"
    job["processed_chunk_ids"] = []
    job["processed_parts"] = {}
    manager.store.write(job)
    manager.resume(created["job_id"], "alice", "hd2ai-admin")
    changed = await finished(manager, created["job_id"])
    assert changed["status"] == "failed"
    assert changed["error_code"] == "document_snapshot_changed"


@pytest.mark.asyncio
async def test_gateway_busy_is_retried_within_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "document_adapter", FakeDocuments(1))
    class BusyOnce(FakeModel):
        def __init__(self):
            super().__init__()
            self.busy = True
        async def execute(self, messages, **kwargs):
            if self.busy:
                self.busy = False
                request = httpx.Request("POST", "https://gateway.invalid/")
                response = httpx.Response(429, request=request)
                raise httpx.HTTPStatusError("busy", request=request, response=response)
            return await super().execute(messages, **kwargs)
    monkeypatch.setattr(module, "gateway_client", BusyOnce())
    async def no_wait(_):
        return None
    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    manager = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    created = await manager.create("alice", "hd2ai-admin", "전체 분석", ["doc_test"])
    await manager.tasks[created["job_id"]]
    completed = manager.store.read(created["job_id"])
    assert completed["status"] == "completed"
    assert completed["model_calls"] >= 3


@pytest.mark.asyncio
async def test_analysis_sets_output_and_timeout_budgets(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "document_adapter", FakeDocuments(1))
    model = FakeModel()
    monkeypatch.setattr(module, "gateway_client", model)
    manager = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    created = await manager.create("alice", "hd2ai-admin", "전체 분석", ["doc_test"])
    await manager.tasks[created["job_id"]]
    assert [option["max_tokens"] for option in model.options] == [4096, 2048]
    assert all(option["timeout_seconds"] == module.settings.ANALYSIS_MODEL_TIMEOUT_SECONDS for option in model.options)
    assert all(option["reasoning_effort"] == "off" for option in model.options)
    assert all(messages[0].role == "user" and "분석 지침:" in messages[0].content for messages in model.messages)


@pytest.mark.asyncio
async def test_oversized_note_is_retried_from_original_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "document_adapter", FakeDocuments(1))
    class LongFirst(FakeModel):
        async def execute(self, messages, **kwargs):
            self.messages.append(messages)
            self.options.append(kwargs)
            if len(self.options) == 1:
                return "근거" * 1500, [], "stop"
            return "짧은 근거", [], "stop"
    model = LongFirst()
    monkeypatch.setattr(module, "gateway_client", model)
    manager = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    created = await manager.create("alice", "hd2ai-admin", "전체 분석", ["doc_test"])
    await manager.tasks[created["job_id"]]
    completed = manager.store.read(created["job_id"])
    assert completed["status"] == "completed"
    assert [option["max_tokens"] for option in model.options] == [4096, 2048, 2048]
    assert "원문:" in model.messages[1][0].content


@pytest.mark.asyncio
async def test_output_limit_expands_only_analysis_call_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "document_adapter", FakeDocuments(1))
    class OutputLimitOnce(FakeModel):
        async def execute(self, messages, **kwargs):
            if not self.options:
                self.options.append(kwargs)
                request = httpx.Request("POST", "https://gateway.invalid/")
                response = httpx.Response(422, json={"code": "output_limit"}, request=request)
                raise httpx.HTTPStatusError("output limit", request=request, response=response)
            return await super().execute(messages, **kwargs)
    model = OutputLimitOnce()
    monkeypatch.setattr(module, "gateway_client", model)
    manager = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    created = await manager.create("alice", "hd2ai-public", "전체 분석", ["doc_test"])
    await manager.tasks[created["job_id"]]
    assert manager.store.read(created["job_id"])["status"] == "completed"
    assert [option["max_tokens"] for option in model.options] == [4096, 8192, 2048]


@pytest.mark.asyncio
async def test_output_truncated_expands_budget_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "document_adapter", FakeDocuments(1))
    class TruncatedOnce(FakeModel):
        async def execute(self, messages, **kwargs):
            self.messages.append(messages)
            self.options.append(kwargs)
            if len(self.options) == 1:
                return "부분 출력", [], "length"
            return "완전한 근거", [], "stop"
    model = TruncatedOnce()
    monkeypatch.setattr(module, "gateway_client", model)
    manager = module.DocumentAnalysisManager(module.AnalysisStore(str(tmp_path)))
    created = await manager.create("alice", "hd2ai-admin", "전체 분석", ["doc_test"])
    await manager.tasks[created["job_id"]]
    assert manager.store.read(created["job_id"])["status"] == "completed"
    assert [option["max_tokens"] for option in model.options] == [4096, 8192, 2048]
