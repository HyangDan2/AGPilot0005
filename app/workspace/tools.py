"""AGCore extension adapter: workspace files exposed as agent tools (contract r2-w/v1+a02).

The R1.3 tool dispatcher/registry is not modified. This module provides
  * `TOOL_MANIFEST` — the 11 agent tool http contracts of Amendment 02, and
  * `WorkspaceToolAdapter` — a handler per tool that accepts a `ScopeView` (AGCore
    `ExecutionScope`), an optional capability profile and a context budget.

Amendment 02 keeps the Amendment 01 engine untouched: every handler only uses
`WorkspaceManager` / `WorkspaceRetriever` / `ContextAssembler` as they are.

The two v1 tools (`workspace_search`, `workspace_get_context`) stay callable as
deprecated aliases so existing callers keep working; they are not advertised in the
manifest any more (agents use `workspace_retrieve`).
"""
from __future__ import annotations

import difflib
import fnmatch
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.workspace.context import ContextAssembler, detect_injection, sanitize_document_text
from app.workspace.contracts import (
    ContentOrigin,
    FileIdentity,
    FileKind,
    InvalidRelativePath,
    SearchMode,
    ScopeView,
    WorkspaceErrorCode,
    WorkspaceWriteRequest,
    validate_relative_path,
)
from app.workspace.manager import WorkspaceFailure, WorkspaceManager
from app.workspace.retrieval import WorkspaceRetriever
from app.workspace.textutil import estimate_tokens

TOOL_MANIFEST_VERSION = "r2-w/v1+a02"

READ_TOOLS = (
    "workspace_list_files",
    "workspace_find_files",
    "workspace_get_file_info",
    "workspace_read_file",
    "workspace_search_text",
    "workspace_retrieve",
)
WRITE_TOOLS = (
    "workspace_create_file",
    "workspace_patch_file",
    "workspace_move_file",
    "workspace_delete_file",
    "workspace_create_directory",
)
#: Tools that are not listed in the manifest but remain callable for v1 callers.
LEGACY_TOOL_ALIASES: Dict[str, str] = {
    "workspace_search": "workspace_retrieve",
    "workspace_get_context": "workspace_retrieve",
}

_SIDE_EFFECTS = {
    "workspace_create_file": "workspace_write",
    "workspace_patch_file": "workspace_write",
    "workspace_move_file": "workspace_write",
    "workspace_delete_file": "irreversible_workspace_delete",
    "workspace_create_directory": "workspace_write",
}


