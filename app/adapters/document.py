"""Document Service Adapter Interface and Implementations.

Adheres to Single Responsibility Principle (SRP):
- Tier 1 (AGPilot0006): Pure Document Intelligence (Parsing, Hierarchy, Chunking, Search, Asset rendering).
- Tier 2 (AGCore Adapter): Protocol translation, token budget protection, and mock resilience fallback.
"""
import logging
from typing import Any, Dict, List, Optional
import httpx
from pydantic import BaseModel
from app.config import settings

logger = logging.getLogger("agcore.adapters.document")

class DocumentSection(BaseModel):
    section_id: str
    title: str
    level: int
    content: str
    subsections: List[str] = []

class DocumentAdapter:
    """Interface for querying document intelligence services."""
    async def get_outline(self, doc_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def read_section(self, doc_id: str, section_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def search(self, doc_id: str, query: str, limit: int = 5) -> Dict[str, Any]:
        raise NotImplementedError

    async def read_chunk(self, doc_id: str, chunk_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_figure_info(self, doc_id: str, figure_id: str) -> Dict[str, Any]:
        raise NotImplementedError


class MockDocumentAdapter(DocumentAdapter):
    """In-memory Mock implementation for MVP, local testing, and offline fallback."""
    def __init__(self):
        self._docs = {
            "hd2lab-guide": {
                "title": "HD2Lab Platform Architecture Guide",
                "outline": [
                    {"section_id": "sec-1", "title": "1. Overview & Vision", "level": 1, "page": 1},
                    {"section_id": "sec-2", "title": "2. AI Gateway Architecture", "level": 1, "page": 3},
                    {"section_id": "sec-2-1", "title": "2.1 Routing and Policies", "level": 2, "page": 4},
                    {"section_id": "sec-3", "title": "3. Agent Core Platform", "level": 1, "page": 7},
                ],
                "sections": {
                    "sec-1": {
                        "title": "1. Overview & Vision",
                        "content": "HD2Lab은 다양한 생성형 AI 애플리케이션(HD2AI, Lingo, Universe 등)을 통합 서비스하는 차세대 플랫폼입니다.",
                        "chunk_ids": ["chk-1-1"],
                        "figure_ids": ["fig-hd2lab-logo"],
                    },
                    "sec-2": {
                        "title": "2. AI Gateway Architecture",
                        "content": "AI Gateway는 모든 AI 모델 호출의 단일 진입점으로서 인증, Rate Limiting, Persona 주입 및 토큰 예산을 관리합니다.",
                        "chunk_ids": ["chk-2-1"],
                        "figure_ids": ["fig-arch-overview"],
                    },
                    "sec-2-1": {
                        "title": "2.1 Routing and Policies",
                        "content": "AI Gateway v3는 5개의 전용 라우트(hd2ai-admin, hd2ai-public, lingo-admin, lingo-public, universe)를 독립적으로 제어합니다.",
                        "chunk_ids": ["chk-2-1-1"],
                        "figure_ids": [],
                    },
                    "sec-3": {
                        "title": "3. Agent Core Platform",
                        "content": "AGCore(AGPilot0005)는 LangGraph 기반 독립 에이전트 런타임으로서 도구 선택, 다단계 실행 루프, 컨텍스트 예산 관리를 담당합니다.",
                        "chunk_ids": ["chk-3-1"],
                        "figure_ids": ["fig-agent-loop"],
                    },
                },
                "chunks": {
                    "chk-1-1": {
                        "chunk_id": "chk-1-1",
                        "parent_section_id": "sec-1",
                        "content": "HD2Lab은 다양한 생성형 AI 애플리케이션(HD2AI, Lingo, Universe 등)을 통합 서비스하는 차세대 플랫폼입니다.",
                        "token_count": 42,
                        "source": {"type": "docx", "page": 1},
                    },
                    "chk-2-1": {
                        "chunk_id": "chk-2-1",
                        "parent_section_id": "sec-2",
                        "content": "AI Gateway는 모든 AI 모델 호출의 단일 진입점으로서 인증, Rate Limiting, Persona 주입 및 토큰 예산을 관리합니다.",
                        "token_count": 55,
                        "source": {"type": "docx", "page": 3},
                    },
                    "chk-2-1-1": {
                        "chunk_id": "chk-2-1-1",
                        "parent_section_id": "sec-2-1",
                        "content": "AI Gateway v3는 Routing 및 정책을 기반으로 5개의 전용 라우트(hd2ai-admin, hd2ai-public, lingo-admin, lingo-public, universe)를 독립적으로 제어합니다.",
                        "token_count": 52,
                        "source": {"type": "docx", "page": 4},
                    },
                    "chk-3-1": {
                        "chunk_id": "chk-3-1",
                        "parent_section_id": "sec-3",
                        "content": "AGCore(AGPilot0005)는 LangGraph 기반 독립 에이전트 런타임으로서 도구 선택, 다단계 실행 루프, 컨텍스트 예산 관리를 담당합니다.",
                        "token_count": 58,
                        "source": {"type": "docx", "page": 7},
                    },
                },
                "figures": {
                    "fig-hd2lab-logo": {
                        "figure_id": "fig-hd2lab-logo",
                        "document_id": "hd2lab-guide",
                        "caption": "HD2Lab Geometric Skew Mark Brand Logo",
                        "page_or_slide": 1,
                        "source_type": "embedded_image",
                        "asset_url": "/api/v1/documents/hd2lab-guide/figures/fig-hd2lab-logo/asset",
                    },
                    "fig-arch-overview": {
                        "figure_id": "fig-arch-overview",
                        "document_id": "hd2lab-guide",
                        "caption": "AI Gateway Topology and Route Architecture",
                        "page_or_slide": 3,
                        "source_type": "slide_render",
                        "asset_url": "/api/v1/documents/hd2lab-guide/figures/fig-arch-overview/asset",
                    },
                    "fig-agent-loop": {
                        "figure_id": "fig-agent-loop",
                        "document_id": "hd2lab-guide",
                        "caption": "LangGraph Agent Execution Loop Diagram",
                        "page_or_slide": 7,
                        "source_type": "chart",
                        "asset_url": "/api/v1/documents/hd2lab-guide/figures/fig-agent-loop/asset",
                    },
                },
            }
        }

    async def get_outline(self, doc_id: str) -> Dict[str, Any]:
        if doc_id not in self._docs:
            return {"error": True, "message": f"Document '{doc_id}' not found."}
        doc = self._docs[doc_id]
        return {
            "doc_id": doc_id,
            "title": doc["title"],
            "outline": doc["outline"],
        }

    async def read_section(self, doc_id: str, section_id: str) -> Dict[str, Any]:
        if doc_id not in self._docs:
            return {"error": True, "message": f"Document '{doc_id}' not found."}
        doc = self._docs[doc_id]
        if section_id not in doc["sections"]:
            return {"error": True, "message": f"Section '{section_id}' not found in document '{doc_id}'."}
        sec = doc["sections"][section_id]
        return {
            "doc_id": doc_id,
            "section_id": section_id,
            "title": sec.get("title", ""),
            "content": sec["content"],
            "chunk_ids": sec.get("chunk_ids", []),
            "figure_ids": sec.get("figure_ids", []),
        }

    async def search(self, doc_id: str, query: str, limit: int = 5) -> Dict[str, Any]:
        if doc_id not in self._docs:
            return {"error": True, "message": f"Document '{doc_id}' not found."}
        doc = self._docs[doc_id]
        matches = []
        q_lower = query.lower()
        for chunk in doc.get("chunks", {}).values():
            if q_lower in chunk["content"].lower():
                matches.append({
                    "chunk_id": chunk["chunk_id"],
                    "section_id": chunk["parent_section_id"],
                    "snippet": chunk["content"],
                    "score": 1.0,
                    "source": chunk["source"],
                })
                if len(matches) >= limit:
                    break
        return {
            "doc_id": doc_id,
            "query": query,
            "total_matches": len(matches),
            "results": matches,
        }

    async def read_chunk(self, doc_id: str, chunk_id: str) -> Dict[str, Any]:
        if doc_id not in self._docs:
            return {"error": True, "message": f"Document '{doc_id}' not found."}
        doc = self._docs[doc_id]
        if chunk_id not in doc.get("chunks", {}):
            return {"error": True, "message": f"Chunk '{chunk_id}' not found in document '{doc_id}'."}
        return {
            "doc_id": doc_id,
            **doc["chunks"][chunk_id]
        }

    async def get_figure_info(self, doc_id: str, figure_id: str) -> Dict[str, Any]:
        if doc_id not in self._docs:
            return {"error": True, "message": f"Document '{doc_id}' not found."}
        doc = self._docs[doc_id]
        if figure_id not in doc.get("figures", {}):
            return {"error": True, "message": f"Figure '{figure_id}' not found in document '{doc_id}'."}
        return {
            "doc_id": doc_id,
            **doc["figures"][figure_id]
        }


class AGToolsHttpAdapter(DocumentAdapter):
    """Direct HTTP REST client adapter connecting to AGPilot0006 Document Intelligence Service."""
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None, timeout: Optional[float] = None):
        self.base_url = (base_url or settings.AGTOOLS_URL).rstrip("/")
        self.token = token or settings.AGTOOLS_TOKEN
        self.timeout = timeout or settings.AGTOOLS_TIMEOUT_SECONDS

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "HD2Lab-AGCore/1.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def get_outline(self, doc_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/documents/{doc_id}/outline"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "doc_id": doc_id,
                    "status": data.get("status"),
                    "outline": data.get("sections", []),
                }
            if resp.status_code == 404:
                return {"error": True, "code": "not_found", "message": f"Document '{doc_id}' not found in AGTools."}
            return {"error": True, "code": f"http_{resp.status_code}", "message": resp.text}

    async def read_section(self, doc_id: str, section_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/documents/{doc_id}/sections/{section_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "doc_id": doc_id,
                    "section_id": data.get("section_id"),
                    "title": data.get("title"),
                    "content": data.get("content"),
                    "figure_ids": data.get("figure_ids", []),
                    "source": data.get("source"),
                }
            if resp.status_code == 404:
                return {"error": True, "code": "not_found", "message": f"Section '{section_id}' not found in document '{doc_id}'."}
            return {"error": True, "code": f"http_{resp.status_code}", "message": resp.text}

    async def search(self, doc_id: str, query: str, limit: int = 5) -> Dict[str, Any]:
        url = f"{self.base_url}/documents/{doc_id}/search"
        payload = {"query": query, "limit": limit}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "doc_id": doc_id,
                    "query": query,
                    "total_matches": data.get("total_matches", 0),
                    "results": data.get("results", []),
                }
            if resp.status_code == 404:
                return {"error": True, "code": "not_found", "message": f"Document '{doc_id}' not found in AGTools."}
            return {"error": True, "code": f"http_{resp.status_code}", "message": resp.text}

    async def read_chunk(self, doc_id: str, chunk_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/documents/{doc_id}/chunks/{chunk_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "doc_id": doc_id,
                    "chunk_id": data.get("chunk_id"),
                    "parent_section_id": data.get("parent_section_id"),
                    "content": data.get("content"),
                    "token_count": data.get("token_count", 0),
                    "figure_ids": data.get("figure_ids", []),
                    "source": data.get("source"),
                }
            if resp.status_code == 404:
                return {"error": True, "code": "not_found", "message": f"Chunk '{chunk_id}' not found in document '{doc_id}'."}
            return {"error": True, "code": f"http_{resp.status_code}", "message": resp.text}

    async def get_figure_info(self, doc_id: str, figure_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/documents/{doc_id}/figures/{figure_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json()
                asset_id = data.get("asset_id")
                return {
                    "doc_id": doc_id,
                    "figure_id": data.get("figure_id"),
                    "caption": data.get("caption"),
                    "page_or_slide": data.get("page_or_slide"),
                    "source_type": data.get("source_type"),
                    "visual_description": data.get("visual_description"),
                    "asset_url": f"{self.base_url}/documents/{doc_id}/figures/{figure_id}/asset",
                }
            if resp.status_code == 404:
                return {"error": True, "code": "not_found", "message": f"Figure '{figure_id}' not found in document '{doc_id}'."}
            return {"error": True, "code": f"http_{resp.status_code}", "message": resp.text}


class CompositeDocumentAdapter(DocumentAdapter):
    """Resilient composite adapter: Queries live AGTools first; falls back to Mock only when ALLOW_MOCK_FALLBACK is enabled."""
    def __init__(self, http_adapter: Optional[AGToolsHttpAdapter] = None, mock_adapter: Optional[MockDocumentAdapter] = None):
        self.http = http_adapter or AGToolsHttpAdapter()
        self.mock = mock_adapter or MockDocumentAdapter()

    async def get_outline(self, doc_id: str) -> Dict[str, Any]:
        try:
            res = await self.http.get_outline(doc_id)
            if not res.get("error"):
                return res
            if not settings.ALLOW_MOCK_FALLBACK:
                return res
        except Exception as e:
            logger.warning("AGTools HTTP outline error: %s", e)
            if not settings.ALLOW_MOCK_FALLBACK:
                return {"error": True, "code": "connection_failed", "message": f"AGTools 연결 실패: {e}"}
        return await self.mock.get_outline(doc_id)

    async def read_section(self, doc_id: str, section_id: str) -> Dict[str, Any]:
        try:
            res = await self.http.read_section(doc_id, section_id)
            if not res.get("error"):
                return res
            if not settings.ALLOW_MOCK_FALLBACK:
                return res
        except Exception as e:
            logger.warning("AGTools HTTP read_section error: %s", e)
            if not settings.ALLOW_MOCK_FALLBACK:
                return {"error": True, "code": "connection_failed", "message": f"AGTools 연결 실패: {e}"}
        return await self.mock.read_section(doc_id, section_id)

    async def search(self, doc_id: str, query: str, limit: int = 5) -> Dict[str, Any]:
        try:
            res = await self.http.search(doc_id, query, limit)
            if not res.get("error"):
                return res
            if not settings.ALLOW_MOCK_FALLBACK:
                return res
        except Exception as e:
            logger.warning("AGTools HTTP search error: %s", e)
            if not settings.ALLOW_MOCK_FALLBACK:
                return {"error": True, "code": "connection_failed", "message": f"AGTools 연결 실패: {e}"}
        return await self.mock.search(doc_id, query, limit)

    async def read_chunk(self, doc_id: str, chunk_id: str) -> Dict[str, Any]:
        try:
            res = await self.http.read_chunk(doc_id, chunk_id)
            if not res.get("error"):
                return res
            if not settings.ALLOW_MOCK_FALLBACK:
                return res
        except Exception as e:
            logger.warning("AGTools HTTP read_chunk error: %s", e)
            if not settings.ALLOW_MOCK_FALLBACK:
                return {"error": True, "code": "connection_failed", "message": f"AGTools 연결 실패: {e}"}
        return await self.mock.read_chunk(doc_id, chunk_id)

    async def get_figure_info(self, doc_id: str, figure_id: str) -> Dict[str, Any]:
        try:
            res = await self.http.get_figure_info(doc_id, figure_id)
            if not res.get("error"):
                return res
            if not settings.ALLOW_MOCK_FALLBACK:
                return res
        except Exception as e:
            logger.warning("AGTools HTTP get_figure_info error: %s", e)
            if not settings.ALLOW_MOCK_FALLBACK:
                return {"error": True, "code": "connection_failed", "message": f"AGTools 연결 실패: {e}"}
        return await self.mock.get_figure_info(doc_id, figure_id)


# Primary document adapter used across AGCore tools
document_adapter: DocumentAdapter = CompositeDocumentAdapter()
