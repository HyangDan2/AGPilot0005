"""Document ingestion: reuse the AGTools (AGPilot0006) parser over HTTP, extract plain text locally.

No PDF/DOCX/PPTX parser is implemented here. Structured formats go to the existing AGTools
Document API; text-like files are decoded locally and chunked by `chunking.py`.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

import httpx

from app.workspace.chunking import CHUNKING_VERSION, SourceChunk, chunk_text
from app.workspace.config import WorkspaceSettings

STRUCTURED_EXTENSIONS = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".log", ".ini", ".cfg", ".toml", ".rst", ".tex", ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".sql", ".sh",
}
TEXT_MIME_PREFIXES = ("text/",)
TEXT_MIME_EXACT = {"application/json", "application/xml", "application/x-yaml", "application/csv"}


class ExtractionError(Exception):
    """Document extraction failure carrying a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:500]
        super().__init__(code)


class ParserUnavailable(ExtractionError):
    def __init__(self, detail: str = "parser service unreachable") -> None:
        super().__init__("parser_unavailable", detail)


@dataclass
class ExtractionResult:
    chunks: List[SourceChunk]
    parser_version: str
    chunking_version: str
    extraction_source: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class DocumentIngestor(Protocol):
    def supports(self, name: str, mime_type: Optional[str]) -> bool: ...

    def extract(self, name: str, mime_type: Optional[str], data: bytes) -> ExtractionResult: ...


