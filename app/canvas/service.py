"""Canvas service container.

Wires storage, type registry, workspace gateway, export provider, AI edit adapter and the
artifact/revision/patch/proposal managers into one object. The REST router and the tool handlers
both resolve the same singleton, which is how a tool call and an HTTP call see the same artifacts.

Feature isolation (WO-C17 / C-FIX-05): the container refuses to build while
``AGCORE_CANVAS_ENABLED`` is off, and the dev preview surface (dev workspace provider, dev edit
adapter, simulated generation stream) is only reachable when ``AGCORE_CANVAS_DEV_PREVIEW`` is on.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.canvas.approval import approval_policy
from app.canvas.config import CanvasSettings, load_canvas_settings
from app.canvas.editing import ProposalService, build_edit_adapter
from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.events import GenerationService, events_contract
from app.canvas.export import provider_for
from app.canvas.manager import ArtifactManager
from app.canvas.revisions import PatchEngine, RevisionManager
from app.canvas.storage import SqliteCanvasStorage
from app.canvas.tools import CANVAS_TOOL_MANIFEST
from app.canvas.types import ArtifactTypeRegistry, default_type_registry
from app.canvas.workspace import build_workspace_gateway, workspace_contract_report

_service: Optional["CanvasService"] = None


class CanvasService:
    def __init__(
        self,
        settings: CanvasSettings,
        *,
        storage: Any = None,
        types: Optional[ArtifactTypeRegistry] = None,
        workspace: Any = None,
        exporter: Any = None,
        edit_adapter: Any = None,
    ) -> None:
        self.settings = settings
        if settings.enabled:
            settings.ensure_dirs()
        self.types = types or default_type_registry
        self.storage = storage or SqliteCanvasStorage(
            settings.db_path,
            settings.blob_root,
            max_content_bytes=settings.max_content_bytes,
            max_revisions=settings.max_revisions,
            min_retained_revisions=settings.effective_min_retained_revisions,
        )
        self.workspace = workspace or build_workspace_gateway(settings)
        self.exporter = exporter or provider_for(self.types)
        self.edit_adapter = edit_adapter or build_edit_adapter(settings)
        self.manager = ArtifactManager(self.storage, self.types, settings, self.exporter, self.workspace)
        self.revisions = RevisionManager(self.storage, settings, self.workspace)
        self.patches = PatchEngine(self.storage, settings, self.workspace)
        self.proposals = ProposalService(
            self.storage, self.manager, self.patches, settings, self.edit_adapter,
            workspace=self.workspace,
        )
        self.generation = GenerationService(self.manager, dev_preview=self.dev_preview)

    @property
    def dev_preview(self) -> bool:
        return bool(self.settings.dev_preview and self.settings.dev_adapters_enabled and not self.settings.production)

    # ------------------------------------------------------------------ introspection

    def capabilities(self) -> Dict[str, Any]:
        return {
            "enabled": self.settings.enabled,
            "environment": self.settings.environment,
            "schema_version": 2,
            "storage": {
                "backend": self.storage.__class__.__name__,
                "database": self.settings.db_path.name,
                "blob_root": self.settings.blob_root.name,
                "independent_dev_database": True,
            },
            "feature_isolation": self.settings.activation(),
            "limits": {
                "max_content_bytes": self.settings.max_content_bytes,
                "max_revisions": self.settings.max_revisions,
                "min_retained_revisions": self.settings.effective_min_retained_revisions,
                "max_title_chars": self.settings.max_title_chars,
                "max_list_limit": self.settings.max_list_limit,
                "max_diff_lines": self.settings.max_diff_lines,
                "max_context_tokens": self.settings.max_context_tokens,
            },
            "revision_retention": {
                "policy": "prune_oldest_unprotected",
                "max_revisions": self.settings.max_revisions,
                "min_retained_revisions": self.settings.effective_min_retained_revisions,
                "editing_blocked_at_limit": False,
            },
            "artifact_types": self.types.keys(),
            "workspace": {
                "provider_mode": getattr(self.workspace, "mode", "unknown"),
                "enforced": bool(getattr(self.workspace, "enforced", False)),
                "read_requires": "read | write | admin",
                "write_requires": "write | admin",
                "contract": workspace_contract_report(),
                "operations": [
                    "check_workspace_access",
                    "register_artifact_reference",
                    "write_file (Amendment 01 / export to workspace)",
                ],
                "lifecycle_enforcement": {
                    "closed": "409 workspace_closed on read / write / export (detach stays allowed)",
                    "deleted": "404 workspace_not_found",
                    "read_only": "403 read_only_workspace on write",
                    "local_browser": "no server-side path access; write answered with client_write",
                },
            },
            "ai_edit": {
                "adapter": getattr(self.edit_adapter, "mode", "unknown"),
                "ai_integration_verified": False,
                "dev_preview": self.dev_preview,
                "note": "No real LLM call is made in R2 backend work; R1.3 runtime integration is pending.",
            },
            "approval": approval_policy(),
            "export": self.exporter.describe(),
            "events": events_contract(),
            "tools": [tool["name"] for tool in CANVAS_TOOL_MANIFEST],
            "security": {
                "html_preview_sandbox": True,
                "scope_fields_from_client": False,
                "generated_code_execution": False,
                "ssrf": "no outbound request is made by Canvas",
            },
        }

    def types_catalogue(self) -> Dict[str, Any]:
        return {
            "artifact_types": self.types.describe(),
            "enabled": self.types.keys(),
            "offset_units": ["utf16", "codepoint"],
            "default_offset_unit": "utf16",
        }


def get_canvas_service(settings: Optional[CanvasSettings] = None, *, reset: bool = False) -> CanvasService:
    global _service
    if reset or _service is None:
        effective = settings or load_canvas_settings()
        if not effective.enabled:
            raise CanvasError(
                CanvasErrorCode.CANVAS_DISABLED,
                "canvas engine is disabled (set AGCORE_CANVAS_ENABLED=true to activate it)",
                context={
                    "feature_flag": "AGCORE_CANVAS_ENABLED",
                    "feature_flag_default": False,
                },
            )
        _service = CanvasService(effective)
    return _service


def reset_canvas_service() -> None:
    global _service
    _service = None
