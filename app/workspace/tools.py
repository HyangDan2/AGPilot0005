"""AGCore extension adapter: workspace retrieval exposed as runtime tools.

The R1.3 tool dispatcher/registry is not modified. This module provides a manifest plus an
adapter that accepts an ExecutionScope, an optional capability profile and a context budget, so
registration after the R1.3 cutover is a few lines in the runtime bootstrap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.workspace.context import ContextAssembler
from app.workspace.contracts import SearchMode, ScopeView
from app.workspace.manager import WorkspaceFailure, WorkspaceManager
from app.workspace.retrieval import WorkspaceRetriever
from app.workspace.textutil import estimate_tokens

TOOL_MANIFEST_VERSION = "r2-w/v1"

TOOL_MANIFEST: Dict[str, Any] = {
    "namespace": "workspace",
    "version": TOOL_MANIFEST_VERSION,
    "description": "Workspace files, document search and citation-backed context for RAG answers.",
    "tools": [
        {
            "name": "workspace_list_files",
            "description": "워크스페이스의 파일과 폴더 목록을 반환합니다. (읽기 전용)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "parent_id": {"type": ["string", "null"]},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": ["workspace_id"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "object"}},
                    "total": {"type": "integer"},
                },
            },
            "side_effects": "none",
        },
        {
            "name": "workspace_search",
            "description": "워크스페이스 문서를 키워드/벡터/하이브리드로 검색합니다. (읽기 전용)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "query": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "mode": {"type": "string", "enum": ["keyword", "vector", "hybrid"], "default": "hybrid"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                    "file_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["workspace_id", "query"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "mode_effective": {"type": "string"},
                    "degraded": {"type": "boolean"},
                    "results": {"type": "array", "items": {"type": "object"}},
                    "total_matches": {"type": "integer"},
                },
            },
            "side_effects": "none",
        },
        {
            "name": "workspace_read_file",
            "description": "워크스페이스 파일의 텍스트 내용을 읽습니다(토큰 예산 내). (읽기 전용)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "file_id": {"type": "string"},
                    "revision": {"type": "integer", "minimum": 1},
                    "max_tokens": {"type": "integer", "minimum": 64, "maximum": 32000},
                },
                "required": ["workspace_id", "file_id"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "revision": {"type": "integer"},
                    "text": {"type": "string"},
                    "truncated": {"type": "boolean"},
                },
            },
            "side_effects": "none",
        },
        {
            "name": "workspace_get_context",
            "description": "검색 결과를 인용(citation)이 포함된 LLM 컨텍스트 블록으로 조립합니다.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "query": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                    "token_budget": {"type": "integer", "minimum": 64, "maximum": 131072},
                    "file_ids": {"type": "array", "items": {"type": "string"}},
                    "search_mode": {"type": "string", "enum": ["keyword", "vector", "hybrid"], "default": "hybrid"},
                },
                "required": ["workspace_id", "query"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "context_text": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "object"}},
                    "used_tokens": {"type": "integer"},
                },
            },
            "side_effects": "none",
        },
    ],
}


@dataclass
class ToolInvocationContext:
    """Values the runtime passes to the adapter without exposing runtime internals."""

    scope: ScopeView
    capability: Optional[Dict[str, Any]] = None
    context_budget: Optional[int] = None
    conversation_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class WorkspaceToolAdapter:
    def __init__(
        self,
        manager: WorkspaceManager,
        retriever: WorkspaceRetriever,
        assembler: ContextAssembler,
    ) -> None:
        self.manager = manager
        self.retriever = retriever
        self.assembler = assembler

    @staticmethod
    def manifest() -> Dict[str, Any]:
        return TOOL_MANIFEST

    @staticmethod
    def tool_names() -> List[str]:
        return [tool["name"] for tool in TOOL_MANIFEST["tools"]]

    def invoke(self, name: str, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        if name not in self.tool_names():
            return {"status": "error", "error": {"code": "unknown_tool", "message": name}}
        allowed = (context.capability or {}).get("allowed_tools")
        if allowed is not None and name not in allowed:
            return {"status": "error", "error": {"code": "capability_denied", "message": name}}
        try:
            handler = getattr(self, f"_{name}")
            result = handler(arguments or {}, context)
            return {"status": "ok", "tool": name, "result": result}
        except WorkspaceFailure as failure:
            return {"status": "error", "tool": name, "error": failure.error.model_dump(mode="json")}
        except (ValueError, TypeError) as exc:
            return {
                "status": "error",
                "tool": name,
                "error": {"code": "invalid_request", "message": str(exc)[:300]},
            }

    # -- tools -------------------------------------------------------------------
    def _workspace_list_files(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        files = self.manager.list_files(
            workspace_id,
            context.scope,
            parent_id=arguments.get("parent_id"),
            recursive=bool(arguments.get("recursive", False)),
        )
        return {
            "workspace_id": workspace_id,
            "files": [item.model_dump(mode="json") for item in files],
            "total": len(files),
        }

    def _workspace_search(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        query = self._require(arguments, "query")
        self.manager.get_workspace(workspace_id, context.scope)
        mode = SearchMode(arguments.get("mode") or SearchMode.HYBRID.value)
        response = self.retriever.search(
            workspace_id,
            query,
            mode=mode,
            top_k=int(arguments.get("top_k") or 8),
            file_ids=arguments.get("file_ids"),
        )
        return response.model_dump(mode="json")

    def _workspace_read_file(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        file_id = self._require(arguments, "file_id")
        max_tokens = int(arguments.get("max_tokens") or min(2000, context.context_budget or 2000))
        content = self.manager.read_file(
            workspace_id,
            file_id,
            context.scope,
            revision=int(arguments["revision"]) if arguments.get("revision") else None,
        )
        if content.text is None:
            return {
                "file_id": file_id,
                "revision": content.revision,
                "name": content.metadata.name,
                "binary": True,
                "text": "",
                "truncated": False,
            }
        text = content.text
        tokens = estimate_tokens(text)
        truncated = tokens > max_tokens
        if truncated:
            ratio = max_tokens / float(tokens)
            text = text[: max(80, int(len(text) * ratio))].rstrip() + " …"
        return {
            "file_id": file_id,
            "revision": content.revision,
            "name": content.metadata.name,
            "mime_type": content.metadata.mime_type,
            "text": text,
            "truncated": truncated,
            "revision_status": content.revision_status.value,
        }

    def _workspace_get_context(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        query = self._require(arguments, "query")
        self.manager.get_workspace(workspace_id, context.scope)
        requested = arguments.get("token_budget")
        budget = int(requested) if requested else None
        if context.context_budget:
            budget = min(budget or context.context_budget, int(context.context_budget))
        response = self.assembler.build(
            workspace_id,
            query,
            top_k=int(arguments.get("top_k") or 8),
            token_budget=budget,
            file_ids=arguments.get("file_ids"),
            mode=SearchMode(arguments.get("search_mode") or SearchMode.HYBRID.value),
        )
        return response.model_dump(mode="json")

    @staticmethod
    def _require(arguments: Dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required")
        return value.strip()
