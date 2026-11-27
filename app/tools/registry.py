"""Tool Registry and Specification Management."""
import inspect
import os
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
from pydantic import BaseModel
from app.schemas.gateway import ToolDefinition, ToolFunctionDefinition
from app.tools.builtin.system import echo, get_datetime
from app.tools.builtin.math import calculate
from app.tools.builtin.document import get_outline, read_section, search, read_chunk, get_figure_info
from app.tools.policy import tool_policy

#: Opt-in switch for the R2 Canvas artifact tools. Registration (not exposure) is gated so the
#: R1.3 runtime surface stays byte-for-byte unchanged unless an operator enables Canvas.
CANVAS_TOOLS_ENV = "AGCORE_CANVAS_TOOLS_ENABLED"
_TRUTHY = ("1", "true", "yes", "on")


def canvas_tools_enabled() -> bool:
    """Read the opt-in flag at call time (1/true/yes/on, case-insensitive)."""
    return (os.environ.get(CANVAS_TOOLS_ENV) or "").strip().lower() in _TRUTHY


class ToolRegistration(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any]
    is_async: bool = True
    allowed_services: Optional[List[str]] = None
    is_read_only: bool = True  # Can be executed in parallel

class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolRegistration] = {}
        self._handlers: Dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
        self._register_default_tools()

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[..., Any],
        allowed_services: Optional[List[str]] = None,
        is_read_only: bool = True,
    ):
        reg = ToolRegistration(
            name=name,
            description=description,
            parameters=parameters,
            is_async=inspect.iscoroutinefunction(handler),
            allowed_services=allowed_services,
            is_read_only=is_read_only,
        )
        self._tools[name] = reg
        self._handlers[name] = handler

    def unregister(self, name: str):
        self._tools.pop(name, None)
        self._handlers.pop(name, None)

    def _ensure_r2_tools(self) -> None:
        """CP-03: Lazily register R2 Workspace & Canvas tools when their feature flags are ON."""
        from app.config import is_workspace_enabled

        if is_workspace_enabled():
            from app.workspace.tools import install_workspace_tools

            install_workspace_tools(self)
        if canvas_tools_enabled():
            from app.canvas.tools import install_canvas_tools, install_canvas_workspace_tools

            install_canvas_tools(self)
            if is_workspace_enabled():
                install_canvas_workspace_tools(self)

    def get_handler(self, name: str) -> Optional[Callable[..., Any]]:
        if name not in self._handlers:
            self._ensure_r2_tools()
        return self._handlers.get(name)

    def get_registration(self, name: str) -> Optional[ToolRegistration]:
        if name not in self._tools:
            self._ensure_r2_tools()
        return self._tools.get(name)

    def list_tools(self, service: Optional[str] = None) -> List[ToolRegistration]:
        self._ensure_r2_tools()
        res = []
        for reg in self._tools.values():
            if service and reg.allowed_services and service not in reg.allowed_services:
                continue
            allowed, _ = tool_policy.is_tool_allowed(reg.name, service)
            if not allowed:
                continue
            res.append(reg)
        return res

    def get_definitions(self, service: Optional[str] = None) -> List[ToolDefinition]:
        """Returns OpenAI-compatible ToolDefinition list for AI Gateway."""
        tools = self.list_tools(service)
        return [
            ToolDefinition(
                type="function",
                function=ToolFunctionDefinition(
                    name=t.name,
                    description=t.description,
                    parameters=t.parameters,
                ),
            )
            for t in tools
        ]

    def _register_default_tools(self):
        # 1. system.echo
        self.register(
            name="system.echo",
            description="입력받은 메시지를 그대로 반환합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "반환할 메시지"}
                },
                "required": ["message"],
            },
            handler=echo,
            is_read_only=True,
        )

        # 2. system.get_datetime
        self.register(
            name="system.get_datetime",
            description="현재 서버의 날짜, 시각, 요일 정보를 조회합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "타임존 (예: Asia/Seoul, UTC)",
                        "default": "Asia/Seoul",
                    }
                },
            },
            handler=get_datetime,
            is_read_only=True,
        )

        # 3. math.calculate
        self.register(
            name="math.calculate",
            description="사칙연산 및 기본 수학 표현식을 안전하게 계산합니다 (eval 미사용).",
            parameters={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "계산할 수학 식 (예: '25 * 40 + 12', 'sqrt(144) + 10')",
                    }
                },
                "required": ["expression"],
            },
            handler=calculate,
            is_read_only=True,
        )

        # 4. document.get_outline
        self.register(
            name="document.get_outline",
            description="문서의 전체 계층 구조와 섹션 목록을 조회합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "문서 식별자"}
                },
                "required": ["doc_id"],
            },
            handler=get_outline,
            is_read_only=True,
        )

        # 5. document.read_section
        self.register(
            name="document.read_section",
            description="문서 내 특정 섹션의 본문 내용을 조회합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "문서 식별자"},
                    "section_id": {"type": "string", "description": "조회할 섹션 식별자"},
                },
                "required": ["doc_id", "section_id"],
            },
            handler=read_section,
            is_read_only=True,
        )

        # 6. document.search
        self.register(
            name="document.search",
            description="문서 내 텍스트를 키워드 및 의미 기반으로 검색하여 연관 청크와 페이지 위치를 조회합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "문서 식별자"},
                    "query": {"type": "string", "description": "검색할 키워드 또는 질의문"},
                    "limit": {"type": "integer", "description": "최대 반환 결과 개수 (기본 5)", "default": 5},
                },
                "required": ["doc_id", "query"],
            },
            handler=search,
            is_read_only=True,
        )

        # 7. document.read_chunk
        self.register(
            name="document.read_chunk",
            description="문서 내 특정 텍스트 청크의 본문, 토큰 수 및 원본 위치를 조회합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "문서 식별자"},
                    "chunk_id": {"type": "string", "description": "조회할 청크 식별자"},
                },
                "required": ["doc_id", "chunk_id"],
            },
            handler=read_chunk,
            is_read_only=True,
        )

        # 8. document.get_figure_info
        self.register(
            name="document.get_figure_info",
            description="문서 내 그림, 차트, 슬라이드 렌더링 시각 정보의 캡션과 다운로드 URL을 조회합니다.",
            parameters={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "문서 식별자"},
                    "figure_id": {"type": "string", "description": "조회할 그림/차트 식별자"},
                },
                "required": ["doc_id", "figure_id"],
            },
            handler=get_figure_info,
            is_read_only=True,
        )

        # 9-16. R2 Canvas artifact tools — opt-in only (AGCORE_CANVAS_TOOLS_ENABLED=1/true/yes/on).
        # Without the flag the registry is exactly the R1.3 surface; exposure is additionally
        # governed by tool profiles (no existing profile lists canvas.*).
        if canvas_tools_enabled():
            from app.canvas.tools import install_canvas_tools

            install_canvas_tools(self)

registry = ToolRegistry()
