"""Canvas tool manifest and handlers (WO-C08 · WO-C18).

Ten tools in two opt-in surfaces. The frozen eight (WO-C08) are compatible with the existing
Dynamic Tool Registry and the AI Gateway wire contract (function names already use the
``namespace.tool`` shape that the wire layer translates):

    canvas.create_artifact   canvas.read_artifact     canvas.list_artifacts
    canvas.propose_edit      canvas.apply_edit        canvas.list_revisions
    canvas.restore_revision  canvas.export_artifact

WO-C18-02 adds the workspace-aware pair (own opt-in switch, ``AGCORE_CANVAS_WORKSPACE_TOOLS_ENABLED``):

    canvas.save_to_workspace   canvas.load_from_workspace

Every handler:
* takes identity **only** from ``current_execution_scope`` (the verified ``ExecutionScope``),
  never from tool arguments — the JSON schemas are closed (``additionalProperties: false``) and
  do not contain ``tenant_id`` / ``owner_id`` / ``service_id`` / ``role``,
* returns a JSON-serialisable dict; failures come back as ``{"error": True, "code": ...}`` so the
  dispatcher records a failed trace instead of a fake success,
* never executes generated code and never opens a terminal.

Registration is opt-in (``AGCORE_CANVAS_TOOLS_ENABLED=1/true/yes/on``), so importing the
registry never changes today's runtime surface. Exposure additionally requires an operator to add
these names to a tool profile (``app/tools/policy.py``); no shipped profile lists them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.offsets import Selection
from app.canvas.omni import (
    OMNI_TOOL_MANIFEST,
    WORKSPACE_TOOL_NAMES,
    omni_integration_report,
    workspace_tools_enabled,
)

CANVAS_TOOL_NAMES = (
    "canvas.create_artifact",
    "canvas.read_artifact",
    "canvas.list_artifacts",
    "canvas.propose_edit",
    "canvas.apply_edit",
    "canvas.list_revisions",
    "canvas.restore_revision",
    "canvas.export_artifact",
)

# WO-C17 / C-FIX-06: the model never sees the tool that writes a revision from a proposal. Applying
# is a user act; exposing it here would let a tool call stand in for the user's approval.
AI_EXPOSED_TOOL_NAMES = tuple(name for name in CANVAS_TOOL_NAMES if name != "canvas.apply_edit")

CANVAS_TOOL_MANIFEST: List[Dict[str, Any]] = [
    {
        "name": "canvas.create_artifact",
        "description": "대화/요청 결과물을 Canvas Artifact로 명시적으로 생성합니다 (Markdown, 텍스트, 코드, HTML, JSON).",
        "read_only": False,
        "side_effect": "creates an artifact and its first revision",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "description": "Artifact 제목", "maxLength": 200},
                "artifact_type": {
                    "type": "string",
                    "enum": ["markdown", "plain_text", "source_code", "html", "structured_json"],
                },
                "content": {"type": "string", "description": "초기 본문"},
                "language": {"type": "string", "description": "source_code 언어 (예: python, javascript)"},
                "conversation_id": {"type": "string", "description": "연결할 대화 식별자 (선택)"},
                "workspace_id": {"type": "string", "description": "연결할 워크스페이스 식별자 (선택)"},
                "change_reason": {"type": "string", "description": "최초 Revision 생성 사유"},
            },
            "required": ["title", "artifact_type", "content"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "revision": {"type": "integer"},
                "artifact": {"type": "object"},
                "duplicate": {"type": "boolean"},
            },
        },
    },
    {
        "name": "canvas.read_artifact",
        "description": "Artifact 메타데이터와 특정 Revision(기본: 현재) 본문을 조회합니다.",
        "read_only": True,
        "side_effect": None,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "artifact_id": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
            },
            "required": ["artifact_id"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "artifact": {"type": "object"},
                "revision": {"type": "object"},
                "content": {"type": "string"},
                "content_metrics": {"type": "object"},
            },
        },
    },
    {
        "name": "canvas.list_artifacts",
        "description": "현재 실행 범위의 Artifact 목록을 조회합니다 (대화/워크스페이스/타입 필터).",
        "read_only": True,
        "side_effect": None,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "conversation_id": {"type": "string"},
                "workspace_id": {"type": "string"},
                "artifact_type": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0},
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {"items": {"type": "array"}, "total": {"type": "integer"}},
        },
    },
    {
        "name": "canvas.propose_edit",
        "description": "선택 영역 또는 문서 전체에 대한 AI 수정 제안을 구조화된 Edit Proposal로 생성합니다 (적용은 별도).",
        "read_only": False,
        "side_effect": "stores a proposal; artifact revision unchanged",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "artifact_id": {"type": "string"},
                "instruction": {"type": "string", "description": "수정 요청 (예: 더 짧게, 문체 변경)"},
                "selection_start": {"type": "integer", "minimum": 0},
                "selection_end": {"type": "integer", "minimum": 0},
                "offset_unit": {"type": "string", "enum": ["utf16", "codepoint"], "default": "utf16"},
                "base_revision": {"type": "integer", "minimum": 1},
                "instruction_kind": {
                    "type": "string",
                    "enum": [
                        "edit_selection", "change_style", "adjust_length", "fix_grammar",
                        "explain_content", "edit_document", "explain_code", "improve_code",
                    ],
                },
                "budget_tokens": {"type": "integer", "minimum": 128, "maximum": 32000},
                "scripted_replacement": {
                    "type": "string",
                    "description": "개발 검증용 고정 치환 텍스트 (Development adapter 전용)",
                },
            },
            "required": ["artifact_id", "instruction"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "proposal": {"type": "object"},
                "event": {"type": "object"},
                "ai_integration_verified": {"type": "boolean"},
            },
        },
    },
    {
        "name": "canvas.apply_edit",
        "description": "사용자가 화면에서 승인한 Edit Proposal을 적용해 신규 Revision을 생성합니다. AI Tool 호출만으로는 승인으로 간주되지 않습니다.",
        "read_only": False,
        "side_effect": "creates a new revision from the proposal",
        "requires_user_approval": True,
        "ai_exposed": False,
        "approval_boundary": "user_action only — a tool call returns 403 user_approval_required",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "proposal_id": {"type": "string"},
                "change_reason": {"type": "string", "maxLength": 500},
            },
            "required": ["proposal_id"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "proposal": {"type": "object"},
                "revision": {"type": "object"},
                "applied": {"type": "boolean"},
            },
        },
    },
    {
        "name": "canvas.list_revisions",
        "description": "Artifact의 Revision 이력을 조회합니다 (최신순, 본문 제외).",
        "read_only": True,
        "side_effect": None,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "artifact_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0},
            },
            "required": ["artifact_id"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"items": {"type": "array"}, "total": {"type": "integer"}},
        },
    },
    {
        "name": "canvas.restore_revision",
        "description": "이전 Revision 내용으로 복원합니다. 기존 이력은 삭제하지 않고 신규 Revision을 생성합니다.",
        "read_only": False,
        "side_effect": "appends a restore revision",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "artifact_id": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
                "change_reason": {"type": "string", "maxLength": 500},
            },
            "required": ["artifact_id", "revision"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"artifact": {"type": "object"}, "revision": {"type": "object"}},
        },
    },
    {
        "name": "canvas.export_artifact",
        "description": "현재 또는 선택한 Revision을 md/txt/html/json/소스 확장자로 Export합니다.",
        "read_only": True,
        "side_effect": None,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "artifact_id": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
                "format": {"type": "string", "enum": ["md", "txt", "html", "json", "source"]},
            },
            "required": ["artifact_id"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "media_type": {"type": "string"},
                "content": {"type": "string"},
                "revision": {"type": "integer"},
            },
        },
    },
]


def _scope_or_error() -> Any:
    from app.core.auth import current_execution_scope

    scope = current_execution_scope.get()
    if scope is None or not getattr(scope, "authenticated", False):
        raise CanvasError(CanvasErrorCode.UNAUTHENTICATED, "no verified execution scope for this tool call")
    return scope


def _error(exc: CanvasError) -> Dict[str, Any]:
    return {
        "error": True,
        "code": exc.code.value,
        "message": exc.message,
        "context": exc.context,
    }


def _service():
    from app.canvas.service import get_canvas_service

    return get_canvas_service()


async def create_artifact(
    title: str,
    artifact_type: str,
    content: str,
    language: Optional[str] = None,
    conversation_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    change_reason: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        from app.core.auth import current_run_conversation_id

        scope = _scope_or_error()
        service = _service()
        resolved_conv_id = conversation_id or current_run_conversation_id.get()
        artifact, revision, duplicate, meta = service.manager.create(
            scope,
            title=title,
            artifact_type=artifact_type,
            content=content,
            language=language,
            conversation_id=resolved_conv_id,
            workspace_id=workspace_id,
            creation_source="tool",
            change_reason=change_reason or "created by canvas.create_artifact",
            actor="ai",
            actor_id=scope.owner_id,
        )
        return {
            "artifact_id": artifact.artifact_id,
            "revision": revision.revision,
            "artifact": artifact.model_dump(),
            "duplicate": duplicate,
            **meta,
        }
    except CanvasError as exc:
        return _error(exc)


async def read_artifact(artifact_id: str, revision: Optional[int] = None) -> Dict[str, Any]:
    try:
        scope = _scope_or_error()
        return _service().manager.get(scope, artifact_id, revision=revision)
    except CanvasError as exc:
        return _error(exc)


async def list_artifacts(
    conversation_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    artifact_type: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    try:
        from app.core.auth import current_run_conversation_id

        scope = _scope_or_error()
        resolved_conv_id = conversation_id or current_run_conversation_id.get()
        page = _service().manager.list(
            scope,
            conversation_id=resolved_conv_id,
            workspace_id=workspace_id,
            artifact_type=artifact_type,
            limit=limit,
            offset=offset,
        )
        return page.model_dump()
    except CanvasError as exc:
        return _error(exc)


async def propose_edit(
    artifact_id: str,
    instruction: str,
    selection_start: Optional[int] = None,
    selection_end: Optional[int] = None,
    offset_unit: str = "utf16",
    base_revision: Optional[int] = None,
    instruction_kind: Optional[str] = None,
    budget_tokens: Optional[int] = None,
    scripted_replacement: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        scope = _scope_or_error()
        selection = None
        if selection_start is not None and selection_end is not None:
            selection = Selection(start=selection_start, end=selection_end, offset_unit=offset_unit)  # type: ignore[arg-type]
        result = _service().proposals.create(
            scope,
            artifact_id,
            instruction=instruction,
            base_revision=base_revision,
            selection=selection,
            instruction_kind=instruction_kind or "edit_selection",
            budget_tokens=budget_tokens,
            scripted_replacement=scripted_replacement,
        )
        result["ai_integration_verified"] = False
        return result
    except CanvasError as exc:
        return _error(exc)


async def apply_edit(proposal_id: str, change_reason: Optional[str] = None) -> Dict[str, Any]:
    """Refused by design (WO-C17 / C-FIX-06).

    A tool call is not a user approval. The handler only proceeds when a verified interactive
    approval is present in the execution context, which the tool dispatcher never sets, so the
    model receives ``403 user_approval_required`` and no revision is written.
    """
    try:
        scope = _scope_or_error()
        from app.canvas.approval import current_user_approval, require_user_approval

        approval = require_user_approval(current_user_approval.get(), proposal_id=proposal_id)
        return _service().proposals.apply(
            scope, proposal_id, change_reason=change_reason, approval=approval,
        )
    except CanvasError as exc:
        return _error(exc)


async def list_revisions(artifact_id: str, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
    try:
        scope = _scope_or_error()
        page = _service().revisions.history(scope, artifact_id, limit=limit, offset=offset)
        return page.model_dump()
    except CanvasError as exc:
        return _error(exc)


async def restore_revision(
    artifact_id: str, revision: int, change_reason: Optional[str] = None
) -> Dict[str, Any]:
    try:
        scope = _scope_or_error()
        outcome = _service().revisions.restore(
            scope, artifact_id, revision, change_reason=change_reason, actor_id=scope.owner_id
        )
        return outcome.model_dump()
    except CanvasError as exc:
        return _error(exc)


async def export_artifact(
    artifact_id: str, revision: Optional[int] = None, format: Optional[str] = None
) -> Dict[str, Any]:
    try:
        scope = _scope_or_error()
        result = _service().manager.export(scope, artifact_id, revision=revision, format=format)
        return {
            "filename": result.filename,
            "media_type": result.media_type,
            "format": result.format,
            "revision": result.revision,
            "content": result.content.decode("utf-8"),
            "size_bytes": len(result.content),
            "warnings": result.warnings,
        }
    except CanvasError as exc:
        return _error(exc)


_HANDLERS = {
    "canvas.create_artifact": create_artifact,
    "canvas.read_artifact": read_artifact,
    "canvas.list_artifacts": list_artifacts,
    "canvas.propose_edit": propose_edit,
    "canvas.apply_edit": apply_edit,
    "canvas.list_revisions": list_revisions,
    "canvas.restore_revision": restore_revision,
    "canvas.export_artifact": export_artifact,
}


# --------------------------------------------------------------------------- workspace tools (WO-C18)


async def save_to_workspace(
    artifact_id: str,
    workspace_id: Optional[str] = None,
    path: Optional[str] = None,
    revision: Optional[int] = None,
    format: Optional[str] = None,
    expected_content_hash: Optional[str] = None,
    create_parents: bool = True,
    overwrite: bool = True,
) -> Dict[str, Any]:
    """Save an artifact into its (or a named) workspace file and record the save state."""
    try:
        scope = _scope_or_error()
        result = _service().manager.save_to_workspace(
            scope,
            artifact_id,
            workspace_id=workspace_id,
            path=path,
            revision=revision,
            format=format,
            expected_content_hash=expected_content_hash,
            create_parents=create_parents,
            overwrite=overwrite,
        )
        # CP-02 #7: Never report server-side tool success when local_browser requires unimplemented client_write
        if result.get("client_write") is not None:
            return _error(
                CanvasError(
                    CanvasErrorCode.PROVIDER_MISMATCH,
                    "local_browser workspace requires client_write completion which is unsupported in server tool runtime",
                    context={
                        "artifact_id": artifact_id,
                        "workspace_id": result.get("workspace_id"),
                        "path": result.get("path"),
                        "save_state": result.get("save_state"),
                        "client_write_required": True,
                    },
                )
            )
        return result
    except CanvasError as exc:
        return _error(exc)


async def load_from_workspace(
    workspace_id: str,
    workspace_file_id: Optional[str] = None,
    path: Optional[str] = None,
    title: Optional[str] = None,
    artifact_type: Optional[str] = None,
    language: Optional[str] = None,
    conversation_id: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Open an existing workspace file as a Canvas editing session (external reference)."""
    try:
        from app.core.auth import current_run_conversation_id

        scope = _scope_or_error()
        resolved_conv_id = conversation_id or current_run_conversation_id.get()
        return _service().manager.load_from_workspace(
            scope,
            workspace_id=workspace_id,
            workspace_file_id=workspace_file_id,
            path=path,
            title=title,
            artifact_type=artifact_type,
            language=language,
            conversation_id=resolved_conv_id,
            force=force,
        )
    except CanvasError as exc:
        return _error(exc)


