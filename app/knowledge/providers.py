"""Knowledge providers behind AGCore (WP-01 federated engine).

Three providers implement the same contract:

* ``WorkspaceProvider``       — the existing R2-W workspace engine (retrieval, citations, blobs)
* ``SessionAttachmentProvider`` — AGTools documents scoped to one conversation's attachment tenant
* ``AgtoolsProvider``         — AGTools managed documents and every format engine it owns

The workspace engine stays authoritative for workspace files and index state; AGCore never
copies workspace bytes into AGTools for indexing. Binary formats that the workspace engine does
not parse (PDF/DOCX/XLSX/PPTX) are read through AGTools' ``read-inline`` call, which reads caller-
supplied bytes without storing a copy.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import httpx

from app.config import settings
from app.core.session_attachments import attachment_tenant, is_session_attachment
from app.knowledge.contracts import (
    CONTRACT_VERSION,
    CapabilityManifest,
    Citation,
    DocumentNamespace,
    DocumentRef,
    FormatCapability,
    FormatFamily,
    IndexState,
    KnowledgeError,
    KnowledgeErrorBody,
    KnowledgeErrorCode,
    Locator,
    LocatorKind,
    ProviderCapability,
    ProviderKind,
    ProviderStatus,
    ReadCapability,
    ReadRequest,
    ReadResponse,
    SearchFilters,
    SearchHit,
    SearchMode,
    SearchRequest,
    SearchResponse,
    VisionCapability,
    WriteCapability,
    format_family,
    make_citation,
    normalize_mime,
)
from app.workspace.contracts import SearchMode as WorkspaceSearchMode


class ScopeLike:
    tenant_id: str
    owner_id: str
    service_id: str


@dataclass
class ProviderSearchResult:
    hits: List[SearchHit] = field(default_factory=list)
    status: ProviderStatus = field(default_factory=ProviderStatus)
    mode_requested: SearchMode = SearchMode.HYBRID
    mode_effective: SearchMode = SearchMode.KEYWORD
    degraded: bool = False
    degradation_reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    duplicates_removed: int = 0


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _excerpt(text: str, query: str, limit: int = 320) -> str:
    lowered = text.lower()
    positions = [lowered.find(term.lower()) for term in query.split() if term.strip()]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return text[:limit].strip()
    start = max(0, min(positions) - 80)
    return text[start : start + limit].strip()


class WorkspaceProvider:
    """Adapter over `WorkspaceRuntime`; no new storage and no re-indexing."""

    kind = ProviderKind.WORKSPACE

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    # ------------------------------------------------------------------ search
    def search(
        self,
        scope: ScopeLike,
        workspace_id: str,
        request: SearchRequest,
        *,
        file_ids: Optional[Sequence[str]] = None,
    ) -> ProviderSearchResult:
        self.runtime.manager.check_workspace_access(workspace_id, scope)
        self.runtime.manager.ensure_open(workspace_id, scope)
        filters = request.filters
        mime_types = list(filters.mime_types)
        for family in filters.families:
            mime_types.extend(_mimes_for(family))
        mime_types = _flatten(mime_types)
        mode = WorkspaceSearchMode(request.mode.value)
        response = self.runtime.retriever.search(
            workspace_id,
            request.query,
            mode=mode,
            top_k=request.top_k,
            file_ids=list(file_ids) if file_ids else None,
            mime_types=mime_types or None,
            allow_degraded=request.allow_degraded,
        )
        hits: List[SearchHit] = []
        seen: set[str] = set()
        duplicates = 0
        for result in response.results:
            record = self.runtime.store.get_file(workspace_id, result.file_id)
            if record is None:
                continue
            ref = workspace_document_ref(record)
            if not _passes_filters(ref, filters):
                continue
            excerpt = result.excerpt
            fingerprint = _hash(excerpt)
            if fingerprint in seen:
                duplicates += 1
                continue
            seen.add(fingerprint)
            locator = Locator(
                kind=LocatorKind.PAGE if result.page_number else LocatorKind.DOCUMENT,
                page=result.page_number,
                section=result.section,
            )
            hits.append(
                SearchHit(
                    hit_id=f"ws:{workspace_id}:{result.chunk_id}",
                    document=ref,
                    locator=locator,
                    excerpt=excerpt,
                    score=result.score,
                    keyword_score=result.keyword_score,
                    vector_score=result.vector_score,
                    matched_by=list(result.matched_by) or ["keyword"],
                    chunk_id=result.chunk_id,
                    citation=make_citation(ref, locator, excerpt, content_hash=ref.content_hash),
                )
            )
        status = ProviderStatus(
            keyword=response.provider_status.keyword,
            embedding=response.provider_status.embedding,
        )
        return ProviderSearchResult(
            hits=hits,
            status=status,
            mode_requested=SearchMode(response.mode_requested.value),
            mode_effective=SearchMode(response.mode_effective.value),
            degraded=response.degraded,
            degradation_reason=response.degradation_reason,
            duplicates_removed=duplicates,
        )

    # ------------------------------------------------------------------ read
    def read(
        self,
        scope: ScopeLike,
        workspace_id: str,
        file_id: str,
        request: ReadRequest,
    ) -> ReadResponse:
        content = self.runtime.manager.read_file(workspace_id, file_id, scope, revision=request.revision)
        record = self.runtime.store.get_file(workspace_id, file_id)
        ref = workspace_document_ref(record) if record else DocumentRef(
            namespace=DocumentNamespace.WORKSPACE,
            document_id=file_id,
            revision=content.revision,
            content_hash=content.content_hash,
            mime_type=content.metadata.mime_type,
            name=content.metadata.name,
        )
        text = content.text
        if text is None:
            text = content.bytes.decode("utf-8", errors="replace")
        nodes = []
        from app.knowledge.contracts import DocNode, NodeKind

        for index, block in enumerate(text.split("\n\n"), start=1):
            stripped = block.strip()
            if not stripped:
                continue
            nodes.append(
                DocNode(
                    node_id=f"p{index}",
                    kind=NodeKind.PARAGRAPH,
                    text=stripped,
                    locator=Locator(kind=LocatorKind.PARAGRAPH, paragraph=index),
                )
            )
        citations = [make_citation(ref, Locator(kind=LocatorKind.DOCUMENT), text[:600])] if text else []
        return ReadResponse(
            document=ref,
            mode=request.mode,
            nodes=nodes,
            citations=citations,
            warnings=[] if text else ["text_not_extracted"],
        )

    def read_bytes(self, scope: ScopeLike, workspace_id: str, file_id: str, revision: Optional[int] = None):
        return self.runtime.manager.read_file(workspace_id, file_id, scope, revision=revision)

    # ------------------------------------------------------------------ manifest
    def capability(self) -> ProviderCapability:
        settings_obj = self.runtime.settings
        modes = [SearchMode.KEYWORD]
        if self.runtime.retriever.embedder_available():
            modes.extend([SearchMode.VECTOR, SearchMode.HYBRID])
        formats = [
            FormatCapability(
                mime_type="text/plain",
                family=FormatFamily.TEXT,
                extensions=[".txt", ".md", ".csv", ".json"],
                read=ReadCapability(tables=False, images=False, partial=True),
                write=WriteCapability(create=True, edit=True),
                citation_kinds=[LocatorKind.PARAGRAPH, LocatorKind.SECTION, LocatorKind.DOCUMENT],
            )
        ]
        return ProviderCapability(
            provider=ProviderKind.WORKSPACE,
            available=True,
            contract_version=CONTRACT_VERSION,
            formats=formats,
            search_modes=modes,
            vision=VisionCapability(available=False, reason="workspace_has_no_vision"),
            detail={
                "context_budget_tokens": getattr(settings_obj, "context_budget_tokens", None),
                "embedding": self.runtime.retriever.embedder_available(),
            },
        )


class AgtoolsProvider:
    """HTTP provider for AGTools `dk/v1`. Degrades explicitly, never pretends."""

    kind = ProviderKind.AGTOOLS

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        tenant: Optional[str] = None,
        timeout: Optional[float] = None,
        client: Optional[httpx.Client] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = (base_url or settings.AGTOOLS_URL).rstrip("/")
        self.token = token if token is not None else settings.AGTOOLS_TOKEN
        self.tenant = tenant or settings.AGTOOLS_TENANT
        self.timeout = float(timeout or settings.AGTOOLS_TIMEOUT_SECONDS)
        self._client = client
        self._transport = transport
        self._manifest_cache: Optional[CapabilityManifest] = None
        self._manifest_at: float = 0.0
        self._failure: Optional[str] = None

    # ------------------------------------------------------------------ plumbing
    def _headers(self, tenant: Optional[str] = None) -> Dict[str, str]:
        headers = {"X-Tenant-Id": tenant or self.tenant, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            if self._client is not None:
                response = self._client.request(method, url, json=json, headers=self._headers(tenant))
            else:
                with httpx.Client(
                    timeout=self.timeout, transport=self._transport, trust_env=False
                ) as client:
                    response = client.request(method, url, json=json, headers=self._headers(tenant))
        except httpx.HTTPError as exc:
            self._failure = type(exc).__name__
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE,
                "document engine unreachable",
                detail={"reason": type(exc).__name__},
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            self._failure = f"http_{response.status_code}"
            raise _error_from_response(response)
        self._failure = None
        try:
            payload = response.json()
        except ValueError as exc:
            raise KnowledgeError(
                KnowledgeErrorCode.PROVIDER_UNAVAILABLE, "document engine returned non-JSON"
            ) from exc
        return payload

    def available(self, *, refresh: bool = False, ttl: float = 30.0) -> bool:
        now = time.time()
        if not refresh and self._manifest_cache is not None and now - self._manifest_at < ttl:
            return True
        try:
            self.manifest(refresh=True)
            return True
        except KnowledgeError:
            return False

    def manifest(self, *, refresh: bool = False) -> CapabilityManifest:
        now = time.time()
        if not refresh and self._manifest_cache is not None and now - self._manifest_at < 30:
            return self._manifest_cache
        payload = self._request("GET", "/knowledge/manifest")
        try:
            manifest = CapabilityManifest.model_validate(payload)
        except Exception as exc:
            raise KnowledgeError(
                KnowledgeErrorCode.CONTRACT_MISMATCH,
                "document engine manifest does not match dk/v1",
                detail={"reason": type(exc).__name__},
            ) from exc
        self._manifest_cache = manifest
        self._manifest_at = now
        return manifest

    # ------------------------------------------------------------------ operations
    def search(
        self,
        request: SearchRequest,
        *,
        tenant: Optional[str] = None,
        namespace: DocumentNamespace = DocumentNamespace.AGTOOLS,
    ) -> ProviderSearchResult:
        payload = self._request(
            "POST", "/knowledge/search", json=request.model_dump(mode="json", by_alias=True), tenant=tenant
        )
        response = SearchResponse.model_validate(payload)
        hits: List[SearchHit] = []
        for hit in response.results:
            document = hit.document.model_copy(
                update={"namespace": namespace, "index_state": hit.document.index_state}
            )
            locator = hit.locator
            citation = hit.citation or make_citation(document, locator, hit.excerpt)
            hits.append(
                hit.model_copy(update={"document": document, "citation": citation})
            )
        return ProviderSearchResult(
            hits=hits,
            status=response.provider_status,
            mode_requested=response.mode_requested,
            mode_effective=response.mode_effective,
            degraded=response.degraded,
            degradation_reason=response.degradation_reason,
            warnings=list(response.warnings),
            duplicates_removed=response.duplicates_removed,
        )

    def read(self, request: ReadRequest, *, tenant: Optional[str] = None) -> ReadResponse:
        payload = self._request(
            "POST", "/knowledge/read", json=request.model_dump(mode="json", by_alias=True), tenant=tenant
        )
        return ReadResponse.model_validate(payload)

    def read_inline(
        self,
        *,
        name: str,
        mime_type: str,
        data: bytes,
        request: ReadRequest,
        tenant: Optional[str] = None,
    ) -> ReadResponse:
        payload = self._request(
            "POST",
            "/knowledge/read-inline",
            json={
                "name": name,
                "mime_type": mime_type,
                "content_base64": base64.b64encode(data).decode("ascii"),
                "request": request.model_dump(mode="json", by_alias=True),
            },
            tenant=tenant,
        )
        return ReadResponse.model_validate(payload)

    def create(self, payload: Dict[str, Any], *, tenant: Optional[str] = None) -> Dict[str, Any]:
        return self._request("POST", "/knowledge/documents/from-blocks", json=payload, tenant=tenant)

    def update(self, document_id: str, payload: Dict[str, Any], *, tenant: Optional[str] = None) -> Dict[str, Any]:
        return self._request("POST", f"/knowledge/documents/{document_id}/update", json=payload, tenant=tenant)

    def validate(self, document_id: str, *, tenant: Optional[str] = None) -> Dict[str, Any]:
        return self._request(
            "POST", f"/knowledge/documents/{document_id}/validate", json={"document_id": document_id}, tenant=tenant
        )

    def vision_analyze(
        self, image: bytes, mime_type: str, prompt: str = "", *, tenant: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/knowledge/vision/analyze",
            json={
                "image_base64": base64.b64encode(image).decode("ascii"),
                "mime_type": mime_type,
                "prompt": prompt,
            },
            tenant=tenant,
        )

    def capability(self) -> ProviderCapability:
        try:
            manifest = self.manifest(refresh=True)
        except KnowledgeError as exc:
            return ProviderCapability(
                provider=self.kind,
                available=False,
                contract_version=CONTRACT_VERSION,
                degraded_reason=exc.body.code.value,
            )
        return ProviderCapability(
            provider=self.kind,
            available=True,
            contract_version=manifest.contract_version,
            formats=manifest.formats,
            search_modes=manifest.search.modes,
            vision=manifest.vision,
            detail={"build": manifest.build, "extensions": manifest.extensions},
        )


class SessionAttachmentProvider(AgtoolsProvider):
    """AGTools documents scoped to one conversation's session-attachment tenant."""

    kind = ProviderKind.SESSION_ATTACHMENT

    def tenant_for(self, scope: ScopeLike, conversation_id: str) -> str:
        return attachment_tenant(scope, conversation_id)

    def search_for_conversation(
        self, scope: ScopeLike, conversation_id: str, request: SearchRequest
    ) -> ProviderSearchResult:
        tenant = self.tenant_for(scope, conversation_id)
        return self.search(request, tenant=tenant, namespace=DocumentNamespace.SESSION_ATTACHMENT)

    def read_for_conversation(self, scope: ScopeLike, conversation_id: str, request: ReadRequest) -> ReadResponse:
        tenant = self.tenant_for(scope, conversation_id)
        response = self.read(request, tenant=tenant)
        return response.model_copy(
            update={
                "document": response.document.model_copy(
                    update={"namespace": DocumentNamespace.SESSION_ATTACHMENT}
                )
            }
        )


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def workspace_document_ref(record: Dict[str, Any]) -> DocumentRef:
    return DocumentRef(
        namespace=DocumentNamespace.WORKSPACE,
        document_id=record["file_id"],
        name=record.get("name") or record["file_id"],
        mime_type=normalize_mime(record.get("mime_type"), filename=record.get("name")),
        revision=int(record.get("revision") or 1),
        content_hash=record.get("content_hash") or "",
        size_bytes=int(record.get("size_bytes") or 0),
        index_state=_index_state(record.get("index_status")),
        metadata={
            "workspace_id": record.get("workspace_id"),
            "path": record.get("rel_path") or record.get("name"),
            "updated_at": record.get("updated_at"),
        },
    )


