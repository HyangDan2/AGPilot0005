"""CP-W03: embedding providers, Korean keyword search and hybrid retrieval."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import SearchMode, WorkspaceErrorCode
from app.workspace.embedding import (
    GATEWAY_EMBEDDING_ENDPOINT_VERIFIED,
    GatewayEmbeddingProvider,
    LocalHashEmbeddingProvider,
    RemoteEmbeddingProvider,
    build_embedder,
)
from app.workspace.indexer import WorkspaceIndexer
from app.workspace.ingest import ExtractionResult
from app.workspace.manager import WorkspaceFailure, WorkspaceManager
from app.workspace.retrieval import WorkspaceRetriever
from app.workspace.storage import WorkspaceStorage
from app.workspace.store import WorkspaceStore
from app.workspace.textutil import estimate_tokens


class Scope:
    tenant_id = "tenant_a"
    owner_id = "owner_a"
    service_id = "hd2ai-admin"


class ParagraphIngestor:
    """One chunk per blank-line paragraph with a page number."""

    def for_scope(self, tenant_id, owner_id):
        return self

    def supports(self, name, mime_type):
        return True

    def extract(self, name, mime_type, data):
        from app.workspace.chunking import SourceChunk

        text = data.decode("utf-8", errors="replace")
        chunks = [
            SourceChunk(
                content=paragraph,
                token_count=estimate_tokens(paragraph),
                page_number=index + 1,
                section=f"sec-{index + 1}",
                offset_start=0,
                offset_end=len(paragraph),
            )
            for index, paragraph in enumerate(p.strip() for p in text.split("\n\n"))
            if paragraph
        ]
        return ExtractionResult(
            chunks=chunks, parser_version="stub", chunking_version="stub", extraction_source="stub"
        )


class UnavailableEmbedder:
    provider_name = "remote"
    model_id = "remote-model"
    dimension = 16
    batch_size = 8
    max_input_tokens = 1000
    timeout = 1.0
    available = False
    index_key = "remote-model:16"

    def embed_documents(self, texts):
        raise RuntimeError("unavailable")

    def embed_query(self, text):
        raise RuntimeError("unavailable")


@pytest.fixture()
def env(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path)
    store = WorkspaceStore(settings.resolved_db_path())
    storage = WorkspaceStorage(settings.resolved_storage_root(), max_file_bytes=settings.max_file_bytes)
    manager = WorkspaceManager(store, storage, settings)
    embedder = LocalHashEmbeddingProvider(settings)
    indexer = WorkspaceIndexer(manager, settings, ParagraphIngestor(), embedder=embedder)
    retriever = WorkspaceRetriever(store, settings, embedder)

    scope = Scope()
    workspace_id = manager.create_workspace(scope, "retrieval").workspace_id
    policy = (
        "문서 파싱 정책은 PDF와 DOCX 파일을 지원합니다.\n\n"
        "파서 버전은 2.1이며 페이지 정보를 함께 추출합니다.\n\n"
        "정책상 암호가 걸린 문서는 파싱하지 않습니다."
    )
    storage_doc = (
        "워크스페이스 저장 용량은 512MB입니다.\n\n"
        "파일 본문은 저장소 루트에 보관되고 메타데이터는 SQLite에 저장됩니다."
    )
    manager.upload_file(workspace_id, scope, "policy.txt", [policy.encode()])
    manager.upload_file(workspace_id, scope, "storage.txt", [storage_doc.encode()])
    indexer.run_pending(limit=10)
    return SimpleNamespace(
        settings=settings,
        store=store,
        manager=manager,
        indexer=indexer,
        retriever=retriever,
        embedder=embedder,
        scope=scope,
        workspace_id=workspace_id,
    )


# --------------------------------------------------------------------------- providers
def test_local_embedding_is_deterministic_and_normalized(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path, embedding_dimension=64)
    provider = LocalHashEmbeddingProvider(settings)
    assert provider.dimension == 64 and provider.available
    first = provider.embed_documents(["문서 파싱 정책", "workspace storage"])[0]
    second = provider.embed_query("문서 파싱 정책")
    assert first == second
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6
    assert provider.index_key == f"{provider.model_id}:64"

    unrelated = provider.embed_query("날씨와 커피")
    similarity = sum(a * b for a, b in zip(first, unrelated))
    assert similarity < 0.9


def test_remote_provider_openai_and_ollama_shapes(tmp_path):
    settings = WorkspaceSettings.for_testing(
        tmp_path, embedding_provider="remote", embedding_url="http://embed.local/v1/embeddings", embedding_dimension=3
    )

    def openai_handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "input" in body
        count = 1 if '"ping"' in body else 2
        payload = [{"index": 0, "embedding": [3, 0, 4]}]
        if count == 2:
            payload.append({"index": 1, "embedding": [0, 1, 0]})
        return httpx.Response(200, json={"data": payload})

    client = httpx.Client(transport=httpx.MockTransport(openai_handler))
    provider = RemoteEmbeddingProvider(settings, client=client)
    vectors = provider.embed_documents(["첫 문서", "second"])
    assert [round(value, 4) for value in vectors[0]] == [0.6, 0.0, 0.8]
    assert provider.available is True

    def ollama_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[1, 0, 0]]})

    settings.embedding_url = "http://ollama.local/api/embed"
    ollama = RemoteEmbeddingProvider(settings, client=httpx.Client(transport=httpx.MockTransport(ollama_handler)))
    assert ollama.embed_documents(["한 문서"]) == [[1.0, 0.0, 0.0]]


def test_gateway_embedding_endpoint_is_not_assumed(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path)
    assert GATEWAY_EMBEDDING_ENDPOINT_VERIFIED is False
    gateway = GatewayEmbeddingProvider(settings)
    assert gateway.available is False
    with pytest.raises(Exception):
        gateway.embed_documents(["x"])


def test_build_embedder_modes(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path)
    settings.embedding_provider = "auto"
    assert isinstance(build_embedder(settings), LocalHashEmbeddingProvider)
    settings.embedding_url = "http://embed.local/v1/embeddings"
    assert isinstance(build_embedder(settings), RemoteEmbeddingProvider)
    settings.embedding_provider = "gateway"
    assert isinstance(build_embedder(settings), GatewayEmbeddingProvider)


# --------------------------------------------------------------------------- keyword
def test_korean_keyword_search_matches_with_particles(env):
    response = env.retriever.search(env.workspace_id, "파싱 정책", mode=SearchMode.KEYWORD)
    assert response.total_matches >= 1
    assert response.results[0].file_name == "policy.txt"
    assert "파싱" in response.results[0].excerpt

    # query with a particle still matches (bigram expansion)
    particle = env.retriever.search(env.workspace_id, "정책은", mode=SearchMode.KEYWORD)
    assert particle.total_matches >= 1
    assert particle.results[0].file_name == "policy.txt"

    unrelated = env.retriever.search(env.workspace_id, "날씨 커피", mode=SearchMode.KEYWORD)
    assert unrelated.total_matches == 0


def test_keyword_filters_by_file_and_mime(env):
    policy_file = next(
        item for item in env.manager.list_files(env.workspace_id, env.scope, recursive=True) if item.name == "policy.txt"
    )
    scoped = env.retriever.search(
        env.workspace_id, "저장", mode=SearchMode.KEYWORD, file_ids=[policy_file.file_id]
    )
    assert scoped.total_matches == 0
    mime = env.retriever.search(env.workspace_id, "저장", mode=SearchMode.KEYWORD, mime_types=["application/pdf"])
    assert mime.total_matches == 0
    text_mime = env.retriever.search(env.workspace_id, "저장", mode=SearchMode.KEYWORD, mime_types=["text/plain"])
    assert text_mime.total_matches >= 1


# --------------------------------------------------------------------------- vector / hybrid
def test_vector_search_ranks_related_document_first(env):
    response = env.retriever.search(env.workspace_id, "PDF 파서 버전", mode=SearchMode.VECTOR)
    assert response.mode_effective is SearchMode.VECTOR
    assert response.total_matches >= 1
    assert response.results[0].file_name == "policy.txt"
    assert response.results[0].matched_by == ["vector"]
    assert response.results[0].vector_score and response.results[0].vector_score > 0.5


def test_hybrid_search_fuses_keyword_and_vector(env):
    response = env.retriever.search(env.workspace_id, "파싱 정책 PDF", mode=SearchMode.HYBRID)
    assert response.mode_effective is SearchMode.HYBRID
    assert response.degraded is False
    top = response.results[0]
    assert top.file_name == "policy.txt"
    assert "keyword" in top.matched_by and "vector" in top.matched_by
    assert 0.0 < top.score <= 1.0
    assert top.keyword_score is not None and top.vector_score is not None
    # scores are fused by rank, not by summing raw component scores
    assert top.score != (top.keyword_score or 0) + (top.vector_score or 0)


def test_hybrid_degrades_to_keyword_when_embedding_unavailable(env):
    retriever = WorkspaceRetriever(env.store, env.settings, UnavailableEmbedder())
    response = retriever.search(env.workspace_id, "파싱 정책", mode=SearchMode.HYBRID)
    assert response.mode_effective is SearchMode.KEYWORD
    assert response.degraded is True
    assert response.degradation_reason == "embedding_unavailable"
    assert response.provider_status.embedding == "unavailable"
    assert response.total_matches >= 1

    with pytest.raises(WorkspaceFailure) as strict:
        retriever.search(env.workspace_id, "파싱 정책", mode=SearchMode.HYBRID, allow_degraded=False)
    assert strict.value.error.code is WorkspaceErrorCode.EMBEDDING_UNAVAILABLE


def test_vector_index_is_separated_by_model_and_dimension(env):
    other_settings = env.settings
    small = LocalHashEmbeddingProvider(other_settings)
    small.dimension = 32
    small.index_key = f"{small.model_id}:32"
    retriever = WorkspaceRetriever(env.store, other_settings, small)
    response = retriever.search(env.workspace_id, "파싱 정책", mode=SearchMode.VECTOR)
    assert response.total_matches == 0  # no vectors for the 32-dim index key
    hybrid = retriever.search(env.workspace_id, "파싱 정책", mode=SearchMode.HYBRID)
    assert hybrid.degraded is True and hybrid.degradation_reason == "vector_index_empty"
    assert hybrid.mode_effective is SearchMode.KEYWORD


def test_adjacent_chunks_are_suppressed(env):
    text = "\n\n".join([f"파싱 정책 문단 {index}번 내용입니다." for index in range(6)])
    env.manager.upload_file(env.workspace_id, env.scope, "many.txt", [text.encode()])
    env.indexer.run_pending(limit=10)
    response = env.retriever.search(env.workspace_id, "파싱 정책", mode=SearchMode.HYBRID, top_k=10)
    many = [result for result in response.results if result.file_name == "many.txt"]
    assert many, response.results
    assert len(many) < 6  # neighbouring chunks of the same file are collapsed


def test_top_k_and_threshold_are_enforced(env):
    response = env.retriever.search(env.workspace_id, "파싱 정책 저장 파일", mode=SearchMode.HYBRID, top_k=1)
    assert len(response.results) <= 1
    strict = env.retriever.search(
        env.workspace_id, "파싱 정책", mode=SearchMode.VECTOR, min_score=0.99
    )
    assert strict.total_matches == 0