_WORKSPACE_HANDLERS = {
    "canvas.save_to_workspace": save_to_workspace,
    "canvas.load_from_workspace": load_from_workspace,
}


def install_canvas_tools(tool_registry: Any = None, *, ai_exposed_only: bool = True) -> List[str]:
    """Register the Canvas tools on the Dynamic Tool Registry (idempotent).

    ``ai_exposed_only`` (default) registers the seven model-facing tools and leaves
    ``canvas.apply_edit`` out: the model cannot apply a proposal at all until the R1.3 integration
    provides a verified user-approval channel. Pass ``ai_exposed_only=False`` only for admin
    tooling that supplies a real approval.

    The return value is the **core** surface it registered (WO-C18 keeps it byte-for-byte). The two
    workspace tools are installed in addition, only when both the Canvas tool gate and
    ``AGCORE_CANVAS_WORKSPACE_TOOLS_ENABLED`` are on — see :mod:`app.canvas.omni`.
    """
    if tool_registry is None:
        from app.tools.registry import registry as default_registry

        target = default_registry
    else:
        target = tool_registry
    allowed = set(AI_EXPOSED_TOOL_NAMES) if ai_exposed_only else set(CANVAS_TOOL_NAMES)
    registered: List[str] = []
    for spec in CANVAS_TOOL_MANIFEST:
        name = spec["name"]
        if name not in allowed:
            continue
        target.register(
            name=name,
            description=spec["description"],
            parameters=spec["parameters"],
            handler=_HANDLERS[name],
            is_read_only=bool(spec.get("read_only")),
        )
        registered.append(name)
    if workspace_tools_enabled():
        install_canvas_workspace_tools(target)
    return registered