def _extension(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def _is_textual(name: str, mime_type: Optional[str]) -> bool:
    extension = _extension(name)
    if extension in TEXT_EXTENSIONS:
        return True
    mime = (mime_type or "").lower()
    if any(mime.startswith(prefix) for prefix in TEXT_MIME_PREFIXES):
        return True
    return mime in TEXT_MIME_EXACT


class LocalTextIngestor:
    """Local extraction for text-like files (no structured-document parsing)."""

    parser_version = "local-text-v1"

    def __init__(self, settings: WorkspaceSettings) -> None:
        self.settings = settings

    def supports(self, name: str, mime_type: Optional[str]) -> bool:
        return _is_textual(name, mime_type)

    def extract(self, name: str, mime_type: Optional[str], data: bytes) -> ExtractionResult:
        text = data.decode("utf-8", errors="replace")
        if text.startswith("\ufeff"):
            text = text.lstrip("\ufeff")
        chunks = chunk_text(
            text,
            target_tokens=self.settings.chunk_target_tokens,
            overlap_tokens=self.settings.chunk_overlap_tokens,
            max_tokens=self.settings.chunk_max_tokens,
            page_number=None,
            section=None,
        )
        if not chunks:
            raise ExtractionError("empty_document", "no text content")
        return ExtractionResult(
            chunks=chunks,
            parser_version=self.parser_version,
            chunking_version=CHUNKING_VERSION,
            extraction_source="local_text",
        )


class AGToolsIngestor:
    """Reuses the AGTools document parser and chunking results (AGPilot0006)."""

    def __init__(
        self,
        settings: WorkspaceSettings,
        *,
        tenant_id: str = "",
        owner_id: str = "",
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.settings = settings
        self.tenant_id = tenant_id
        self.owner_id = owner_id
        self._client = client

    # -- helpers -----------------------------------------------------------------
    def supports(self, name: str, mime_type: Optional[str]) -> bool:
        if not self.settings.agtools_enabled:
            return False
        return _extension(name) in STRUCTURED_EXTENSIONS

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"X-Access-Scope": "read_write"}
        if self.settings.agtools_token:
            headers["X-Service-Token"] = self.settings.agtools_token
        if self.owner_id:
            headers["X-Owner-Id"] = self.owner_id
        if self.tenant_id:
            headers["X-Tenant-Id"] = self.tenant_id
        return headers

    def _client_or_new(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return (
            httpx.Client(
                base_url=self.settings.agtools_url.rstrip("/"),
                timeout=self.settings.agtools_timeout_seconds,
                trust_env=False,
            ),
            True,
        )

    def _wait_for_parse(self, client: httpx.Client, document_id: str) -> Dict[str, Any]:
        import time

        deadline = time.monotonic() + max(1.0, self.settings.parser_max_wait_seconds)
        while True:
            response = client.get(f"/documents/{document_id}/status", headers=self._headers())
            if response.status_code == 404:
                raise ExtractionError("parser_rejected", "document disappeared during parsing")
            response.raise_for_status()
            status = response.json()
            state = (status.get("status") or "").lower()
            if state in {"completed", "ready"}:
                return status
            if state in {"failed", "error", "cancelled"}:
                raise ExtractionError("parser_failed", str(status.get("error_message") or state))
            if time.monotonic() >= deadline:
                raise ExtractionError("parser_timeout", "document parsing exceeded the wait budget")
            time.sleep(max(0.01, self.settings.parser_poll_interval_seconds))

    def _section_titles(self, client: httpx.Client, document_id: str) -> Dict[str, str]:
        try:
            response = client.get(
                f"/documents/{document_id}/outline", params={"limit": 500}, headers=self._headers()
            )
            if response.status_code != 200:
                return {}
            payload = response.json()
            return {
                str(section.get("section_id")): str(section.get("title") or "")
                for section in payload.get("sections") or []
                if section.get("section_id")
            }
        except Exception:
            return {}

    def _list_chunks(self, client: httpx.Client, document_id: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = client.get(f"/documents/{document_id}/chunks", params=params, headers=self._headers())
            response.raise_for_status()
            payload = response.json()
            items.extend(payload.get("chunks") or [])
            cursor = payload.get("next_cursor")
            if not cursor or len(items) >= self.settings.index_max_chunks_per_file:
                break
        return items[: self.settings.index_max_chunks_per_file]

    @staticmethod
    def _page_of(source: Dict[str, Any]) -> Optional[int]:
        if not isinstance(source, dict):
            return None
        for key in ("page", "slide", "slide_start"):
            value = source.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return None

    def extract(self, name: str, mime_type: Optional[str], data: bytes) -> ExtractionResult:
        client, owned = self._client_or_new()
        try:
            files = {
                "file": (
                    name,
                    data,
                    mime_type or STRUCTURED_EXTENSIONS.get(_extension(name), "application/octet-stream"),
                )
            }
            try:
                upload = client.post("/documents", files=files, headers=self._headers())
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                raise ParserUnavailable(str(exc)) from exc
            if upload.status_code not in (200, 201, 202):
                raise ExtractionError("parser_rejected", f"HTTP {upload.status_code}")
            document_id = str(upload.json().get("document_id") or "")
            if not document_id:
                raise ExtractionError("parser_rejected", "missing document_id")

            status = self._wait_for_parse(client, document_id)
            parser_version = str(status.get("parser_version") or "agtools")

            section_titles = self._section_titles(client, document_id)
            listed = self._list_chunks(client, document_id)
            if not listed:
                text_response = client.get(f"/documents/{document_id}/text", headers=self._headers())
                if text_response.status_code != 200:
                    raise ExtractionError("empty_document", "parser returned no chunks")
                text = str(text_response.json().get("text") or "")
                chunks = chunk_text(
                    text,
                    target_tokens=self.settings.chunk_target_tokens,
                    overlap_tokens=self.settings.chunk_overlap_tokens,
                    max_tokens=self.settings.chunk_max_tokens,
                )
                if not chunks:
                    raise ExtractionError("empty_document", "parser returned empty text")
                return ExtractionResult(
                    chunks=chunks,
                    parser_version=parser_version,
                    chunking_version=CHUNKING_VERSION,
                    extraction_source="agtools_text",
                    metadata={"document_id": document_id},
                )

            # Chunk list deliberately omits content; fetch details with bounded concurrency.
            def fetch(item: Dict[str, Any]) -> SourceChunk:
                chunk_id = str(item.get("chunk_id"))
                detail = client.get(f"/documents/{document_id}/chunks/{chunk_id}", headers=self._headers())
                detail.raise_for_status()
                payload = detail.json()
                source = payload.get("source") or item.get("source") or {}
                section_id = str(payload.get("parent_section_id") or item.get("section_id") or "")
                content = str(payload.get("content") or "")
                return SourceChunk(
                    content=content,
                    token_count=int(payload.get("token_count") or 0),
                    page_number=self._page_of(source),
                    section=section_titles.get(section_id) or None,
                    offset_start=0,
                    offset_end=len(content),
                    metadata={"agtools_chunk_id": chunk_id, "section_id": section_id or None},
                )

            with ThreadPoolExecutor(max_workers=max(1, self.settings.parser_chunk_fetch_concurrency)) as pool:
                chunks = list(pool.map(fetch, listed))
            chunks = [chunk for chunk in chunks if chunk.content.strip()]
            if not chunks:
                raise ExtractionError("empty_document", "parser chunks were empty")
            return ExtractionResult(
                chunks=chunks,
                parser_version=parser_version,
                chunking_version="agtools",
                extraction_source="agtools",
                metadata={"document_id": document_id, "chunks_from_parser": True},
            )
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.ConnectError):
                raise ParserUnavailable(str(exc)) from exc
            raise ExtractionError("parser_failed", str(exc)) from exc
        finally:
            if owned:
                client.close()


class CompositeIngestor:
    """Dispatches to AGTools for structured documents and to local text extraction otherwise."""

    def __init__(self, settings: WorkspaceSettings, agtools: Optional[AGToolsIngestor] = None) -> None:
        self.settings = settings
        self.local = LocalTextIngestor(settings)
        self.agtools = agtools

    def for_scope(self, tenant_id: str, owner_id: str) -> "CompositeIngestor":
        """Return an ingestor bound to the caller scope (AGTools owner/tenant headers)."""

        agtools = None
        if self.settings.agtools_enabled:
            agtools = AGToolsIngestor(self.settings, tenant_id=tenant_id, owner_id=owner_id)
        return CompositeIngestor(self.settings, agtools)

    def supports(self, name: str, mime_type: Optional[str]) -> bool:
        return self.local.supports(name, mime_type) or bool(self.agtools and self.agtools.supports(name, mime_type))

    def extract(self, name: str, mime_type: Optional[str], data: bytes) -> ExtractionResult:
        if self.agtools is not None and self.agtools.supports(name, mime_type):
            try:
                return self.agtools.extract(name, mime_type, data)
            except ParserUnavailable:
                if self.local.supports(name, mime_type):
                    return self.local.extract(name, mime_type, data)
                raise
            except ExtractionError:
                raise
        if self.local.supports(name, mime_type):
            return self.local.extract(name, mime_type, data)
        raise ExtractionError("unsupported_media_type", f"no parser for {mime_type or _extension(name)}")
