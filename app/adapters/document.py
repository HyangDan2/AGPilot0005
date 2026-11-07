"""Document Service Adapter Interface and Mock Implementation."""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

class DocumentSection(BaseModel):
    section_id: str
    title: str
    level: int
    content: str
    subsections: List[str] = []

class DocumentAdapter:
    """Interface for querying external document services."""
    async def get_outline(self, doc_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    async def read_section(self, doc_id: str, section_id: str) -> Dict[str, Any]:
        raise NotImplementedError

class MockDocumentAdapter(DocumentAdapter):
    """In-memory Mock implementation for MVP and QA tests."""
    def __init__(self):
        self._docs = {
            "hd2lab-guide": {
                "title": "HD2Lab Platform Architecture Guide",
                "outline": [
                    {"section_id": "sec-1", "title": "1. Overview & Vision", "level": 1},
                    {"section_id": "sec-2", "title": "2. AI Gateway Architecture", "level": 1},
                    {"section_id": "sec-2-1", "title": "2.1 Routing and Policies", "level": 2},
                    {"section_id": "sec-3", "title": "3. Agent Core Platform", "level": 1},
                ],
                "sections": {
                    "sec-1": "HD2Lab은 다양한 생성형 AI 애플리케이션(HD2AI, Lingo, Universe 등)을 통합 서비스하는 차세대 플랫폼입니다.",
                    "sec-2": "AI Gateway는 모든 AI 모델 호출의 단일 진입점으로서 인증, Rate Limiting, Persona 주입 및 토큰 예산을 관리합니다.",
                    "sec-2-1": "AI Gateway v3는 5개의 전용 라우트(hd2ai-admin, hd2ai-public, lingo-admin, lingo-public, universe)를 독립적으로 제어합니다.",
                    "sec-3": "AGCore(AGPilot0005)는 LangGraph 기반 독립 에이전트 런타임으로서 도구 선택, 다단계 실행 루프, 컨텍스트 예산 관리를 담당합니다.",
                }
            }
        }

    async def get_outline(self, doc_id: str) -> Dict[str, Any]:
        if doc_id not in self._docs:
            return {"error": True, "message": f"Document '{doc_id}' not found."}
        doc = self._docs[doc_id]
        return {
            "doc_id": doc_id,
            "title": doc["title"],
            "outline": doc["outline"]
        }

    async def read_section(self, doc_id: str, section_id: str) -> Dict[str, Any]:
        if doc_id not in self._docs:
            return {"error": True, "message": f"Document '{doc_id}' not found."}
        doc = self._docs[doc_id]
        if section_id not in doc["sections"]:
            return {"error": True, "message": f"Section '{section_id}' not found in document '{doc_id}'."}
        return {
            "doc_id": doc_id,
            "section_id": section_id,
            "content": doc["sections"][section_id]
        }

document_adapter: DocumentAdapter = MockDocumentAdapter()
