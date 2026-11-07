# AGPilot0006 연동을 위한 Tool Adapter 인터페이스 계약 (TOOL_ADAPTER_CONTRACT)

## 1. 개요
본 문서는 AGPilot0005(AGCore 플랫폼)와 향후 구축될 AGPilot0006(확장 도구/도큐먼트/RAG 서비스) 간의 Tool Adapter 연동 규약을 정의합니다.

## 2. 어댑터 설계 원칙
- **Decoupled Architecture**: AGCore의 엔진 코드를 수정하지 않고 신규 도구를 플러그인 형태로 등록할 수 있어야 합니다.
- **Protocol Agnostic**: 내부 Python 함수 호출, 로컬 소켓 RPC, HTTP REST, Model Context Protocol(MCP)을 모두 수용 가능한 표준 인터페이스를 제공합니다.
- **Fail-Safe & Sandboxing**: 도구의 장애가 AGCore 런타임 전체를 중단시키지 않도록 구조화된 오류 반환과 독립 타임아웃을 강제합니다.

## 3. 표준 Tool Adapter 인터페이스

```python
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

class ToolParameterSchema(BaseModel):
    type: str = "object"
    properties: Dict[str, Any]
    required: Optional[List[str]] = None

class ToolMetadata(BaseModel):
    name: str                       # e.g., "agpilot0006.search_kb"
    description: str                # Tool purpose for LLM function calling
    parameters: ToolParameterSchema # OpenAPI/JSON Schema for LLM
    is_read_only: bool = True       # Can execute in parallel
    timeout_seconds: float = 30.0   # Individual execution timeout
    allowed_services: Optional[List[str]] = None

class BaseToolAdapter:
    """AGPilot0006 Tool Adapter Base Interface."""
    
    @property
    def metadata(self) -> ToolMetadata:
        """Returns tool schema definition and policies."""
        raise NotImplementedError

    async def execute(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes the tool logic.
        Must return a dictionary (or JSON serializable object).
        On error, must return {"error": True, "code": "...", "message": "..."}.
        """
        raise NotImplementedError
```

## 4. AGCore 등록 방법
AGPilot0006 어댑터 구현체는 AGCore의 `ToolRegistry`에 다음과 같이 동적으로 등록됩니다:
```python
from app.tools.registry import registry

adapter = AGPilot0006SearchAdapter(...)
registry.register(
    name=adapter.metadata.name,
    description=adapter.metadata.description,
    parameters=adapter.metadata.parameters.model_dump(),
    handler=adapter.execute,
    allowed_services=adapter.metadata.allowed_services,
    is_read_only=adapter.metadata.is_read_only,
)
```

## 5. MCP (Model Context Protocol) 확장 계획
AGPilot0006에서 표준 MCP 서버를 도입할 경우, AGCore의 `ToolDispatcher`에 MCP Client를 연결하여 MCP 도구들을 AGCore의 `ToolRegistry`에 일괄 자동 동기화할 수 있도록 지원합니다.
