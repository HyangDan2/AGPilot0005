"""WO-R21-ADD-01 — unified document reference and evidence coverage.

Two levels are covered here:

* the pure contract (:mod:`app.knowledge.references`): requested vs available references,
  deterministic citation refs, evidence coverage, bounded enrichment planning;
* the runtime path (REST + SSE) with the two synthetic PDFs from R2-BETA-HOTFIX-08A —
  Workspace ``R2-BETA-WORKSPACE-731`` and Session Attachment ``R2-BETA-ATTACHMENT-428`` —
  cited in one answer, a ``partial`` answer when one of them cannot be read, and an
  unchanged simple-chat path with zero document searches or extra model calls.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.knowledge.contracts import CONTRACT_EXTENSIONS
from app.knowledge.references import (
    AnalysisTarget,
    CoverageStatus,
    DocumentReference,
    EvidenceTracker,
    ReferenceScope,
    ReferenceSourceType,
    SynthesisBudget,
    attachment_reference,
    make_citation_ref,
    plan_enrichment,
    resolve_references,
    workspace_reference,
)
from app.knowledge import (
    EVIDENCE_COVERAGE_EXTENSION,
    UNIFIED_REFERENCE_EXTENSION,
    DocumentReference as ExportedDocumentReference,
    EvidenceCoverage,
    ReferenceResolution,
    resolve_references as exported_resolve,
)
from app.schemas.gateway import FunctionCall, ToolCall
from app.workspace.contracts import WorkspaceWriteRequest
from app.workspace.ingest import ExtractionResult
from app.workspace.textutil import estimate_tokens
from app.main import app as fastapi_app

from tests.test_r2_integration_cp05 import (
    ScriptedGateway,
    _parse_sse,
    _proxy_headers,
    _scope,
    _setup_r2_env,
)

WORKSPACE_MARKER = "R2-BETA-WORKSPACE-731"
ATTACHMENT_MARKER = "R2-BETA-ATTACHMENT-428"


@pytest.fixture(autouse=True)
def _restore_process_state():
    """The tool registry and workspace runtime are process singletons — put them back."""

    from app.canvas.service import reset_canvas_service
    from app.tools.registry import registry
    from app.workspace.runtime import reset_runtime

    tools = dict(registry._tools)
    handlers = dict(registry._handlers)
    yield
    registry._tools = tools
    registry._handlers = handlers
    reset_runtime()
    reset_canvas_service()


# ---------------------------------------------------------------------------------------
# Fixtures: real synthetic PDFs (PyMuPDF), one per side
# ---------------------------------------------------------------------------------------


def _make_pdf(path: Path, lines: list[str]) -> bytes:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    text = "\n".join(lines)
    page.insert_textbox(pymupdf.Rect(36, 36, 560, 800), text, fontsize=11)
    document.save(str(path))
    document.close()
    return path.read_bytes()


class PdfPageIngestor:
    """Test ingestor: page-accurate PDF text chunks, the shape AGTools would return."""

    def for_scope(self, tenant_id, owner_id):  # noqa: D401 - protocol shape
        return self

    def supports(self, name, mime_type):
        ext = (name or "").lower().rsplit(".", 1)[-1]
        return ext == "pdf" or (mime_type or "") == "application/pdf"

    def extract(self, name, mime_type, data):
        from app.workspace.chunking import SourceChunk

        import pymupdf

        chunks = []
        document = pymupdf.open(stream=data, filetype="pdf")
        try:
            for index, page in enumerate(document, start=1):
                body = (page.get_text() or "").strip()
                if not body:
                    continue
                chunks.append(
                    SourceChunk(
                        content=body,
                        token_count=estimate_tokens(body),
                        page_number=index,
                        section=f"p{index}",
                        offset_start=0,
                        offset_end=len(body),
                    )
                )
        finally:
            document.close()
        return ExtractionResult(
            chunks=chunks, parser_version="pymupdf-test", chunking_version="page", extraction_source="pymupdf"
        )


def _agtools_transport(search_total: int):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.headers.get("X-Access-Scope") != "private":
            return httpx.Response(403, json={"detail": "admin session attachment"})
        if request.method == "POST" and path.endswith("/documents"):
            return httpx.Response(202, json={"document_id": "doc-1", "status": "queued"})
        if path.endswith("/outline"):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "filename": f"{ATTACHMENT_MARKER}.pdf",
                    "document_name": f"{ATTACHMENT_MARKER}.pdf",
                    "sections": [{"section_id": "sec-1", "title": "attachment", "page": 1}],
                },
            )
        if path.endswith("/search"):
            results = (
                [
                    {
                        "chunk_id": "chk_att_1",
                        "section_id": "sec-1",
                        "snippet": f"ATTACHMENT evidence marker {ATTACHMENT_MARKER}",
                        "score": 0.9,
                        "source": {"page": 2},
                    }
                ]
                if search_total
                else []
            )
            return httpx.Response(200, json={"total_matches": len(results), "results": results})
        if path.endswith("/text"):
            return httpx.Response(200, json={"text": f"attachment only {ATTACHMENT_MARKER}"})
        return httpx.Response(200, json={"status": "completed", "page_count": 1})

    return httpx.MockTransport(handler)


def _patch_agtools(monkeypatch, search_total: int = 1):
    from types import SimpleNamespace

    import httpx

    from app.adapters import document

    transport = _agtools_transport(search_total)
    monkeypatch.setattr(
        document,
        "httpx",
        SimpleNamespace(AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs)),
    )
    monkeypatch.setattr(document.document_adapter, "http", document.AGToolsHttpAdapter())


def _allow_document_search(monkeypatch):
    from app.tools.policy import tool_policy

    original_allow = tool_policy.is_tool_allowed
    monkeypatch.setattr(
        tool_policy,
        "is_tool_allowed",
        lambda name, service: (True, None) if name == "document.search" else original_allow(name, service),
    )


async def _setup_mixed_env(monkeypatch, tmp_path, gateway, *, search_total: int = 1):
    workspace, _ = _setup_r2_env(monkeypatch, tmp_path, gateway)
    monkeypatch.setenv("AGCORE_SESSION_ATTACHMENT_SERVICES", "hd2ai-beta")
    monkeypatch.setattr(settings, "AGTOOLS_TOKEN", "sandbox-parser-token")
    monkeypatch.setattr(settings, "ALLOW_MOCK_FALLBACK", False)
    _patch_agtools(monkeypatch, search_total=search_total)
    _allow_document_search(monkeypatch)

    workspace.indexer.ingestor = PdfPageIngestor()
    scope = _scope(service="hd2ai-beta")
    folder = workspace.manager.new_workspace(scope, "unified-reference")
    pdf_bytes = _make_pdf(
        tmp_path / f"{WORKSPACE_MARKER}.pdf",
        [
            f"Workspace evidence document. Identifier {WORKSPACE_MARKER}.",
            "This page belongs to the workspace PDF only.",
        ],
    )
    created = workspace.manager.write_file(
        folder.workspace_id,
        scope,
        WorkspaceWriteRequest(
            path=f"{WORKSPACE_MARKER}.pdf",
            content_base64=base64.b64encode(pdf_bytes).decode(),
            mime_type="application/pdf",
            create_parents=True,
        ),
    )
    workspace.indexer.run_pending(limit=10)
    return workspace, folder, created.file.file_id


async def _bind_conversation(client, headers, conversation: str, workspace_id):
    response = await client.put(
        f"/api/v1/conversations/{conversation}/workspace", headers=headers, json={"workspace_id": workspace_id}
    )
    assert response.status_code == 200, response.text


async def _upload_attachment(client, headers, conversation: str) -> str:
    files = {"file": (f"{ATTACHMENT_MARKER}.pdf", b"%PDF-1.7 synthetic", "application/pdf")}
    response = await client.post(f"/api/v1/conversations/{conversation}/attachments", headers=headers, files=files)
    assert response.status_code == 202, response.text
    return response.json()["document_id"]


# ---------------------------------------------------------------------------------------
# Contract level
# ---------------------------------------------------------------------------------------


def test_contract_is_exported_for_runtime_and_r3_reuse():
    assert UNIFIED_REFERENCE_EXTENSION == "unified-document-reference"
    assert EVIDENCE_COVERAGE_EXTENSION == "evidence-coverage"
    for extension in ("unified-document-reference", "evidence-coverage", "bounded-enrichment", "synthesis-budget-reservation"):
        assert extension in CONTRACT_EXTENSIONS
    assert ExportedDocumentReference is DocumentReference
    assert resolve_references is exported_resolve
    assert {"DocumentReference", "ReferenceResolution", "EvidenceCoverage"} <= set(
        __import__("app.knowledge", fromlist=["__all__"]).__all__
    )
    reference = workspace_reference(file_id="wf_1", name="a.pdf", revision=3)
    assert reference.citation_ref == make_citation_ref(ReferenceSourceType.WORKSPACE_FILE, "wf_1", 3)
    assert DocumentReference(
        source_type=ReferenceSourceType.SESSION_ATTACHMENT,
        document_id="att-1",
        name="x.pdf",
    ).citation_ref == "session_attachment:att-1@1"


def test_resolution_separates_available_from_requested():
    scope = ReferenceScope(workspace_id="ws_1", conversation_id="conv-1")
    named = workspace_reference(file_id="wf_731", name=f"{WORKSPACE_MARKER}.pdf", path="docs/b.pdf", scope=scope)
    unnamed = workspace_reference(file_id="wf_9", name="unrelated.txt", path="docs/u.txt", scope=scope)
    attachment = attachment_reference(document_id="att-428", name=f"{ATTACHMENT_MARKER}.pdf", scope=scope)

    resolution = resolve_references(
        message=f"{WORKSPACE_MARKER}.pdf 내용을 알려줘",
        explicit_ids=["att-428"],
        available=[named, unnamed, attachment],
        scope=scope,
    )
    assert isinstance(resolution, ReferenceResolution)
    assert resolution.route == "document"
    assert [ref.document_id for ref in resolution.requested_refs] == ["att-428", "wf_731"]
    assert {ref.document_key for ref in resolution.available_refs} == {
        "workspace_file:wf_731",
        "workspace_file:wf_9",
        "session_attachment:att-428",
    }
    assert resolution.requested_by_key("workspace_file:wf_731").origin.value == "natural_language"
    assert resolution.requested_target is AnalysisTarget.SECTION or resolution.requested_target is AnalysisTarget.FULL

    chat = resolve_references(message="오늘 날씨 어때?", available=[named, unnamed], scope=scope)
    assert chat.route == "chat" and chat.requested_refs == []


def test_unresolved_references_carry_reasons():
    resolution = resolve_references(
        message="이 파일 봐줘",
        explicit_ids=["att-foreign", "wf_missing"],
        available=[],
        missing_explicit=[("wf_missing", "workspace_file_not_found")],
    )
    reasons = {(item.mention, item.reason) for item in resolution.unresolved}
    assert ("att-foreign", "attachment_not_bound_to_conversation") in reasons
    assert ("wf_missing", "workspace_file_not_found") in reasons
    assert all(ref.availability.value == "missing" for ref in resolution.requested_refs)


def test_evidence_tracker_only_credits_real_content():
    attachment = attachment_reference(document_id="att-428", name="a.pdf")
    tracker = EvidenceTracker([attachment])
    assert tracker.coverage().status == "missing"

    # An outline is structure, not evidence: it must not complete the reference.
    tracker.record_tool_result(
        "document.get_outline", {"doc_id": "att-428"}, json.dumps({"doc_id": "att-428", "outline": [{"section_id": "s1"}]})
    )
    assert tracker.coverage().entries[0].status is CoverageStatus.MISSING

    # A search with no matches is a recorded miss with a reason, not silent success.
    tracker.record_tool_result(
        "document.search", {"doc_id": "att-428"}, json.dumps({"doc_id": "att-428", "total_matches": 0, "results": []})
    )
    assert tracker.coverage().entries[0].missing_reason == "no_matching_evidence"

    tracker.record_tool_result(
        "document.read_section",
        {"doc_id": "att-428", "section_id": "s1"},
        json.dumps({"doc_id": "att-428", "section_id": "s1", "content": "ATTACHMENT-428 body", "source": {"page": 2}}),
    )
    coverage = tracker.coverage()
    assert coverage.status == "covered" and coverage.entries[0].pages == [2]
    assert coverage.entries[0].citation_refs

    # A later failure never removes evidence that was already secured.
    tracker.record_tool_result("document.search", {"doc_id": "att-428"}, json.dumps({"error": True, "code": "http_503"}), is_error=True)
    assert tracker.coverage().status == "covered"


def test_workspace_evidence_needs_the_named_file_not_any_hit():
    from app.knowledge.references import workspace_reference as ws_ref

    target = ws_ref(file_id="wf_731", name=f"{WORKSPACE_MARKER}.pdf", path=f"docs/{WORKSPACE_MARKER}.pdf")
    other = ws_ref(file_id="wf_other", name="other.md", path="docs/other.md")
    tracker = EvidenceTracker([target])
    tracker.record_tool_result(
        "workspace_retrieve",
        {"query": "q"},
        json.dumps(
            {
                "status": "ok",
                "sources": [{"file_id": "wf_other", "chunk_id": "chk_2", "page_number": 1, "citation_id": "cit_2"}],
            }
        ),
    )
    assert tracker.coverage().entries[0].status is CoverageStatus.MISSING

    tracker.record_tool_result(
        "workspace_retrieve",
        {"query": "q", "file_ids": ["wf_731"]},
        json.dumps(
            {
                "status": "ok",
                "sources": [
                    {
                        "file_id": "wf_731",
                        "file_name": f"{WORKSPACE_MARKER}.pdf",
                        "chunk_id": "chk_9",
                        "page_number": 1,
                        "citation_id": "cit_9",
                    }
                ],
            }
        ),
    )
    assert tracker.coverage().status == "covered"


def test_enrichment_is_bounded_and_targets_missing_references_only():
    covered = attachment_reference(document_id="att-1", name="a.pdf")
    missing_ws = workspace_reference(file_id="wf_2", name="b.pdf", scope=ReferenceScope(workspace_id="ws_1"))
    tracker = EvidenceTracker([covered, missing_ws], enrichment_limit=2)
    tracker.record_tool_result(
        "document.read_section", {"doc_id": "att-1"}, json.dumps({"doc_id": "att-1", "chunk_id": "c1", "content": "x"})
    )
    candidates = tracker.enrichment_candidates(attempts_left=2)
    assert [ref.document_id for ref in candidates] == ["wf_2"]

    calls = plan_enrichment(candidates, query="q")
    assert [(call.tool_name, call.arguments.get("workspace_id")) for call in calls] == [("workspace_retrieve", "ws_1")]
    tracker.note_enrichment_attempt([call.reference_key for call in calls])
    assert tracker.enrichment_candidates(attempts_left=1) == []
    assert tracker.coverage().enrichment.attempts == 1
    assert tracker.coverage().budget.reserved_tokens == 0


# ---------------------------------------------------------------------------------------
# Runtime: mixed Workspace PDF + Session Attachment PDF in one answer
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_and_attachment_pdfs_are_cited_in_one_answer(monkeypatch, tmp_path):
    gateway = ScriptedGateway([])
    workspace, folder, workspace_file_id = await _setup_mixed_env(monkeypatch, tmp_path, gateway)
    headers = _proxy_headers(service="hd2ai-beta")

    gateway.script = [
        {"tool_calls": [ToolCall(id="outline-1", function=FunctionCall(name="document.get_outline", arguments="{}"))]},
        {"tool_calls": [ToolCall(id="search-1", function=FunctionCall(
            name="document.search", arguments='{"doc_id":"att-doc-1","query":"attachment evidence"}'))]},
        # Draft answer that only used the attachment: the runtime must not finish here.
        {"content": f"첨부 문서 기준입니다. {ATTACHMENT_MARKER}"},
        {"content": f"두 자료를 비교했습니다. Workspace {WORKSPACE_MARKER} · Attachment {ATTACHMENT_MARKER}"},
    ]

    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as client:
        await _bind_conversation(client, headers, "mixed", folder.workspace_id)
        attachment = await _upload_attachment(client, headers, "mixed")

        # Permission isolation (R2-BETA-HOTFIX-08A evidence kept):
        assert (
            await client.get(
                f"/api/v1/conversations/other/attachments/{attachment}/status", headers=headers
            )
        ).status_code == 404
        foreign = await client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={"message": "check", "conversation_id": "other", "context_refs": [attachment]},
        )
        assert foreign.status_code == 422
        admin_headers = _proxy_headers(service="hd2ai-admin")
        admin_upload = await client.post(
            "/api/v1/conversations/mixed/attachments",
            headers=admin_headers,
            files={"file": ("x.pdf", b"%PDF", "application/pdf")},
        )
        assert admin_upload.status_code == 403

        response = await client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={
                "message": f"{WORKSPACE_MARKER}.pdf 와 첨부 PDF를 비교해줘",
                "conversation_id": "mixed",
                "context_refs": [attachment],
                "stream": True,
            },
        )
        assert response.status_code == 200, response.text
        events = _parse_sse(response.text)
        completed = [event for event in events if event["event"] == "run.completed"]
        assert completed, response.text
        payload = completed[-1]["data"]
        assert payload["status"] == "completed", payload
        assert payload["terminal_status"] == "completed"
        assert payload["partial"] is False
        assert WORKSPACE_MARKER in payload["answer"] and ATTACHMENT_MARKER in payload["answer"]
        coverage = payload["evidence_coverage"]
        assert coverage["status"] == "covered" and coverage["covered"] == 2
        assert coverage["budget"]["reserved_tokens"] > 0
        assert payload["missing_references"] == []
        assert {ref.split(":")[0] for ref in payload["references"]["requested_refs"]} == {
            "workspace_file",
            "session_attachment",
        }
        assert any(event["event"] == "evidence.coverage" and event["data"]["covered"] >= 1 for event in events)

        run_id = events[0]["data"]["run_id"]
        status = await client.get(f"/api/v1/agent/runs/{run_id}", headers=headers, params={"conversation_id": "mixed"})
        assert status.status_code == 200
        body = status.json()
        assert body["status"] == "completed"
        assert body["evidence_coverage"]["status"] == "covered"
        assert body["missing_references"] == []
        tool_names = [trace["tool_name"] for trace in body["traces"]]
        assert "workspace_retrieve" in tool_names  # bounded enrichment actually read the workspace PDF
        assert "document.search" in tool_names
        # The reference scope of the enrichment call never leaves the conversation workspace.
        enrichment = next(trace for trace in body["traces"] if trace["tool_name"] == "workspace_retrieve")
        assert enrichment["arguments"]["file_ids"] == [workspace_file_id]
        assert enrichment["arguments"]["workspace_id"] == folder.workspace_id


@pytest.mark.asyncio
async def test_unreadable_reference_ends_partial_with_reasons(monkeypatch, tmp_path):
    gateway = ScriptedGateway([])
    workspace, folder, _ = await _setup_mixed_env(monkeypatch, tmp_path, gateway, search_total=0)
    headers = _proxy_headers(service="hd2ai-beta")
    gateway.script = [
        {"tool_calls": [ToolCall(id="outline-1", function=FunctionCall(name="document.get_outline", arguments="{}"))]},
        {"content": f"첨부는 확인했지만 근거가 없습니다. {ATTACHMENT_MARKER}"},
        {"content": f"Workspace {WORKSPACE_MARKER} 만 인용합니다."},
    ]

    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as client:
        await _bind_conversation(client, headers, "partial", folder.workspace_id)
        attachment = await _upload_attachment(client, headers, "partial")
        response = await client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={
                "message": f"{WORKSPACE_MARKER}.pdf 와 첨부 PDF를 비교해줘",
                "conversation_id": "partial",
                "context_refs": [attachment],
                "stream": True,
            },
        )
        assert response.status_code == 200, response.text
        events = _parse_sse(response.text)
        completed = [event for event in events if event["event"] == "run.completed"][-1]["data"]
        assert completed["status"] == "partial"
        assert completed["terminal_status"] == "partial"
        assert completed["partial"] is True
        assert completed["missing_references"] == ["session_attachment:att-doc-1@1"]
        assert completed["partial_reasons"]["session_attachment:att-doc-1@1"] in ("no_matching_evidence", "empty_result")
        assert completed["evidence_coverage"]["covered"] == 1
        assert "answer" in completed and completed["answer"]

        run_id = events[0]["data"]["run_id"]
        body = (
            await client.get(f"/api/v1/agent/runs/{run_id}", headers=headers, params={"conversation_id": "partial"})
        ).json()
        assert body["status"] == "partial"
        assert body["partial_reasons"]
        assert body["missing_references"] == ["session_attachment:att-doc-1@1"]
        # Enrichment stayed bounded: at most EVIDENCE_ENRICHMENT_LIMIT extra reads.
        reads = [trace for trace in body["traces"] if trace["tool_name"] in ("document.search", "workspace_retrieve")]
        assert len(reads) <= 1 + int(settings.EVIDENCE_ENRICHMENT_LIMIT)


@pytest.mark.asyncio
async def test_explicit_workspace_file_selection_is_honoured(monkeypatch, tmp_path):
    gateway = ScriptedGateway([])
    workspace, folder, workspace_file_id = await _setup_mixed_env(monkeypatch, tmp_path, gateway)
    headers = _proxy_headers(service="hd2ai-beta")
    gateway.script = [
        # No file name in the message and no tool call: only the explicit id can trigger this read.
        {"content": "파일을 확인하지 못했습니다."},
        {"content": f"Workspace {WORKSPACE_MARKER} 확인 완료"},
    ]

    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as client:
        await _bind_conversation(client, headers, "explicit", folder.workspace_id)
        response = await client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={
                "message": "이 파일 좀 봐줘",
                "conversation_id": "explicit",
                "context_refs": [workspace_file_id],
                "stream": True,
            },
        )
        assert response.status_code == 200, response.text
        events = _parse_sse(response.text)
        payload = [event for event in events if event["event"] == "run.completed"][-1]["data"]
        assert payload["status"] == "completed", payload
        assert payload["references"]["requested_refs"] == [f"workspace_file:{workspace_file_id}@1"]
        assert payload["evidence_coverage"]["covered"] == 1
        run_id = events[0]["data"]["run_id"]
        body = (await client.get(f"/api/v1/agent/runs/{run_id}", headers=headers, params={"conversation_id": "explicit"})).json()
        assert body["traces"][0]["tool_name"] == "workspace_retrieve"

        # A workspace file reference without a bound workspace is a client error, not a silent search.
        unbound = await client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={"message": "봐줘", "conversation_id": "unbound", "context_refs": [workspace_file_id]},
        )
        assert unbound.status_code == 422
        # A file id that is not in this conversation's workspace is refused the same way.
        foreign_file = await client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={"message": "봐줘", "conversation_id": "explicit", "context_refs": ["wf_not_in_workspace"]},
        )
        assert foreign_file.status_code == 422


@pytest.mark.asyncio
async def test_simple_chat_does_not_search_or_extra_call(monkeypatch, tmp_path):
    gateway = ScriptedGateway([])
    workspace, folder, _ = await _setup_mixed_env(monkeypatch, tmp_path, gateway)
    headers = _proxy_headers(service="hd2ai-beta")
    gateway.script = [{"content": "안녕하세요. 무엇을 도와드릴까요?"}]

    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as client:
        await _bind_conversation(client, headers, "plain", folder.workspace_id)
        response = await client.post(
            "/api/v1/agent/runs",
            headers=headers,
            json={"message": "안녕", "conversation_id": "plain", "stream": True},
        )
        assert response.status_code == 200, response.text
        events = _parse_sse(response.text)
        assert not any(event["event"] == "evidence.coverage" for event in events)
        payload = [event for event in events if event["event"] == "run.completed"][-1]["data"]
        assert payload["status"] == "completed"
        assert "evidence_coverage" not in payload or payload.get("missing_references") == []
        run_id = events[0]["data"]["run_id"]
        body = (await client.get(f"/api/v1/agent/runs/{run_id}", headers=headers, params={"conversation_id": "plain"})).json()
        assert len(gateway.calls) == 1
        assert body["traces"] == []
        assert body["requested_refs"] == []
        assert body["evidence_coverage"] is None
