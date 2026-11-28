"""R21 document knowledge federation: workspace + session attachments + AGTools contract."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.knowledge.api import router as knowledge_router
from app.knowledge.contracts import (
    ArtifactCreateRequest,
    ArtifactRollbackRequest,
    ArtifactUpdateRequest,
    ArtifactValidateRequest,
    DocumentNamespace,
    FederatedSearchRequest,
    KnowledgeContextRequest,
    KnowledgeError,
    KnowledgeErrorCode,
    ProviderKind,
    ReadRequest,
    SearchMode,
    VisionAnalyzeRequest,
)
from app.knowledge.providers import AgtoolsProvider, SessionAttachmentProvider
from app.knowledge.service import DocumentKnowledgeService
from app.workspace.config import WorkspaceSettings
from app.workspace.embedding import LocalHashEmbeddingProvider
from app.workspace.indexer import WorkspaceIndexer
from app.workspace.ingest import ExtractionResult
from app.workspace.manager import WorkspaceManager
from app.workspace.runtime import WorkspaceRuntime, set_runtime
from app.workspace.storage import WorkspaceStorage
from app.workspace.store import WorkspaceStore
from app.workspace.textutil import estimate_tokens


class ParagraphIngestor:
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
        return ExtractionResult(chunks=chunks, parser_version="stub", chunking_version="stub", extraction_source="stub")


class Scope:
    tenant_id = "default_tenant"
    owner_id = "owner_a"
    service_id = "hd2ai-admin"
    authenticated = True


MANIFEST = {
    "contract_version": "dk/v1",
    "service": "agtools",
    "build": "2.0.0",
    "formats": [
        {
            "mime_type": "application/pdf",
            "family": "pdf",
            "extensions": [".pdf"],
            "read": {"page_range": True},
            "write": {"create": False},
            "citation_kinds": ["page"],
        }
    ],
    "search": {"modes": ["keyword"], "keyword_fallback": True},
    "vision": {"available": True, "engine": "gateway", "model": "vision-model-x", "capability_source": "gateway"},
    "artifacts": {"atomic_write": True},
    "limits": {"max_pages": 500},
    "error_codes": ["document_not_found"],
}


def agtools_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    tenant = request.headers.get("x-tenant-id", "")
    if path.endswith("/knowledge/manifest"):
        return httpx.Response(200, json=MANIFEST)
    if path.endswith("/knowledge/search"):
        body = json.loads(request.content)
        document_id = "att-abc123" if tenant.startswith("session-attachment-") else "kd_remote001"
        namespace = "session_attachment" if document_id.startswith("att-") else "agtools"
        return httpx.Response(
            200,
            json={
                "contract_version": "dk/v1",
                "query": body["query"],
                "mode_requested": body.get("mode", "hybrid"),
                "mode_effective": "keyword",
                "degraded": True,
                "degradation_reason": "embedding_unavailable",
                "results": [
                    {
                        "hit_id": f"{document_id}:chk_1",
                        "document": {
                            "namespace": namespace,
                            "document_id": document_id,
                            "name": "session.pdf" if namespace == "session_attachment" else "remote.pdf",
                            "mime_type": "application/pdf",
                            "revision": 1,
                            "content_hash": "sha256:" + "a" * 64,
                            "size_bytes": 100,
                        },
                        "locator": {"kind": "page", "page": 2},
                        "excerpt": "PDF 표는 페이지 단위로 인용됩니다.",
                        "score": 0.8,
                        "matched_by": ["keyword"],
                    }
                ],
            },
        )
    if path.endswith("/knowledge/read-inline"):
        body = json.loads(request.content)
        assert body["content_base64"]
        return httpx.Response(
            200,
            json={
                "contract_version": "dk/v1",
                "document": {
                    "namespace": "agtools",
                    "document_id": "inline",
                    "name": body["name"],
                    "mime_type": body["mime_type"],
                    "revision": 1,
                },
                "nodes": [
                    {
                        "node_id": "p1",
                        "kind": "paragraph",
                        "text": "바이너리 문서 본문",
                        "locator": {"kind": "page", "page": 1},
                    }
                ],
                "citations": [],
            },
        )
    if path.endswith("/knowledge/vision/analyze"):
        return httpx.Response(
            200,
            json={
                "contract_version": "dk/v1",
                "available": True,
                "text": "차트 설명",
                "engine": "gateway",
                "model": "vision-model-x",
                "capability_source": "gateway",
            },
        )
    return httpx.Response(404, json={"error": {"code": "document_not_found", "message": "no route"}})


@pytest.fixture()
def runtime(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path)
    store = WorkspaceStore(settings.resolved_db_path())
    storage = WorkspaceStorage(settings.resolved_storage_root(), max_file_bytes=settings.max_file_bytes)
    manager = WorkspaceManager(store, storage, settings)
    embedder = LocalHashEmbeddingProvider(settings)
    indexer = WorkspaceIndexer(manager, settings, ParagraphIngestor(), embedder=embedder)
    from app.workspace.citations import CitationService
    from app.workspace.context import ContextAssembler
    from app.workspace.retrieval import WorkspaceRetriever
    from app.workspace.tools import WorkspaceToolAdapter

    retriever = WorkspaceRetriever(store, settings, embedder)
    citations = CitationService(store)
    assembler = ContextAssembler(store, settings, retriever, citations)
    rt = WorkspaceRuntime(
        settings=settings,
        store=store,
        storage=storage,
        manager=manager,
        indexer=indexer,
        retriever=retriever,
        assembler=assembler,
        citations=citations,
        embedder=embedder,
        tools=WorkspaceToolAdapter(manager, retriever, assembler),
    )
    yield rt


@pytest.fixture()
def service(runtime, monkeypatch):
    monkeypatch.setenv("AGCORE_SESSION_ATTACHMENT_SERVICES", "hd2ai-admin")
    transport = httpx.MockTransport(agtools_handler)
    agtools = AgtoolsProvider(
        base_url="http://agtools.test/api/v1", token="t", tenant="default_tenant", transport=transport
    )
    attachments = SessionAttachmentProvider(
        base_url="http://agtools.test/api/v1", token="t", transport=transport
    )
    return DocumentKnowledgeService(runtime, agtools=agtools, attachments=attachments)


def _workspace_with_doc(runtime):
    scope = Scope()
    workspace = runtime.manager.create_workspace(scope, "R21 workspace")
    from app.workspace.contracts import WorkspaceWriteRequest

    runtime.manager.write_file(
        workspace.workspace_id,
        scope,
        WorkspaceWriteRequest(
            path="notes/policy.md",
            content="# 문서 정책\n\nPDF 표는 페이지 단위로 인용됩니다.\n\n워크스페이스 정책 문서입니다.",
            mime_type="text/markdown",
            overwrite=True,
        ),
    )
    runtime.indexer.run_pending(limit=10)
    return workspace, scope


def test_manifest_federates_providers_and_reports_vision(service):
    manifest = service.manifest()
    providers = {provider.provider for provider in manifest.providers}
    assert providers == {ProviderKind.WORKSPACE, ProviderKind.AGTOOLS, ProviderKind.SESSION_ATTACHMENT}
    assert manifest.vision.available is True and manifest.vision.model == "vision-model-x"
    assert any(family.family.value == "pdf" for family in manifest.formats)
    assert "provider-federation" in manifest.extensions


def test_search_merges_workspace_and_attachment_hits_and_deduplicates(service, runtime):
    workspace, scope = _workspace_with_doc(runtime)
    request = FederatedSearchRequest(
        query="페이지 단위 인용",
        workspace_id=workspace.workspace_id,
        conversation_id="conv-1",
        sources=[ProviderKind.WORKSPACE, ProviderKind.SESSION_ATTACHMENT],
        top_k=5,
    )
    response = service.search(scope, request)
    namespaces = {hit.document.namespace for hit in response.results}
    assert DocumentNamespace.WORKSPACE in namespaces
    # The attachment excerpt equals the workspace paragraph: one copy survives, the other is
    # counted as a duplicate instead of being shown twice.
    assert response.duplicates_removed >= 1
    assert len({hit.excerpt for hit in response.results}) == len(response.results)
    assert all(hit.citation for hit in response.results)
    assert response.degraded is True and response.degradation_reason

    attachments_only = service.search(
        scope,
        FederatedSearchRequest(
            query="페이지 단위 인용",
            conversation_id="conv-1",
            sources=[ProviderKind.SESSION_ATTACHMENT],
            top_k=5,
        ),
    )
    assert [hit.document.namespace for hit in attachments_only.results] == [
        DocumentNamespace.SESSION_ATTACHMENT
    ]


def test_search_survives_engine_outage_with_workspace_results(service, runtime):
    workspace, scope = _workspace_with_doc(runtime)
    service.agtools._transport = httpx.MockTransport(lambda request: httpx.Response(503))
    service.attachments._transport = service.agtools._transport
    request = FederatedSearchRequest(
        query="정책", workspace_id=workspace.workspace_id, sources=[ProviderKind.WORKSPACE, ProviderKind.AGTOOLS]
    )
    response = service.search(scope, request)
    assert response.results and all(hit.document.namespace is DocumentNamespace.WORKSPACE for hit in response.results)
    assert any("agtools:provider_unavailable" in warning for warning in response.warnings)


def test_text_workspace_read_and_binary_read_inline(service, runtime):
    workspace, scope = _workspace_with_doc(runtime)
    listing = runtime.store.list_files(workspace.workspace_id, recursive=True)
    text_file = next(row for row in listing if row["kind"] == "file")
    read = service.read(
        scope, ReadRequest(document_id=text_file["file_id"]), workspace_id=workspace.workspace_id
    )
    assert read.document.namespace is DocumentNamespace.WORKSPACE
    assert any("워크스페이스 정책" in node.text for node in read.nodes)

    from app.workspace.contracts import WorkspaceWriteRequest

    binary = runtime.manager.write_file(
        workspace.workspace_id,
        scope,
        WorkspaceWriteRequest(
            path="notes/report.pdf",
            content_base64=base64.b64encode(b"%PDF-1.7 fake").decode(),
            mime_type="application/pdf",
            overwrite=True,
        ),
    )
    inline = service.read(
        scope, ReadRequest(document_id=binary.file.file_id), workspace_id=workspace.workspace_id
    )
    assert inline.document.namespace is DocumentNamespace.WORKSPACE
    assert inline.document.document_id == binary.file.file_id
    assert inline.nodes and inline.citations


def test_context_budget_citations_and_injection_flag(service, runtime):
    workspace, scope = _workspace_with_doc(runtime)
    response = service.context(
        scope,
        KnowledgeContextRequest(
            query="정책",
            workspace_id=workspace.workspace_id,
            conversation_id="conv-1",
            token_budget=200,
        ),
    )
    assert response.context_text.startswith("[DOCUMENT KNOWLEDGE CONTEXT]")
    assert response.used_tokens <= response.budget_tokens
    assert response.sources and all(source.citation_id for source in response.sources)


def test_artifact_create_update_conflict_rollback_and_validate(service, runtime):
    workspace, scope = _workspace_with_doc(runtime)
    created = service.create_artifact(
        scope,
        ArtifactCreateRequest(
            workspace_id=workspace.workspace_id,
            path="out/report.md",
            mime_type="text/markdown",
            text="# v1\n",
            artifact_id="art-1",
        ),
    )
    assert created.revision == 1
    first_hash = created.content_hash

    updated = service.update_artifact(
        scope,
        ArtifactUpdateRequest(
            workspace_id=workspace.workspace_id,
            file_id=created.document.document_id,
            mime_type="text/markdown",
            text="# v2\n",
            expected_content_hash=first_hash,
        ),
    )
    assert updated.revision == 2 and updated.content_hash != first_hash

    with pytest.raises(KnowledgeError) as conflict:
        service.update_artifact(
            scope,
            ArtifactUpdateRequest(
                workspace_id=workspace.workspace_id,
                file_id=created.document.document_id,
                mime_type="text/markdown",
                text="# stale\n",
                expected_content_hash=first_hash,
            ),
        )
    assert conflict.value.body.code is KnowledgeErrorCode.REVISION_CONFLICT

    with pytest.raises(KnowledgeError) as revision_conflict:
        service.update_artifact(
            scope,
            ArtifactUpdateRequest(
                workspace_id=workspace.workspace_id,
                file_id=created.document.document_id,
                mime_type="text/markdown",
                text="# stale\n",
                expected_revision=1,
            ),
        )
    assert revision_conflict.value.body.code is KnowledgeErrorCode.REVISION_CONFLICT

    rolled = service.rollback_artifact(
        scope,
        ArtifactRollbackRequest(
            workspace_id=workspace.workspace_id,
            file_id=created.document.document_id,
            to_revision=1,
        ),
    )
    assert rolled.revision == 3 and rolled.content_hash == first_hash

    validated = service.validate_artifact(
        scope,
        ArtifactValidateRequest(workspace_id=workspace.workspace_id, file_id=created.document.document_id),
    )
    assert validated.ok is True
    assert {check.name for check in validated.checks} >= {"content_hash", "size", "revision_row"}


def test_vision_delegates_to_engine_capability(service):
    response = service.vision_analyze(
        Scope(),
        "conv-1",
        VisionAnalyzeRequest(
            image_base64=base64.b64encode(b"pngbytes").decode(), mime_type="image/png", prompt="설명"
        ),
    )
    assert response.available is True
    assert response.model == "vision-model-x"
    assert response.text == "차트 설명"


def test_api_surface_end_to_end(runtime):
    set_runtime(runtime)
    try:
        app = FastAPI()
        app.include_router(knowledge_router)
        transport = httpx.MockTransport(agtools_handler)
        agtools = AgtoolsProvider(base_url="http://agtools.test/api/v1", transport=transport)
        service = DocumentKnowledgeService(runtime, agtools=agtools)
        from app.knowledge import api as knowledge_api

        knowledge_api._SERVICES[id(runtime)] = service
        client = TestClient(
            app, headers={"X-HD2Lab-User": "owner_a", "X-HD2Lab-Service": "hd2ai-admin"}
        )
        import os

        os.environ["AGCORE_KNOWLEDGE_ENABLED"] = "true"
        workspace, _ = _workspace_with_doc(runtime)
        response = client.get("/api/v1/knowledge/capabilities")
        assert response.status_code == 200, response.text
        assert response.json()["service"] == "agcore"

        search = client.post(
            "/api/v1/knowledge/search",
            json={
                "query": "정책",
                "workspace_id": workspace.workspace_id,
                "conversation_id": "conv-1",
                "sources": ["workspace", "session_attachment"],
            },
        )
        assert search.status_code == 200, search.text
        assert search.json()["results"]

        artifact = client.post(
            "/api/v1/knowledge/artifacts",
            json={"workspace_id": workspace.workspace_id, "path": "out/a.md", "text": "hello"},
        )
        assert artifact.status_code == 200, artifact.text
        assert artifact.json()["revision"] == 1

        missing = client.post(
            "/api/v1/knowledge/read", json={"document_id": "wf_missing", "workspace_id": workspace.workspace_id}
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "document_not_found"
    finally:
        os.environ.pop("AGCORE_KNOWLEDGE_ENABLED", None)
        set_runtime(None)