def install_canvas_workspace_tools(tool_registry: Any = None) -> List[str]:
    """Install ``canvas.save_to_workspace`` / ``canvas.load_from_workspace`` (WO-C18-02).

    Callers other than :func:`install_canvas_tools` must have checked
    :func:`app.canvas.omni.workspace_tools_enabled` themselves: this function only registers.
    """
    if tool_registry is None:
        from app.tools.registry import registry as default_registry

        target = default_registry
    else:
        target = tool_registry
    registered: List[str] = []
    for spec in OMNI_TOOL_MANIFEST:
        name = spec["name"]
        target.register(
            name=name,
            description=spec["description"],
            parameters=spec["parameters"],
            handler=_WORKSPACE_HANDLERS[name],
            is_read_only=bool(spec.get("read_only")),
        )
        registered.append(name)
    return registered


def manifest() -> Dict[str, Any]:
    from app.canvas.approval import approval_policy

    return {
        "version": "r2-canvas/v1",
        "registry": "AGCore Dynamic Tool Registry (app/tools/registry.py)",
        "tools": [
            {**tool, "ai_exposed": tool["name"] in AI_EXPOSED_TOOL_NAMES}
            for tool in CANVAS_TOOL_MANIFEST
        ],
        # WO-C18-02: the workspace-aware surface is reported separately so the frozen eight
        # (and the 90-PASS evidence recorded for them) stay exactly what they were.
        "workspace_tools": [
            {
                **tool,
                "ai_exposed": True,
                "gate": "AGCORE_CANVAS_TOOLS_ENABLED + AGCORE_CANVAS_WORKSPACE_TOOLS_ENABLED",
            }
            for tool in OMNI_TOOL_MANIFEST
        ],
        "tool_names": list(CANVAS_TOOL_NAMES) + list(WORKSPACE_TOOL_NAMES),
        "approval_boundary": {
            **approval_policy(),
            "ai_exposed_tools": list(AI_EXPOSED_TOOL_NAMES),
            "withheld_tools": [name for name in CANVAS_TOOL_NAMES if name not in AI_EXPOSED_TOOL_NAMES],
        },
        "scope": {
            "source": "current_execution_scope (verified ExecutionScope)",
            "client_supplied_identity": False,
            "tenant_owner_service_fields": "not accepted in any tool schema",
        },
        "integration": {
            "runtime_tool_calling": "not connected in R2 (R1.3 integration stage; registration requires AGCORE_CANVAS_TOOLS_ENABLED)",
            "ai_exposed_tools": len(AI_EXPOSED_TOOL_NAMES),
            "workspace_tools_enabled": workspace_tools_enabled(),
            "verification": "dev API / adapter only; no real LLM call was made",
            "exposure": "registered only when AGCORE_CANVAS_TOOLS_ENABLED is on; no shipped tool profile lists them",
        },
        "omni": omni_integration_report(),
    }
