"""Keyword, vector and hybrid (RRF) retrieval over workspace chunks."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import (
    ProviderStatus,
    SearchMode,
    SearchResponse,
    SearchResult,
)
from app.workspace.embedding import EmbeddingProvider, EmbeddingUnavailable
from app.workspace.store import WorkspaceStore
from app.workspace.textutil import coverage, excerpt, fts_query, query_terms


class WorkspaceRetriever:
    def __init__(
        self,
        store: WorkspaceStore,
        settings: WorkspaceSettings,
        embedder: Optional[EmbeddingProvider],
    ) -> None:
        self.store = store
        self.settings = settings
        self.embedder = embedder

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _hash_label(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]

    def _file_map(
        self, workspace_id: str, file_ids: Optional[Sequence[str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        rows = self.store.list_files(workspace_id, recursive=True, include_deleted=False)
        mapping = {row["file_id"]: row for row in rows if row["kind"] == "file"}
        if file_ids:
            allowed = set(file_ids)
            mapping = {file_id: row for file_id, row in mapping.items() if file_id in allowed}
        return mapping

    def embedder_available(self) -> bool:
        return bool(self.embedder is not None and getattr(self.embedder, "available", False))

    # ------------------------------------------------------------------ keyword
    def search_keyword(
        self,
        workspace_id: str,
        query: str,
        *,
        top_k: int = 8,
        file_ids: Optional[Sequence[str]] = None,
        mime_types: Optional[Sequence[str]] = None,
        min_score: float = 0.0,
    ) -> List[SearchResult]:
        match = fts_query(query)
        if not match:
            return []
        files = self._file_map(workspace_id, file_ids)
        if not files:
            return []
        if mime_types:
            allowed_mimes = {mime.lower() for mime in mime_types}
            files = {
                file_id: row
                for file_id, row in files.items()
                if (row.get("mime_type") or "").lower() in allowed_mimes
            }
            if not files:
                return []
        rows = self.store.search_keyword(workspace_id, match, max(top_k * 4, 20), list(files))
        if not rows:
            return []
        terms = query_terms(query)
        strengths = [max(0.0, -float(row.get("rank") or 0.0)) for row in rows]
        peak = max(strengths) if strengths else 0.0
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for row, strength in zip(rows, strengths):
            cov = coverage(row["content"], terms) if terms else 0.5
            normalized = (strength / peak) if peak > 0 else 0.0
            score = 0.6 * cov + 0.4 * normalized
            if score < min_score:
                continue
            scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["chunk_index"]))
        results: List[SearchResult] = []
        for score, row in scored[:top_k]:
            file_row = files.get(row["file_id"]) or {}
            results.append(
                SearchResult(
                    chunk_id=row["chunk_id"],
                    file_id=row["file_id"],
                    file_revision=int(row["file_revision"]),
                    file_name=file_row.get("name") or "",
                    page_number=row.get("page_number"),
                    section=row.get("section"),
                    excerpt=excerpt(row["content"], query),
                    score=round(score, 6),
                    keyword_score=round(score, 6),
                    vector_score=None,
                    matched_by=["keyword"],
                )
            )
        return results

    # ------------------------------------------------------------------ vector
    def search_vector(
        self,
        workspace_id: str,
        query: str,
        *,
        top_k: int = 8,
        file_ids: Optional[Sequence[str]] = None,
        mime_types: Optional[Sequence[str]] = None,
        min_score: float = 0.0,
    ) -> List[SearchResult]:
        if not self.embedder_available():
            return []
        index_key = getattr(self.embedder, "index_key", f"{self.embedder.model_id}:{self.embedder.dimension}")
        rows = self.store.load_vectors(workspace_id, index_key)
        if not rows:
            return []
        files = self._file_map(workspace_id, file_ids)
        if not files:
            return []
        if mime_types:
            allowed_mimes = {mime.lower() for mime in mime_types}
            files = {
                file_id: row
                for file_id, row in files.items()
                if (row.get("mime_type") or "").lower() in allowed_mimes
            }
            if not files:
                return []
        filtered = [row for row in rows if row["file_id"] in files]
        if not filtered:
            return []
        query_vector = np.asarray(self.embedder.embed_query(query), dtype=np.float32)
        query_norm = float(np.linalg.norm(query_vector))
        if query_norm <= 0:
            return []
        query_vector = query_vector / query_norm
        matrix = []
        usable: List[Dict[str, Any]] = []
        for row in filtered:
            vector = np.frombuffer(row["vector"], dtype=np.float32)
            if vector.shape[0] != query_vector.shape[0]:
                continue
            norm = float(np.linalg.norm(vector))
            if norm <= 0:
                continue
            matrix.append(vector / norm)
            usable.append(row)
        if not matrix:
            return []
        similarities = np.asarray(matrix, dtype=np.float32) @ query_vector
        candidates: List[Tuple[float, Dict[str, Any]]] = []
        for similarity, row in zip(similarities.tolist(), usable):
            score = float(max(0.0, min(1.0, (similarity + 1.0) / 2.0)))
            if score < min_score:
                continue
            candidates.append((score, row))
        candidates.sort(key=lambda item: -item[0])
        results: List[SearchResult] = []
        for score, row in candidates[:top_k]:
            file_row = files.get(row["file_id"]) or {}
            content = row.get("content") or ""
            results.append(
                SearchResult(
                    chunk_id=row["chunk_id"],
                    file_id=row["file_id"],
                    file_revision=int(row["file_revision"]),
                    file_name=file_row.get("name") or "",
                    page_number=row.get("page_number"),
                    section=row.get("section"),
                    excerpt=excerpt(content, query),
                    score=round(score, 6),
                    keyword_score=None,
                    vector_score=round(score, 6),
                    matched_by=["vector"],
                )
            )
        return results

    # ------------------------------------------------------------------ hybrid
    @staticmethod
    def _rrf_fuse(lists: Sequence[Sequence[SearchResult]], k: int = 60) -> List[SearchResult]:
        fused: Dict[str, Dict[str, Any]] = {}
        for results in lists:
            for position, result in enumerate(results, start=1):
                record = fused.setdefault(
                    result.chunk_id,
                    {
                        "result": result,
                        "score": 0.0,
                        "keyword": result.keyword_score,
                        "vector": result.vector_score,
                        "matched": set(),
                    },
                )
                record["score"] += 1.0 / (k + position)
                if result.keyword_score is not None:
                    record["keyword"] = max(record["keyword"] or 0.0, result.keyword_score)
                if result.vector_score is not None:
                    record["vector"] = max(record["vector"] or 0.0, result.vector_score)
                record["matched"].update(result.matched_by)
        maximum = 2.0 / (k + 1) if lists else 1.0
        fused_results: List[SearchResult] = []
        for record in fused.values():
            base: SearchResult = record["result"]
            fused_results.append(
                base.model_copy(
                    update={
                        "score": round(min(1.0, record["score"] / maximum), 6),
                        "keyword_score": record["keyword"],
                        "vector_score": record["vector"],
                        "matched_by": sorted(record["matched"]),
                    }
                )
            )
        fused_results.sort(key=lambda item: -item.score)
        return fused_results

    def _chunk_positions(self, results: Sequence[SearchResult]) -> Dict[str, int]:
        positions: Dict[str, int] = {}
        for file_id in {result.file_id for result in results}:
            for row in self.store.chunks_for_file(file_id):
                positions[row["chunk_id"]] = int(row["chunk_index"])
        return positions

    def _suppress_adjacent(self, results: Sequence[SearchResult], limit: int) -> List[SearchResult]:
        """Keep the best chunk when neighbouring chunks of the same file both match."""

        positions = self._chunk_positions(results)
        kept: List[SearchResult] = []
        kept_positions: set[Tuple[str, int]] = set()
        for result in results:
            index = positions.get(result.chunk_id)
            if index is not None:
                if (result.file_id, index - 1) in kept_positions or (result.file_id, index + 1) in kept_positions:
                    continue
                kept_positions.add((result.file_id, index))
            kept.append(result)
            if len(kept) >= limit:
                break
        return kept

    # ------------------------------------------------------------------ entry point
    def search(
        self,
        workspace_id: str,
        query: str,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        top_k: Optional[int] = None,
        file_ids: Optional[Sequence[str]] = None,
        mime_types: Optional[Sequence[str]] = None,
        min_score: Optional[float] = None,
        allow_degraded: bool = True,
    ) -> SearchResponse:
        from app.workspace.manager import WorkspaceFailure
        from app.workspace.contracts import WorkspaceErrorCode

        cleaned = (query or "").strip()
        if not cleaned:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "empty query")
        limit = max(1, min(int(top_k or self.settings.search_default_top_k), self.settings.search_max_top_k))
        threshold = float(self.settings.search_min_score if min_score is None else min_score)

        requested = SearchMode(mode)
        effective = requested
        degraded = False
        reason: Optional[str] = None
        provider_status = ProviderStatus(keyword="ok", embedding="ok")

        embedding_ready = self.embedder_available()
        if requested in {SearchMode.VECTOR, SearchMode.HYBRID} and not embedding_ready:
            if not allow_degraded:
                raise WorkspaceFailure(
                    WorkspaceErrorCode.EMBEDDING_UNAVAILABLE, "embedding provider unavailable"
                )
            effective = SearchMode.KEYWORD
            degraded = True
            reason = "embedding_unavailable"
            provider_status.embedding = "unavailable"

        keyword_results: List[SearchResult] = []
        vector_results: List[SearchResult] = []
        try:
            if effective in {SearchMode.KEYWORD, SearchMode.HYBRID}:
                keyword_results = self.search_keyword(
                    workspace_id,
                    cleaned,
                    top_k=limit,
                    file_ids=file_ids,
                    mime_types=mime_types,
                    min_score=threshold,
                )
            if effective in {SearchMode.VECTOR, SearchMode.HYBRID} and embedding_ready:
                vector_results = self.search_vector(
                    workspace_id,
                    cleaned,
                    top_k=limit,
                    file_ids=file_ids,
                    mime_types=mime_types,
                    min_score=threshold,
                )
                if effective is SearchMode.HYBRID and not vector_results:
                    index_key = getattr(
                        self.embedder, "index_key", f"{self.embedder.model_id}:{self.embedder.dimension}"
                    )
                    if self.store.count_vectors(workspace_id, index_key) == 0:
                        provider_status.embedding = "empty_index"
                        effective = SearchMode.KEYWORD
                        degraded = True
                        reason = reason or "vector_index_empty"
        except EmbeddingUnavailable:
            provider_status.embedding = "unavailable"
            if effective in {SearchMode.VECTOR, SearchMode.HYBRID}:
                if not allow_degraded:
                    raise WorkspaceFailure(
                        WorkspaceErrorCode.EMBEDDING_UNAVAILABLE, "embedding provider failed"
                    )
                effective = SearchMode.KEYWORD
                degraded = True
                reason = "embedding_failed"
                vector_results = []

        if effective is SearchMode.KEYWORD:
            results = keyword_results
        elif effective is SearchMode.VECTOR:
            results = vector_results
        else:
            results = self._rrf_fuse([keyword_results, vector_results], k=self.settings.search_rrf_k)
            results = self._suppress_adjacent(results, limit)
        results = results[:limit]
        return SearchResponse(
            workspace_id=workspace_id,
            query=cleaned,
            mode_requested=requested,
            mode_effective=effective,
            degraded=degraded,
            degradation_reason=reason,
            provider_status=provider_status,
            total_matches=len(results),
            results=results,
        )
