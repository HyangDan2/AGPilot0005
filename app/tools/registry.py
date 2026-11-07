"""Tool Registry and Specification Management."""
import inspect
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
from pydantic import BaseModel
from app.schemas.gateway import ToolDefinition, ToolFunctionDefinition
from app.tools.builtin.system import echo, get_datetime
from app.tools.builtin.math import calculate
from app.tools.builtin.document import get_outline, read_section

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

    def get_handler(self, name: str) -> Optional[Callable[..., Any]]:
        return self._handlers.get(name)

    def get_registration(self, name: str) -> Optional[ToolRegistration]:
        return self._tools.get(name)

    def list_tools(self, service: Optional[str] = None) -> List[ToolRegistration]:
        res = []
        for reg in self._tools.values():
            if service and reg.allowed_services and service not in reg.allowed_services:
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

registry = ToolRegistry()