def _index_state(value: Optional[str]) -> Optional[IndexState]:
    if not value:
        return None
    try:
        return IndexState(str(value).upper())
    except ValueError:
        return None


def _mimes_for(family: FormatFamily) -> List[str]:
    from app.knowledge.contracts import FAMILY_BY_MIME

    return [mime for mime, item in FAMILY_BY_MIME.items() if item is family]


def _flatten(values: List[Any]) -> List[str]:
    flat: List[str] = []
    for value in values:
        if isinstance(value, list):
            flat.extend(str(item) for item in value)
        else:
            flat.append(str(value))
    return flat


def _passes_filters(ref: DocumentRef, filters: SearchFilters) -> bool:
    if filters.names and not any(name.lower() in ref.name.lower() for name in filters.names):
        return False
    if filters.path_prefix and not str(ref.metadata.get("path", "")).startswith(filters.path_prefix):
        return False
    for key, value in filters.metadata.items():
        if str(ref.metadata.get(key, "")) != value:
            return False
    return True


def _error_from_response(response: httpx.Response) -> KnowledgeError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    body = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(body, dict):
        try:
            parsed = KnowledgeErrorBody.model_validate(body)
            return KnowledgeError(parsed.code, parsed.message, detail=parsed.detail, retryable=parsed.retryable)
        except Exception:
            pass
    detail = payload.get("detail") if isinstance(payload, dict) else None
    fallback = KnowledgeErrorCode.PROVIDER_UNAVAILABLE if response.status_code >= 500 else KnowledgeErrorCode.INVALID_REQUEST
    return KnowledgeError(
        fallback,
        "document engine request failed",
        detail={"status": response.status_code, "detail": str(detail)[:200]},
        retryable=response.status_code >= 500,
    )
