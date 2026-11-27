"""Omni Agent capability adapter for Canvas (WO-C18-02 / WO-C18-04).

Canvas already registers eight ``canvas.*`` tools on the Dynamic Tool Registry (WO-C08). The Omni
Agent amendment adds the two **workspace-aware** tools, and this module is the Canvas-side adapter
and contract surface for them:

    canvas.save_to_workspace    Artifact (or one revision) → workspace file, with save state
    canvas.load_from_workspace  existing workspace file     → Canvas editing session

Design rules that are *not* negotiable here (they are the reason this is a separate surface):

* **Identity** comes only from ``current_execution_scope``. Neither JSON schema below carries
  ``tenant_id`` / ``owner_id`` / ``service_id`` / ``role`` / workspace access fields.
* **User approval boundary (C-FIX-06) is unchanged** — ``canvas.apply_edit`` stays withheld from the
  model-facing set and a tool call still returns ``403 user_approval_required``. Saving to a
  workspace is an ordinary authorized write (``ExecutionScope`` + workspace ``write`` level), not a
  proposal apply, so it does not need a second approval channel.
* **R1.3 runtime is not modified.** The dispatcher and ``ActionValidator`` already validate every
  tool call before execution; this module only declares what Canvas offers and what an operator has
  to patch. Nothing under ``app/runtime/**`` is imported or changed.
* **Opt-in gates.** The core surface keeps ``AGCORE_CANVAS_TOOLS_ENABLED``. The workspace surface
  additionally needs ``AGCORE_CANVAS_WORKSPACE_TOOLS_ENABLED``: these two tools write into (and read
  from) a folder the user actually owns, so they are never offered just because the artifact tools
  are enabled.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from app.canvas.workspace import (
    WORKSPACE_CONTRACT_REVISION,
    WORKSPACE_CONTRACT_SSOT_SHA,
    contract_switch_point,
    workspace_contract_report,
)

#: Second, narrower opt-in switch for the workspace read/write tools (WO-C18-02).
WORKSPACE_TOOLS_ENV = "AGCORE_CANVAS_WORKSPACE_TOOLS_ENABLED"
CORE_TOOLS_ENV = "AGCORE_CANVAS_TOOLS_ENABLED"
_TRUTHY = ("1", "true", "yes", "on")

OMNI_CONTRACT_VERSION = "r2-canvas/omni-v1"

WORKSPACE_TOOL_NAMES: Tuple[str, ...] = (
    "canvas.save_to_workspace",
    "canvas.load_from_workspace",
)

#: Action classes the Omni Agent (and the tool policy layer) can key policy on.
OMNI_ACTION_CLASSES: Tuple[str, ...] = (
    "canvas_read",
    "canvas_write",
    "canvas_write_user_approved",
    "workspace_read",
    "workspace_write",
)


def _flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


def workspace_tools_enabled() -> bool:
    """The workspace tools need **both** switches: the Canvas tool gate and their own gate."""

    return _flag(CORE_TOOLS_ENV) and _flag(WORKSPACE_TOOLS_ENV)


# --------------------------------------------------------------------------- tool manifest


OMNI_TOOL_MANIFEST: List[Dict[str, Any]] = [
    {
        "name": "canvas.save_to_workspace",
        "description": (
            "Canvas Artifact(또는 선택 Revision)를 Workspace 파일로 저장합니다. 최초 저장 시 "
            "workspace_file_id/relative_path/저장 해시가 연결되고, 이후 저장은 그 해시를 "
            "expected_content_hash로 보내 외부 수정 시 409 content_conflict로 자동 덮어쓰기를 막습니다."
        ),
        "read_only": False,
        "side_effect": "writes an artifact export into a workspace file and records the save state",
        "action_class": "workspace_write",
        "external_side_effect": True,
        "requires_user_approval": False,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "artifact_id": {"type": "string"},
                "workspace_id": {"type": "string", "description": "생략하면 연결된 워크스페이스"},
                "path": {"type": "string", "description": "워크스페이스 상대 경로 (생략 시 연결 경로/내보내기 파일명)"},
                "revision": {"type": "integer", "minimum": 1},
                "format": {"type": "string", "enum": ["md", "txt", "html", "json", "source"]},
                "expected_content_hash": {
                    "type": "string",
                    "description": "생략하면 연결된 마지막 저장 해시를 사용 (낙관적 동시성)",
                },
                "create_parents": {"type": "boolean", "default": True},
                "overwrite": {"type": "boolean", "default": True},
            },
            "required": ["artifact_id"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "path": {"type": "string"},
                "write_mode": {"type": "string"},
                "save_state": {"type": "string"},
                "workspace_link": {"type": "object"},
                "expected_content_hash_used": {"type": "string"},
                "client_write": {"type": "object"},
            },
        },
    },
    {
        "name": "canvas.load_from_workspace",
        "description": (
            "Workspace에 있는 기존 파일을 Canvas 편집 세션으로 엽니다. 원본 파일을 Canvas 저장소에 "
            "영구 복제하지 않고 외부 파일 참조 + 편집 버퍼(Revision)로만 관리합니다."
        ),
        "read_only": False,
        "side_effect": "creates (or reuses) a workspace-linked artifact and its edit buffer revision",
        "action_class": "workspace_read",
        "external_side_effect": False,
        "requires_user_approval": False,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "workspace_id": {"type": "string"},
                "workspace_file_id": {"type": "string", "description": "workspace_file_id 또는 path 중 하나 필요"},
                "path": {"type": "string", "description": "워크스페이스 상대 경로"},
                "title": {"type": "string", "maxLength": 200},
                "artifact_type": {
                    "type": "string",
                    "enum": ["markdown", "plain_text", "source_code", "html", "structured_json"],
                },
                "language": {"type": "string"},
                "conversation_id": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Canvas에 저장하지 않은 편집이 있어도 워크스페이스 최신 내용으로 버퍼를 덮어씁니다",
                },
            },
            "required": ["workspace_id"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "artifact": {"type": "object"},
                "revision": {"type": "object"},
                "reused": {"type": "boolean"},
                "refreshed": {"type": "boolean"},
                "source": {"type": "object"},
                "workspace_link": {"type": "object"},
            },
        },
    },
]


#: One entry per Canvas tool the Omni Agent can be offered, including the frozen eight.
CANVAS_OMNI_TOOL_CONTRACTS: List[Dict[str, Any]] = [
    {
        "name": "canvas.create_artifact",
        "action_class": "canvas_write",
        "ai_exposed": True,
        "requires_user_approval": False,
        "external_side_effect": False,
        "idempotency": "idempotency_key (optional)",
        "gate": CORE_TOOLS_ENV,
    },
    {
        "name": "canvas.read_artifact",
        "action_class": "canvas_read",
        "ai_exposed": True,
        "requires_user_approval": False,
        "external_side_effect": False,
        "idempotency": "read only",
        "gate": CORE_TOOLS_ENV,
    },
    {
        "name": "canvas.list_artifacts",
        "action_class": "canvas_read",
        "ai_exposed": True,
        "requires_user_approval": False,
        "external_side_effect": False,
        "idempotency": "read only",
        "gate": CORE_TOOLS_ENV,
    },
    {
        "name": "canvas.propose_edit",
        "action_class": "canvas_write",
        "ai_exposed": True,
        "requires_user_approval": False,
        "external_side_effect": False,
        "idempotency": "creates one proposal; artifact revision unchanged",
        "gate": CORE_TOOLS_ENV,
    },
    {
        "name": "canvas.apply_edit",
        "action_class": "canvas_write_user_approved",
        "ai_exposed": False,
        "requires_user_approval": True,
        "external_side_effect": False,
        "idempotency": "one revision per proposal (409 when already applied)",
        "gate": CORE_TOOLS_ENV,
        "approval_boundary": "C-FIX-06: a tool call is never an approval; 403 user_approval_required",
    },
    {
        "name": "canvas.list_revisions",
        "action_class": "canvas_read",
        "ai_exposed": True,
        "requires_user_approval": False,
        "external_side_effect": False,
        "idempotency": "read only",
        "gate": CORE_TOOLS_ENV,
    },
    {
        "name": "canvas.restore_revision",
        "action_class": "canvas_write",
        "ai_exposed": True,
        "requires_user_approval": False,
        "external_side_effect": False,
        "idempotency": "appends a restore revision",
        "gate": CORE_TOOLS_ENV,
    },
    {
        "name": "canvas.export_artifact",
        "action_class": "canvas_read",
        "ai_exposed": True,
        "requires_user_approval": False,
        "external_side_effect": False,
        "idempotency": "read only",
        "gate": CORE_TOOLS_ENV,
    },
    {
        "name": "canvas.save_to_workspace",
        "action_class": "workspace_write",
        "ai_exposed": True,
        "requires_user_approval": False,
        "external_side_effect": True,
        "idempotency": "same path + expected_content_hash (409 content_conflict otherwise)",
        "gate": f"{CORE_TOOLS_ENV} + {WORKSPACE_TOOLS_ENV}",
        "requires_workspace_level": "write",
    },
    {
        "name": "canvas.load_from_workspace",
        "action_class": "workspace_read",
        "ai_exposed": True,
        "requires_user_approval": False,
        "external_side_effect": False,
        "idempotency": "one artifact per workspace file (reused on reload)",
        "gate": f"{CORE_TOOLS_ENV} + {WORKSPACE_TOOLS_ENV}",
        "requires_workspace_level": "write",
    },
]

_CONTRACTS_BY_NAME = {contract["name"]: contract for contract in CANVAS_OMNI_TOOL_CONTRACTS}


def omni_tool_contract(name: str) -> Optional[Dict[str, Any]]:
    return dict(_CONTRACTS_BY_NAME[name]) if name in _CONTRACTS_BY_NAME else None


def omni_exposed_tool_names() -> Tuple[str, ...]:
    """Tool names the model may see, given the current switches (per-tool, not manifest-wide)."""

    names = [c["name"] for c in CANVAS_OMNI_TOOL_CONTRACTS if c["ai_exposed"]]
    if not workspace_tools_enabled():
        names = [name for name in names if name not in WORKSPACE_TOOL_NAMES]
    return tuple(names)


# --------------------------------------------------------------------------- adapters / patches


def omni_adapters() -> List[Dict[str, Any]]:
    """The adapter layer that connects Canvas to the Omni Agent (WO-C18-02)."""

    return [
        {
            "name": "CanvasToolAdapter",
            "module": "app.canvas.tools",
            "binds": "8 core canvas.* handlers → app.tools.registry.Dynamic Tool Registry",
            "gate": CORE_TOOLS_ENV,
            "state": "existing (WO-C08), unchanged",
        },
        {
            "name": "CanvasWorkspaceToolAdapter",
            "module": "app.canvas.tools + app.canvas.omni",
            "binds": (
                "canvas.save_to_workspace → ArtifactManager.save_to_workspace; "
                "canvas.load_from_workspace → ArtifactManager.load_from_workspace"
            ),
            "gate": f"{CORE_TOOLS_ENV} + {WORKSPACE_TOOLS_ENV}",
            "state": "new (WO-C18-02)",
        },
        {
            "name": "WorkspaceReadAdapter",
            "module": "app.canvas.workspace::read_workspace_file",
            "binds": "contract get_file_metadata / list_workspace_files / read_file → one text buffer",
            "gate": "workspace provider mode (workspace | dev | unavailable)",
            "state": "new (WO-C18-04), exercised against the dev provider",
        },
        {
            "name": "WorkspaceWriteAdapter",
            "module": "app.canvas.workspace::write_workspace_file",
            "binds": "WorkspaceWriteRequest (path/content/expected_content_hash/…) → provider write_file",
            "gate": "workspace provider mode (workspace | dev | unavailable)",
            "state": "existing (Amendment 01), reused by save_to_workspace",
        },
        {
            "name": "ExecutionScopeAdapter",
            "module": "app.core.auth::current_execution_scope",
            "binds": "tenant/owner/service identity for every handler",
            "gate": "verified caller only",
            "state": "existing, unchanged — no identity field in any tool schema",
        },
        {
            "name": "ApprovalBoundaryAdapter",
            "module": "app.canvas.approval",
            "binds": "C-FIX-06 user approval for apply_edit; withheld from the AI-exposed set",
            "gate": "interactive user approval",
            "state": "existing, unchanged by WO-C18",
        },
    ]


def omni_integration_patches() -> List[Dict[str, Any]]:
    """What an integrator must patch to actually give these tools to the Omni Agent."""

    return [
        {
            "target": "app/runtime/** (LangGraph runner, ToolDispatcher, ActionValidator)",
            "change": "none",
            "why": (
                "the runner asks the registry for definitions and the dispatcher resolves "
                "canvas.* handlers through it; ActionValidator still mints ValidatedAction per "
                "tool_call_id before execution. Canvas adds no runtime import."
            ),
        },
        {
            "target": "app/tools/registry.py",
            "change": "none (opt-in block already present since WO-C08)",
            "why": "install_canvas_tools() is called only when AGCORE_CANVAS_TOOLS_ENABLED is on",
        },
        {
            "target": "app/tools/policy.py",
            "change": "+2 BUILTIN_TOOLS names (canvas.save_to_workspace, canvas.load_from_workspace)",
            "why": (
                "a tool is offered only when its profile lists it; no shipped profile lists any "
                "canvas.* tool, so runtime behaviour is unchanged until an operator adds them"
            ),
        },
        {
            "target": "app/api/canvas.py",
            "change": "+2 routes: POST /artifacts/from-workspace, POST /artifacts/{id}/save-to-workspace",
            "why": "HTTP parity for the same capability (frontend CP-04 / AGPilot0011)",
        },
        {
            "target": "app/schemas/events.py",
            "change": "none",
            "why": "no new event type is introduced by WO-C18",
        },
        {
            "target": "app/workspace/**",
            "change": "none — read-only reference (SSoT 907482c)",
            "why": "Canvas consumes the shared contract and never modifies Workspace/RAG code",
        },
    ]


def omni_integration_report() -> Dict[str, Any]:
    """``/capabilities`` block: contracts, gates, adapters, patches, unverified items."""

    return {
        "contract": {
            "version": OMNI_CONTRACT_VERSION,
            "work_orders": ["WO-C18-02", "WO-C18-04", "WO-C18-05"],
            "workspace_contract": WORKSPACE_CONTRACT_REVISION,
            "workspace_ssot_sha": WORKSPACE_CONTRACT_SSOT_SHA,
        },
        "gates": {
            "core_tools": CORE_TOOLS_ENV,
            "workspace_tools": WORKSPACE_TOOLS_ENV,
            "workspace_tools_require_both": True,
            "core_tools_enabled": _flag(CORE_TOOLS_ENV),
            "workspace_tools_enabled": workspace_tools_enabled(),
            "tool_profiles": "no shipped profile lists a canvas.* tool; an operator must add them",
        },
        "ai_exposed_tools": list(omni_exposed_tool_names()),
        "withheld_tools": [
            contract["name"] for contract in CANVAS_OMNI_TOOL_CONTRACTS if not contract["ai_exposed"]
        ],
        "tool_contracts": [dict(contract) for contract in CANVAS_OMNI_TOOL_CONTRACTS],
        "adapters": omni_adapters(),
        "integration_patches": omni_integration_patches(),
        "action_validator": {
            "module": "app/runtime/policy/validator.py",
            "changed": False,
            "note": "unchanged; Canvas tools are ordinary registry tools and pass the same validation",
        },
        "user_approval": {
            "apply_edit": "403 user_approval_required (C-FIX-06, unchanged)",
            "save_to_workspace": "authorized write (ExecutionScope + workspace write level), no extra approval",
        },
        "workspace_contract": workspace_contract_report(),
        "switch_point": contract_switch_point(),
        "unverified": [
            "live workspace engine (feat/r2-workspace-rag 907482c not merged into this branch)",
            "local_browser read/load: no server-side path access; no client-read contract exists",
            "local_browser save: the client_write directive has no completion report in r2-w/v1+a01",
            "real LLM tool-calling run against these contracts (dev API / dispatcher only)",
        ],
    }


def workspace_save_contract() -> Dict[str, Any]:
    """Save/load semantics as data, so the frontend and the Omni Agent read one definition."""

    return {
        "load": {
            "route": "POST /api/v1/canvas/artifacts/from-workspace",
            "tool": "canvas.load_from_workspace",
            "requires_workspace_level": "write",
            "inputs": ["workspace_id", "workspace_file_id | path", "title?", "artifact_type?"],
            "copy_semantics": "external_reference_with_edit_buffer",
            "reuse": "one artifact per (scope, workspace, relative_path); reloading refreshes the buffer",
            "refused": {
                "closed": "409 workspace_closed",
                "read_only": "403 read_only_workspace",
                "local_browser": "400 provider_mismatch (client_read_required)",
                "non_text": "415 unsupported_artifact_type",
                "unsaved_edits": "409 content_conflict (force=true discards)",
            },
        },
        "save": {
            "routes": [
                "POST /api/v1/canvas/artifacts/{artifact_id}/save-to-workspace",
                "POST /api/v1/canvas/artifacts/{artifact_id}/export-to-workspace (stateless export)",
            ],
            "tool": "canvas.save_to_workspace",
            "requires_workspace_level": "write",
            "links": ["workspace_file_id", "relative_path", "last_saved_content_hash"],
            "expected_content_hash": "defaults to last_saved_content_hash on the same path",
            "conflict": "409 content_conflict, workspace file untouched, Canvas content preserved",
            "client_write": "200 with write_mode=client_write and save_state=client_write_pending",
        },
    }