def _schema(properties: Dict[str, Any], required: Sequence[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_WS = {"type": "string", "description": "워크스페이스 ID"}
_FILE = {"type": "string", "description": "파일 ID (path와 둘 중 하나)"}
_PATH = {"type": "string", "description": "워크스페이스 루트 기준 상대 경로 (예: reports/summary.md)"}

TOOL_MANIFEST: Dict[str, Any] = {
    "namespace": "workspace",
    "version": TOOL_MANIFEST_VERSION,
    "description": "워크스페이스 파일 탐색·읽기·검색·RAG 검색과 파일 생성/수정/이동/삭제.",
    "tools": [
        {
            "name": "workspace_list_files",
            "purpose": "inspect",  # which pipeline stage may use it
            "description": "워크스페이스의 파일/폴더 목록을 반환합니다. 파일 본문은 포함하지 않습니다. (읽기 전용)",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "parent_id": {"type": ["string", "null"], "default": None},
                    "path": {"type": ["string", "null"], "description": "기준 폴더 경로 (선택)"},
                    "recursive": {"type": "boolean", "default": False},
                    "kind": {"type": "string", "enum": ["file", "folder"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 500},
                },
                ["workspace_id"],
            ),
            "output_schema": {"type": "object", "properties": {
                "workspace_id": {"type": "string"},
                "entries": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "truncated": {"type": "boolean"},
            }},
            "side_effects": "none",
        },
        {
            "name": "workspace_find_files",
            "purpose": "inspect",
            "description": "파일 이름/경로로 파일을 찾습니다(부분 일치·glob). 내용은 읽지 않습니다. (읽기 전용)",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "query": {"type": "string", "description": "이름/경로 부분 문자열 (한국어 허용)"},
                    "glob": {"type": "string", "description": "예: reports/*.md, **/*.txt"},
                    "kind": {"type": "string", "enum": ["file", "folder"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                },
                ["workspace_id"],
            ),
            "output_schema": {"type": "object", "properties": {
                "matches": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            }},
            "side_effects": "none",
        },
        {
            "name": "workspace_get_file_info",
            "purpose": "inspect",
            "description": "파일 메타데이터(경로, 리비전, 해시, 인덱스 상태, 청크 수)를 반환합니다. 본문은 읽지 않습니다.",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "file_id": _FILE,
                    "path": _PATH,
                    "include_revisions": {"type": "boolean", "default": True},
                },
                ["workspace_id"],
            ),
            "output_schema": {"type": "object", "properties": {
                "file": {"type": "object"},
                "revisions": {"type": "array", "items": {"type": "object"}},
                "chunks": {"type": "integer"},
            }},
            "side_effects": "none",
        },
        {
            "name": "workspace_read_file",
            "purpose": "inspect",
            "description": "파일 본문을 토큰 예산 안에서 읽습니다. 리비전을 지정하면 과거 리비전을 읽습니다. (읽기 전용)",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "file_id": _FILE,
                    "path": _PATH,
                    "revision": {"type": "integer", "minimum": 1},
                    "max_tokens": {"type": "integer", "minimum": 64, "maximum": 32000},
                    "offset_chars": {"type": "integer", "minimum": 0, "default": 0},
                },
                ["workspace_id"],
            ),
            "output_schema": {"type": "object", "properties": {
                "file_id": {"type": "string"},
                "path": {"type": "string"},
                "revision": {"type": "integer"},
                "content_hash": {"type": "string"},
                "text": {"type": "string"},
                "truncated": {"type": "boolean"},
            }},
            "side_effects": "none",
        },
        {
            "name": "workspace_search_text",
            "purpose": "search",
            "description": "정확한 문자열을 파일 본문에서 찾습니다(줄 번호 포함). 인덱스가 없어도 동작합니다. (읽기 전용)",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "query": {"type": "string", "minLength": 1, "maxLength": 400},
                    "file_ids": {"type": "array", "items": {"type": "string"}},
                    "case_sensitive": {"type": "boolean", "default": False},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                    "max_matches_per_file": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                },
                ["workspace_id", "query"],
            ),
            "output_schema": {"type": "object", "properties": {
                "matches": {"type": "array", "items": {"type": "object"}},
                "files_scanned": {"type": "integer"},
                "truncated_scan": {"type": "boolean"},
            }},
            "side_effects": "none",
        },
        {
            "name": "workspace_retrieve",
            "purpose": "search",
            "description": "의미 기반 검색(vector/hybrid/keyword). 인용(citation)이 포함된 컨텍스트 블록을 함께 반환합니다.",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "query": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "mode": {"type": "string", "enum": ["keyword", "vector", "hybrid"], "default": "hybrid"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                    "file_ids": {"type": "array", "items": {"type": "string"}},
                    "token_budget": {"type": "integer", "minimum": 64, "maximum": 131072},
                    "include_context": {"type": "boolean", "default": True},
                },
                ["workspace_id", "query"],
            ),
            "output_schema": {"type": "object", "properties": {
                "mode_effective": {"type": "string"},
                "degraded": {"type": "boolean"},
                "results": {"type": "array", "items": {"type": "object"}},
                "citations": {"type": "array", "items": {"type": "object"}},
                "context_text": {"type": "string"},
            }},
            "side_effects": "none",
        },
        {
            "name": "workspace_create_file",
            "purpose": "write",
            "description": "새 파일을 만듭니다. 기존 파일 덮어쓰기는 사용자 승인(overwrite+confirm_overwrite)이 필요합니다.",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "path": _PATH,
                    "content": {"type": "string"},
                    "mime_type": {"type": ["string", "null"]},
                    "create_parents": {"type": "boolean", "default": True},
                    "overwrite": {"type": "boolean", "default": False},
                    "confirm_overwrite": {"type": "boolean", "default": False},
                },
                ["workspace_id", "path", "content"],
            ),
            "output_schema": {"type": "object", "properties": {
                "file": {"type": "object"},
                "revision": {"type": "integer"},
                "content_hash": {"type": "string"},
                "created": {"type": "boolean"},
            }},
            "side_effects": "workspace_write",
        },
        {
            "name": "workspace_patch_file",
            "purpose": "write",
            "description": "기존 파일의 일부만 수정합니다(문자열 치환 또는 섹션 교체). 이전/새 리비전과 diff를 반환합니다.",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "file_id": _FILE,
                    "path": _PATH,
                    "expected_revision": {"type": "integer", "minimum": 1},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string"},
                                "replace": {"type": "string"},
                                "all": {"type": "boolean", "default": False},
                            },
                            "required": ["find", "replace"],
                            "additionalProperties": False,
                        },
                    },
                    "section": {"type": "string", "description": "교체할 마크다운 섹션 제목(부분 일치)"},
                    "content": {"type": "string", "description": "섹션의 새 본문"},
                    "append_if_missing": {"type": "boolean", "default": False},
                },
                ["workspace_id"],
            ),
            "output_schema": {"type": "object", "properties": {
                "file": {"type": "object"},
                "previous_revision": {"type": "integer"},
                "revision": {"type": "integer"},
                "diff": {"type": "string"},
                "replacements": {"type": "integer"},
            }},
            "side_effects": "workspace_write",
        },
        {
            "name": "workspace_move_file",
            "purpose": "write",
            "description": "파일/폴더의 이름을 바꾸거나 다른 폴더로 옮깁니다. 대상에 파일이 있으면 승인이 필요합니다.",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "file_id": _FILE,
                    "path": _PATH,
                    "new_path": {"type": "string"},
                    "expected_revision": {"type": "integer", "minimum": 1},
                    "confirm_overwrite": {"type": "boolean", "default": False},
                },
                ["workspace_id", "new_path"],
            ),
            "output_schema": {"type": "object", "properties": {
                "file": {"type": "object"},
                "from_path": {"type": "string"},
                "to_path": {"type": "string"},
            }},
            "side_effects": "workspace_write",
        },
        {
            "name": "workspace_delete_file",
            "purpose": "write",
            "description": "파일을 삭제합니다. 비가역 작업이므로 confirm=true(사용자 승인) 없이는 실행하지 않습니다.",
            "input_schema": _schema(
                {
                    "workspace_id": _WS,
                    "file_id": _FILE,
                    "path": _PATH,
                    "confirm": {"type": "boolean", "default": False},
                },
                ["workspace_id"],
            ),
            "output_schema": {"type": "object", "properties": {
                "file_id": {"type": "string"},
                "deleted": {"type": "boolean"},
                "removed_files": {"type": "integer"},
            }},
            "side_effects": "irreversible_workspace_delete",
        },
        {
            "name": "workspace_create_directory",
            "purpose": "write",
            "description": "폴더를 만듭니다(중간 경로 포함).",
            "input_schema": _schema(
                {"workspace_id": _WS, "path": _PATH},
                ["workspace_id", "path"],
            ),
            "output_schema": {"type": "object", "properties": {"folder": {"type": "object"}}},
            "side_effects": "workspace_write",
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


def relative_path_of(identity: FileIdentity) -> str:
    """Workspace-relative path of a file (root-relative, no leading slash)."""

    if identity.relative_path:
        return identity.relative_path
    return (identity.path or "/").lstrip("/")


def resolve_file(
    manager: WorkspaceManager,
    workspace_id: str,
    scope: ScopeView,
    *,
    file_id: Optional[str] = None,
    path: Optional[str] = None,
) -> FileIdentity:
    """Resolve one file by id or by workspace-relative path (single path gate)."""

    if file_id:
        return manager.get_file_metadata(workspace_id, file_id, scope)
    if not path:
        raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "file_id or path is required")
    try:
        rel = validate_relative_path(path)
    except InvalidRelativePath as exc:
        raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, str(exc)) from exc
    rows = manager.list_files(workspace_id, scope, recursive=True)
    lowered = rel.lower()
    for identity in rows:
        if relative_path_of(identity) == rel:
            return identity
    for identity in rows:
        if relative_path_of(identity).lower() == lowered:
            return identity
    raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, path=rel)


