"""Citation contract: build (persisted), resolve and validate source references."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.workspace.contracts import (
    Citation,
    CitationResolveResponse,
    CitationValidationResponse,
    RevisionStatus,
)
from app.workspace.store import WorkspaceStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def excerpt_for_citation(content: str, max_chars: int = 600) -> str:
    cleaned = (content or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "…"


def revision_status(file_row: Optional[Dict[str, Any]], chunk_revision: int) -> RevisionStatus:
    if file_row is None or file_row.get("deleted_at"):
        return RevisionStatus.DELETED
    if int(file_row.get("revision") or 0) != int(chunk_revision):
        return RevisionStatus.SUPERSEDED
    return RevisionStatus.CURRENT


class CitationService:
    """Citations are persisted so they stay resolvable after a reindex or a file change."""

    def __init__(self, store: WorkspaceStore) -> None:
        self.store = store

    def build(
        self, chunk: Dict[str, Any], file_row: Optional[Dict[str, Any]], *, persist: bool = True
    ) -> Citation:
        citation = Citation(
            citation_id=chunk.get("citation_id")
            or "cit_" + hashlib.sha256(chunk["chunk_id"].encode("utf-8")).hexdigest()[:12],
            workspace_id=chunk["workspace_id"],
            file_id=chunk["file_id"],
            file_revision=int(chunk["file_revision"]),
            file_name=(file_row or {}).get("name") or chunk.get("file_name") or "",
            chunk_id=chunk["chunk_id"],
            page_number=chunk.get("page_number"),
            section=chunk.get("section"),
            source_excerpt=chunk.get("source_excerpt")
            if chunk.get("source_excerpt") is not None
            else excerpt_for_citation(chunk.get("content") or ""),
            revision_status=revision_status(file_row, int(chunk["file_revision"])),
        )
        if persist:
            try:
                self.store.upsert_citation(
                    {
                        "citation_id": citation.citation_id,
                        "workspace_id": citation.workspace_id,
                        "file_id": citation.file_id,
                        "file_revision": citation.file_revision,
                        "chunk_id": citation.chunk_id,
                        "file_name": citation.file_name,
                        "page_number": citation.page_number,
                        "section": citation.section,
                        "source_excerpt": citation.source_excerpt,
                        "created_at": _now(),
                    }
                )
            except Exception:
                # Citation persistence must never break context assembly.
                pass
        return citation

    def _blob_available(self, file_id: str, revision: int) -> bool:
        return self.store.get_revision(file_id, revision) is not None

    def resolve(self, workspace_id: str, citation_id: str) -> CitationResolveResponse:
        live_chunk = self.store.get_chunk_by_citation(workspace_id, citation_id)
        record = self.store.get_citation(workspace_id, citation_id)
        if live_chunk is not None:
            file_row = self.store.get_file(workspace_id, live_chunk["file_id"], include_deleted=True)
            citation = self.build(live_chunk, file_row)
        elif record is not None:
            file_row = self.store.get_file(workspace_id, record["file_id"], include_deleted=True)
            citation = Citation(
                citation_id=record["citation_id"],
                workspace_id=record["workspace_id"],
                file_id=record["file_id"],
                file_revision=int(record["file_revision"]),
                file_name=record.get("file_name") or "",
                chunk_id=record["chunk_id"],
                page_number=record.get("page_number"),
                section=record.get("section"),
                source_excerpt=record.get("source_excerpt") or "",
                revision_status=revision_status(file_row, int(record["file_revision"])),
            )
        else:
            return CitationResolveResponse(
                available=False,
                citation=None,
                revision_status=RevisionStatus.DELETED,
                download_url=None,
                reason="unknown_citation",
            )

        status = citation.revision_status
        file_intact = (
            file_row is not None
            and not file_row.get("deleted_at")
            and self._blob_available(citation.file_id, citation.file_revision)
        )
        available = bool(file_intact)
        download_url = (
            f"/api/v1/workspaces/{workspace_id}/files/{citation.file_id}/content"
            f"?revision={citation.file_revision}"
            if available
            else None
        )
        return CitationResolveResponse(
            available=available,
            citation=citation,
            revision_status=status,
            download_url=download_url,
            reason=None if available else status.value,
        )

    def validate(self, workspace_id: str, citation_ids: Sequence[str]) -> CitationValidationResponse:
        valid: List[str] = []
        unknown: List[str] = []
        seen = set()
        for citation_id in citation_ids:
            if not isinstance(citation_id, str) or not citation_id or citation_id in seen:
                continue
            seen.add(citation_id)
            resolved = self.resolve(workspace_id, citation_id)
            if resolved.available or (
                resolved.citation is not None and resolved.revision_status is RevisionStatus.SUPERSEDED
            ):
                valid.append(citation_id)
            else:
                unknown.append(citation_id)
        return CitationValidationResponse(valid=valid, unknown=unknown)