def _read_text(
    manager: WorkspaceManager, workspace_id: str, identity: FileIdentity, scope: ScopeView, revision: Optional[int] = None
) -> Tuple[Optional[str], int, str]:
    content = manager.read_file(workspace_id, identity.file_id, scope, revision=revision)
    return content.text, content.revision, content.content_hash


def _heading_level(line: str) -> Tuple[int, str]:
    stripped = line.strip()
    if not stripped.startswith("#"):
        return 0, ""
    level = len(stripped) - len(stripped.lstrip("#"))
    if level > 6 or (len(stripped) > level and stripped[level] != " "):
        return 0, ""
    return level, stripped[level:].strip()


def replace_section(text: str, section: str, content: str, *, append_if_missing: bool = False) -> Tuple[str, bool]:
    """Replace the body of the markdown section whose heading contains `section`."""

    lines = text.splitlines()
    target = section.strip().lower()
    start = -1
    level = 0
    for index, line in enumerate(lines):
        heading_level, title = _heading_level(line)
        if heading_level and title and target in title.lower():
            start = index
            level = heading_level
            break
    if start < 0:
        if not append_if_missing:
            raise WorkspaceFailure(WorkspaceErrorCode.SECTION_NOT_FOUND, section=section)
        suffix = "" if not text or text.endswith("\n") else "\n"
        block = f"{suffix}\n## {section.strip()}\n\n{content.strip()}\n"
        return text + block, False
    end = len(lines)
    for index in range(start + 1, len(lines)):
        heading_level, _title = _heading_level(lines[index])
        if heading_level and heading_level <= level:
            end = index
            break
    new_body = content.strip("\n")
    tail = lines[end:]
    prefix = lines[: start + 1]
    block = [""] + new_body.splitlines() + ([""] if tail else [])
    merged = prefix + block + tail
    return "\n".join(merged).rstrip() + "\n", True


def _unified_diff(path: str, before: str, after: str, *, context: int = 3) -> str:
    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=context,
        )
    )
    return "\n".join(diff[:400])


class WorkspaceToolAdapter:
    def __init__(
        self,
        manager: WorkspaceManager,
        retriever: WorkspaceRetriever,
        assembler: ContextAssembler,
        settings: Optional[Any] = None,
    ) -> None:
        self.manager = manager
        self.retriever = retriever
        self.assembler = assembler
        self.settings = settings

    # -- manifest ----------------------------------------------------------------
    @staticmethod
    def manifest() -> Dict[str, Any]:
        return TOOL_MANIFEST

    @staticmethod
    def tool_names() -> List[str]:
        return [tool["name"] for tool in TOOL_MANIFEST["tools"]]

    @staticmethod
    def tool_spec(name: str) -> Optional[Dict[str, Any]]:
        for tool in TOOL_MANIFEST["tools"]:
            if tool["name"] == name:
                return tool
        return None

    def _check_capability(self, name: str, context: ToolInvocationContext) -> None:
        capability = context.capability or {}
        allowed = capability.get("allowed_tools")
        if allowed is not None and name not in allowed:
            raise WorkspaceFailure(WorkspaceErrorCode.CAPABILITY_DENIED, tool=name)
        if capability.get("read_only") and name in WRITE_TOOLS:
            raise WorkspaceFailure(WorkspaceErrorCode.CAPABILITY_DENIED, tool=name, reason="read_only_capability")
        if capability.get("write") is False and name in WRITE_TOOLS:
            raise WorkspaceFailure(WorkspaceErrorCode.CAPABILITY_DENIED, tool=name, reason="write_not_granted")
        if capability.get("delete") is False and name == "workspace_delete_file":
            raise WorkspaceFailure(WorkspaceErrorCode.CAPABILITY_DENIED, tool=name, reason="delete_not_granted")

    def invoke(self, name: str, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        canonical = LEGACY_TOOL_ALIASES.get(name, name)
        if canonical not in self.tool_names() and name not in LEGACY_TOOL_ALIASES:
            return {"status": "error", "error": {"code": "unknown_tool", "message": name}}
        try:
            self._check_capability(canonical, context)
            started = time.monotonic()
            # v1 aliases keep their own handler so old callers see the old payload shape.
            handler = getattr(self, f"_{name if name in LEGACY_TOOL_ALIASES else canonical}")
            result = handler(arguments or {}, context)
            spec = self.tool_spec(canonical) or {}
            return {
                "status": "ok",
                "tool": name,
                "side_effects": spec.get("side_effects", "none"),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "result": result,
            }
        except WorkspaceFailure as failure:
            error = failure.error.model_dump(mode="json")
            if error.get("code") == WorkspaceErrorCode.APPROVAL_REQUIRED.value:
                return {"status": "approval_required", "tool": name, "error": error, "approval": error.get("context", {})}
            return {"status": "error", "tool": name, "error": error}
        except (ValueError, TypeError) as exc:
            return {
                "status": "error",
                "tool": name,
                "error": {"code": "invalid_request", "message": str(exc)[:300]},
            }

    # -- read tools ---------------------------------------------------------------
    def _workspace_list_files(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        limit = max(1, min(int(arguments.get("limit") or 500), 5000))
        parent_id = arguments.get("parent_id")
        if arguments.get("path"):
            parent_id = resolve_file(
                self.manager, workspace_id, context.scope, path=arguments["path"]
            ).file_id
        files = self.manager.list_files(
            workspace_id,
            context.scope,
            parent_id=parent_id,
            recursive=bool(arguments.get("recursive", False)),
        )
        kind = arguments.get("kind")
        if kind:
            files = [item for item in files if item.kind.value == kind]
        total = len(files)
        entries = [self._entry(item) for item in files[:limit]]
        return {
            "workspace_id": workspace_id,
            "entries": entries,
            "files": entries,  # v1 compatibility
            "total": total,
            "truncated": total > limit,
            "folders": sum(1 for item in files if item.kind is FileKind.FOLDER),
        }

    def _workspace_find_files(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        query = (arguments.get("query") or "").strip()
        glob = (arguments.get("glob") or "").strip()
        if not query and not glob:
            raise ValueError("query or glob is required")
        limit = max(1, min(int(arguments.get("limit") or 20), 200))
        kind = arguments.get("kind")
        needle = query.lower()
        scored: List[Tuple[int, str, FileIdentity]] = []
        for identity in self.manager.list_files(workspace_id, context.scope, recursive=True):
            if kind and identity.kind.value != kind:
                continue
            rel = relative_path_of(identity)
            haystack = f"{identity.name} {rel}".lower()
            if glob and not (fnmatch.fnmatch(rel.lower(), glob.lower()) or fnmatch.fnmatch(identity.name.lower(), glob.lower())):
                continue
            if needle:
                if needle not in haystack:
                    continue
                score = 3 if identity.name.lower() == needle else 2 if identity.name.lower().startswith(needle) else 1
            else:
                score = 1
            scored.append((-score, rel, identity))
        scored.sort(key=lambda item: (item[0], item[1]))
        matches = [self._entry(identity) for _score, _rel, identity in scored[:limit]]
        return {"workspace_id": workspace_id, "matches": matches, "total": len(scored), "limited": len(scored) > limit}

    def _workspace_get_file_info(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        identity = resolve_file(
            self.manager,
            workspace_id,
            context.scope,
            file_id=arguments.get("file_id"),
            path=arguments.get("path"),
        )
        result: Dict[str, Any] = {
            "file": self._entry(identity),
            "chunks": self.manager.store.count_chunks_for_file(identity.file_id),
            "revisions": [],
        }
        if arguments.get("include_revisions", True) and identity.kind is FileKind.FILE:
            rows = self.manager.store.list_revisions(identity.file_id)
            result["revisions"] = [
                {
                    "revision": int(row["revision"]),
                    "content_hash": row.get("content_hash"),
                    "size_bytes": int(row.get("size_bytes") or 0),
                    "created_at": row.get("created_at"),
                }
                for row in rows[-10:]
            ]
        return result

    def _workspace_read_file(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        identity = resolve_file(
            self.manager,
            workspace_id,
            context.scope,
            file_id=arguments.get("file_id"),
            path=arguments.get("path"),
        )
        max_tokens = int(arguments.get("max_tokens") or min(2000, context.context_budget or 2000))
        if context.context_budget:
            max_tokens = min(max_tokens, int(context.context_budget))
        content = self.manager.read_file(
            workspace_id,
            identity.file_id,
            context.scope,
            revision=int(arguments["revision"]) if arguments.get("revision") else None,
        )
        if content.text is None:
            return {
                "file_id": identity.file_id,
                "path": relative_path_of(identity),
                "name": identity.name,
                "revision": content.revision,
                "content_hash": content.content_hash,
                "binary": True,
                "text": "",
                "truncated": False,
            }
        offset = max(0, int(arguments.get("offset_chars") or 0))
        text = content.text[offset:]
        tokens = estimate_tokens(text)
        truncated = tokens > max_tokens
        if truncated:
            ratio = max_tokens / float(tokens)
            text = text[: max(80, int(len(text) * ratio))].rstrip() + " …"
        return {
            "file_id": identity.file_id,
            "path": relative_path_of(identity),
            "name": identity.name,
            "mime_type": identity.mime_type,
            "revision": content.revision,
            "revision_status": content.revision_status.value,
            "content_hash": content.content_hash,
            "text": text,
            "truncated": truncated,
            "total_chars": len(content.text),
            "next_offset_chars": offset + len(text) if truncated else None,
            "injection_suspected": detect_injection(content.text),
        }

    def _workspace_search_text(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        query = self._require(arguments, "query")
        case_sensitive = bool(arguments.get("case_sensitive", False))
        needle = query if case_sensitive else query.lower()
        max_files = max(1, min(int(arguments.get("max_files") or 200), 2000))
        per_file = max(1, min(int(arguments.get("max_matches_per_file") or 5), 50))
        file_ids = set(arguments.get("file_ids") or [])
        matches: List[Dict[str, Any]] = []
        scanned = 0
        truncated_scan = False
        for identity in self.manager.list_files(workspace_id, context.scope, recursive=True):
            if identity.kind is not FileKind.FILE:
                continue
            if file_ids and identity.file_id not in file_ids:
                continue
            if scanned >= max_files:
                truncated_scan = True
                break
            scanned += 1
            try:
                text, revision, content_hash = _read_text(self.manager, workspace_id, identity, context.scope)
            except WorkspaceFailure:
                continue
            if not text:
                continue
            haystack = text if case_sensitive else text.lower()
            if needle not in haystack:
                continue
            hits: List[Dict[str, Any]] = []
            start = 0
            total = 0
            while True:
                index = haystack.find(needle, start)
                if index < 0:
                    break
                total += 1
                if len(hits) < per_file:
                    line_number = haystack.count("\n", 0, index) + 1
                    line_start = haystack.rfind("\n", 0, index) + 1
                    line_end = haystack.find("\n", index)
                    if line_end < 0:
                        line_end = len(haystack)
                    hits.append(
                        {
                            "line": line_number,
                            "text": sanitize_document_text(text[line_start:line_end])[:300],
                        }
                    )
                start = index + max(1, len(needle))
            matches.append(
                {
                    "file_id": identity.file_id,
                    "path": relative_path_of(identity),
                    "relative_path": relative_path_of(identity),
                    "name": identity.name,
                    "revision": revision,
                    "content_hash": content_hash,
                    "occurrences": total,
                    "hits": hits,
                    "injection_suspected": detect_injection(text),
                }
            )
        return {
            "workspace_id": workspace_id,
            "query": query,
            "matches": matches,
            "total_matches": sum(item["occurrences"] for item in matches),
            "files_scanned": scanned,
            "truncated_scan": truncated_scan,
        }

    def _workspace_retrieve(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        query = self._require(arguments, "query")
        self.manager.get_workspace(workspace_id, context.scope)
        mode = SearchMode(arguments.get("mode") or SearchMode.HYBRID.value)
        requested = arguments.get("token_budget")
        budget = int(requested) if requested else None
        if context.context_budget:
            budget = min(budget or int(context.context_budget), int(context.context_budget))
        response = self.assembler.build(
            workspace_id,
            query,
            top_k=int(arguments.get("top_k") or 8),
            token_budget=budget,
            file_ids=arguments.get("file_ids"),
            mode=mode,
        )
        include_context = bool(arguments.get("include_context", True))
        payload = response.model_dump(mode="json")
        payload["mode_effective"] = payload.get("mode_effective") or mode.value
        payload["citations"] = payload.get("sources", [])
        if not include_context:
            payload["context_text"] = ""
        return payload

    # -- write tools --------------------------------------------------------------
    def _workspace_create_file(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        try:
            rel = validate_relative_path(self._require(arguments, "path"))
        except InvalidRelativePath as exc:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, str(exc)) from exc
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        existing = self._find_rel(workspace_id, context.scope, rel)
        if existing is not None and existing.kind is FileKind.FOLDER:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, path=rel, reason="is_a_folder")
        confirmed = bool(arguments.get("overwrite") and arguments.get("confirm_overwrite"))
        if existing is not None and not confirmed:
            raise WorkspaceFailure(
                WorkspaceErrorCode.APPROVAL_REQUIRED,
                path=rel,
                file_id=existing.file_id,
                current_revision=existing.revision,
                action="overwrite",
                required=["overwrite", "confirm_overwrite"],
            )
        request = WorkspaceWriteRequest(
            path=rel,
            content=content,
            mime_type=arguments.get("mime_type"),
            expected_content_hash=existing.content_hash if existing is not None else None,
            create_parents=bool(arguments.get("create_parents", True)),
            overwrite=confirmed,
        )
        result = self.manager.write_file(workspace_id, context.scope, request)
        payload = result.model_dump(mode="json")
        payload["created"] = existing is None
        payload["injection_suspected"] = detect_injection(content)
        payload["file"] = payload.get("file") or {}
        payload["file"]["path"] = "/" + rel
        payload["file"]["relative_path"] = rel
        return payload

    def _workspace_patch_file(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        identity = resolve_file(
            self.manager,
            workspace_id,
            context.scope,
            file_id=arguments.get("file_id"),
            path=arguments.get("path"),
        )
        if identity.kind is not FileKind.FILE:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "folder has no content")
        expected = arguments.get("expected_revision")
        if expected is not None and int(expected) != identity.revision:
            raise WorkspaceFailure(
                WorkspaceErrorCode.REVISION_CONFLICT,
                expected_revision=int(expected),
                current_revision=identity.revision,
            )
        text, previous_revision, previous_hash = _read_text(self.manager, workspace_id, identity, context.scope)
        if text is None:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "binary file cannot be patched")
        updated = text
        replacements = 0
        edits = arguments.get("edits") or []
        if edits:
            for edit in edits:
                find = edit.get("find")
                replace = edit.get("replace")
                if not isinstance(find, str) or not isinstance(replace, str) or not find:
                    raise ValueError("edits need non-empty find/replace")
                occurrences = updated.count(find)
                if occurrences == 0:
                    raise WorkspaceFailure(WorkspaceErrorCode.PATCH_CONFLICT, reason="find_not_found", find=find[:120])
                if occurrences > 1 and not edit.get("all"):
                    raise WorkspaceFailure(
                        WorkspaceErrorCode.PATCH_CONFLICT,
                        reason="ambiguous_find",
                        occurrences=occurrences,
                        find=find[:120],
                    )
                updated = updated.replace(find, replace) if edit.get("all") else updated.replace(find, replace, 1)
                replacements += occurrences if edit.get("all") else 1
        if arguments.get("section") is not None:
            section = self._require(arguments, "section")
            content = arguments.get("content")
            if not isinstance(content, str):
                raise ValueError("content is required with section")
            updated, _found = replace_section(
                updated, section, content, append_if_missing=bool(arguments.get("append_if_missing", False))
            )
            replacements += 1
        if not edits and arguments.get("section") is None:
            raise ValueError("edits or section is required")
        if updated == text:
            return {
                "file": self._entry(identity),
                "previous_revision": previous_revision,
                "revision": previous_revision,
                "diff": "",
                "replacements": 0,
                "no_change": True,
            }
        rel = relative_path_of(identity)
        if identity.content_origin is ContentOrigin.BLOB:
            # Blob (v1) workspaces have no path index for the write contract: reuse the v1
            # create/overwrite path with the revision guard instead.
            updated_identity = self.manager.create_file(
                workspace_id,
                context.scope,
                identity.name,
                updated.encode("utf-8"),
                parent_id=identity.parent_id,
                overwrite=True,
                expected_revision=previous_revision,
            )
            new_revision = updated_identity.revision
            content_hash = updated_identity.content_hash
            entry = self._entry(updated_identity)
        else:
            result = self.manager.write_file(
                workspace_id,
                context.scope,
                WorkspaceWriteRequest(
                    path=rel,
                    content=updated,
                    mime_type=identity.mime_type,
                    expected_content_hash=previous_hash,
                    create_parents=False,
                    overwrite=True,
                ),
            )
            new_revision = result.revision
            content_hash = result.content_hash
            entry = self._entry(result.file) if result.file is not None else self._entry(identity)
        return {
            "file": entry,
            "previous_revision": previous_revision,
            "revision": new_revision,
            "content_hash": content_hash,
            "diff": _unified_diff(rel, text, updated),
            "replacements": replacements,
            "no_change": False,
        }

    def _workspace_move_file(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        identity = resolve_file(
            self.manager,
            workspace_id,
            context.scope,
            file_id=arguments.get("file_id"),
            path=arguments.get("path"),
        )
        try:
            new_rel = validate_relative_path(self._require(arguments, "new_path"))
        except InvalidRelativePath as exc:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, str(exc)) from exc
        from_path = relative_path_of(identity)
        if new_rel == from_path:
            return {"file": self._entry(identity), "from_path": from_path, "to_path": new_rel, "no_change": True}
        existing = self._find_rel(workspace_id, context.scope, new_rel)
        if existing is not None and existing.file_id != identity.file_id:
            if not arguments.get("confirm_overwrite"):
                raise WorkspaceFailure(
                    WorkspaceErrorCode.APPROVAL_REQUIRED,
                    path=new_rel,
                    file_id=existing.file_id,
                    action="move_overwrite",
                    required=["confirm_overwrite"],
                )
            self.manager.delete_file(workspace_id, existing.file_id, context.scope)
        dir_rel, _sep, new_name = new_rel.rpartition("/")
        parent_id: Optional[str] = None
        if dir_rel:
            parent_id = self.manager.create_directory(workspace_id, context.scope, dir_rel).file_id
        expected = arguments.get("expected_revision")
        moved = identity
        if new_name and new_name != identity.name:
            moved = self.manager.rename_file(
                workspace_id,
                identity.file_id,
                context.scope,
                new_name,
                expected_revision=int(expected) if expected is not None else None,
            )
        if parent_id != identity.parent_id:
            moved = self.manager.move_file(workspace_id, identity.file_id, context.scope, parent_id)
        return {"file": self._entry(moved), "from_path": from_path, "to_path": relative_path_of(moved), "no_change": False}

    def _workspace_delete_file(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        identity = resolve_file(
            self.manager,
            workspace_id,
            context.scope,
            file_id=arguments.get("file_id"),
            path=arguments.get("path"),
        )
        if not arguments.get("confirm"):
            raise WorkspaceFailure(
                WorkspaceErrorCode.APPROVAL_REQUIRED,
                path=relative_path_of(identity),
                file_id=identity.file_id,
                kind=identity.kind.value,
                action="delete",
                required=["confirm"],
            )
        result = self.manager.delete_file(workspace_id, identity.file_id, context.scope)
        result["path"] = relative_path_of(identity)
        return result

    def _workspace_create_directory(self, arguments: Dict[str, Any], context: ToolInvocationContext) -> Dict[str, Any]:
        workspace_id = self._require(arguments, "workspace_id")
        folder = self.manager.create_directory(workspace_id, context.scope, self._require(arguments, "path"))
        return {"workspace_id": workspace_id, "folder": self._entry(folder)}

    # -- deprecated v1 aliases ----------------------------------------------------
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

    # -- helpers ------------------------------------------------------------------
    def _find_rel(self, workspace_id: str, scope: ScopeView, rel: str) -> Optional[FileIdentity]:
        for identity in self.manager.list_files(workspace_id, scope, recursive=True):
            if relative_path_of(identity) == rel:
                return identity
        return None

    @staticmethod
    def _entry(identity: FileIdentity) -> Dict[str, Any]:
        return {
            "file_id": identity.file_id,
            "kind": identity.kind.value,
            "name": identity.name,
            "path": identity.path,
            "relative_path": relative_path_of(identity),
            "size_bytes": identity.size_bytes,
            "revision": identity.revision,
            "content_hash": identity.content_hash,
            "index_status": identity.index_status.value if identity.index_status else None,
            "updated_at": identity.updated_at,
            "storage_provider": identity.storage_provider.value,
            "content_origin": identity.content_origin.value,
        }

    @staticmethod
    def _require(arguments: Dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required")
        return value.strip()


WORKSPACE_TOOL_NAMES: Tuple[str, ...] = READ_TOOLS + WRITE_TOOLS


def install_workspace_tools(tool_registry: Any) -> List[str]:
    """CP-03: Register all 11 Workspace Agent Tools into the AGCore ToolRegistry."""
    from app.core.auth import current_execution_scope
    from app.workspace.runtime import get_runtime as _get_workspace_runtime

    installed: List[str] = []
    existing_tools = getattr(tool_registry, "_tools", {})
    for spec in TOOL_MANIFEST["tools"]:
        tool_name = spec["name"]
        if tool_name in existing_tools:
            installed.append(tool_name)
            continue

        def _make_handler(tname: str):
            async def _handler(**kwargs: Any) -> Dict[str, Any]:
                scope = current_execution_scope.get()
                if scope is None or not getattr(scope, "authenticated", False):
                    return {
                        "error": True,
                        "status": "error",
                        "code": "forbidden",
                        "message": "authenticated ExecutionScope is required for workspace tools",
                    }
                rt = _get_workspace_runtime()
                ctx = ToolInvocationContext(scope=scope)
                out = rt.tools.invoke(tname, kwargs, ctx)
                # CP-02 #7: Never report server-side tool success when local_browser requires client_write
                inner = out.get("result") if isinstance(out, dict) else None
                if isinstance(inner, dict) and inner.get("client_write") is not None:
                    return {
                        "error": True,
                        "status": "error",
                        "tool": tname,
                        "code": "provider_mismatch",
                        "message": "local_browser workspace requires client_write completion which is unsupported in server tool runtime",
                        "client_write_required": True,
                        "client_write": inner.get("client_write"),
                    }
                if isinstance(out, dict) and out.get("status") != "ok":
                    return {"error": True, **out}
                return out

            _handler.__name__ = tname
            return _handler

        tool_registry.register(
            name=tool_name,
            description=spec["description"],
            parameters=spec["input_schema"],
            handler=_make_handler(tool_name),
            is_read_only=(tool_name in READ_TOOLS),
        )
        installed.append(tool_name)
    return installed
