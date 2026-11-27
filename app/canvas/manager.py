"""Artifact Manager (WO-C02).

An artifact is a persistent object with its own lifecycle. It *may* carry a ``conversation_id`` and
a ``workspace_id``, but nothing here reads, writes or deletes chat messages: renaming, editing or
deleting a message (or a whole conversation) never touches an artifact, and deleting an artifact
never touches a conversation. Linking is an explicit operation.

Isolation is derived only from the verified ``ExecutionScope``: ``tenant_id``, ``owner_id`` and
``service_id`` must all match the stored artifact. A mismatch is reported as
``artifact_not_found`` (404), never ``403``, so a caller cannot probe for other users' artifact ids.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.canvas.config import CanvasSettings
from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.export import CompositeExportProvider, ExportResult
from app.canvas.models import Artifact, ArtifactPage, PatchRequest, Revision
from app.canvas.security import sanitize_title, validate_identifier, reject_scope_override
from app.canvas.storage import CanvasStorage, iso_now, new_artifact_id
from app.canvas.types import ArtifactTypeRegistry
from app.canvas.workspace import WorkspaceGateway


def scope_ids(scope: Any) -> tuple[str, str, str]:
    return (
        str(getattr(scope, "tenant_id", "") or ""),
        str(getattr(scope, "owner_id", "") or ""),
        str(getattr(scope, "service_id", "") or ""),
    )


def visible_artifact(storage: CanvasStorage, artifact_id: str, scope: Any, *, include_deleted: bool = False) -> Artifact:
    """Load an artifact and enforce tenant/owner/service isolation in one place."""
    validate_identifier(artifact_id, "artifact_id")
    artifact = storage.get_artifact(artifact_id, include_deleted=include_deleted)
    if artifact is None:
        raise CanvasError(CanvasErrorCode.ARTIFACT_NOT_FOUND, context={"artifact_id": artifact_id})
    tenant_id, owner_id, service_id = scope_ids(scope)
    if (
        artifact.tenant_id != tenant_id
        or artifact.owner_id != owner_id
        or artifact.service_id != service_id
    ):
        # Deliberately indistinguishable from "missing" so ids of other users are not confirmed.
        raise CanvasError(CanvasErrorCode.ARTIFACT_NOT_FOUND, context={"artifact_id": artifact_id})
    return artifact


def validate_structured_json(content: str) -> Dict[str, Any]:
    """Validation info for ``structured_json`` artifacts (stored as-is, reported honestly)."""
    try:
        json.loads(content) if content.strip() else None
    except json.JSONDecodeError as exc:
        return {"valid": False, "error": f"{exc.msg} (line {exc.lineno}, column {exc.colno})"}
    return {"valid": True, "error": None}


class ArtifactManager:
    def __init__(
        self,
        storage: CanvasStorage,
        types: ArtifactTypeRegistry,
        settings: CanvasSettings,
        exporter: CompositeExportProvider,
        workspace: WorkspaceGateway,
    ) -> None:
        self.storage = storage
        self.types = types
        self.settings = settings
        self.exporter = exporter
        self.workspace = workspace

    # ------------------------------------------------------------------ create / read

    def create(
        self,
        scope: Any,
        *,
        title: str,
        artifact_type: str,
        content: str,
        language: Optional[str] = None,
        conversation_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        creation_source: str = "user",
        change_reason: Optional[str] = None,
        actor: Optional[str] = None,
        actor_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> tuple[Artifact, Revision, bool, Dict[str, Any]]:
        spec = self.types.get(artifact_type)
        normalized_language = self.types.normalize_language(spec.key, language)
        clean_title = sanitize_title(title, max_chars=self.settings.max_title_chars)
        if conversation_id is not None:
            conversation_id = validate_identifier(conversation_id, "conversation_id")
        if workspace_id is not None:
            workspace_id = validate_identifier(workspace_id, "workspace_id")
            self._assert_workspace_access(scope, workspace_id)

        tenant_id, owner_id, service_id = scope_ids(scope)
        if creation_source not in ("user", "ai", "import", "tool"):
            raise CanvasError(CanvasErrorCode.INVALID_REQUEST, "unsupported creation_source")
        if actor not in ("user", "ai", "import", "system"):
            actor = creation_source if creation_source in ("user", "ai", "import") else "user"

        now = iso_now()
        artifact = Artifact(
            artifact_id=artifact_id or new_artifact_id(),
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            service_id=service_id,
            title=clean_title,
            artifact_type=spec.key,
            language=normalized_language,
            current_revision=1,
            content_hash="",
            created_at=now,
            updated_at=now,
            creation_source=creation_source,  # type: ignore[arg-type]
        )
        revision = Revision(
            artifact_id=artifact.artifact_id,
            revision=1,
            content_hash="",
            size_bytes=len(content.encode("utf-8")),
            actor=actor,  # type: ignore[arg-type]
            change_reason=change_reason or "initial revision",
            operation="create",
            created_at=now,
            created_by=actor_id or owner_id,
        )
        stored, stored_revision, duplicate = self.storage.create_artifact(
            artifact, revision, content, event_key=idempotency_key
        )
        validation = (
            validate_structured_json(content) if spec.key == "structured_json" else None
        )
        from app.canvas.offsets import content_metrics

        meta: Dict[str, Any] = {"content_metrics": content_metrics(content)}
        if validation is not None:
            meta["content_validation"] = validation
        return stored, stored_revision, duplicate, meta

    def list(
        self,
        scope: Any,
        *,
        conversation_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        artifact_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> ArtifactPage:
        if artifact_type is not None:
            self.types.get(artifact_type)
        page_limit = min(int(limit or self.settings.default_page_limit), self.settings.max_list_limit)
        tenant_id, owner_id, service_id = scope_ids(scope)
        items, total = self.storage.list_artifacts(
            tenant_id=tenant_id,
            owner_id=owner_id,
            service_id=service_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            artifact_type=artifact_type,
            limit=page_limit,
            offset=max(0, int(offset)),
        )
        return ArtifactPage(items=items, total=total, limit=page_limit, offset=max(0, int(offset)))

    def get(self, scope: Any, artifact_id: str, *, revision: Optional[int] = None) -> Dict[str, Any]:
        artifact = visible_artifact(self.storage, artifact_id, scope)
        if revision is None:
            _current_artifact, revision_row, content = self.storage.read_current(artifact_id)
        else:
            revision_row, content = self.storage.read_revision_content(artifact_id, revision)
        return {
            "artifact": artifact.model_dump(),
            "revision": revision_row.model_dump(),
            "content": content,
            "content_metrics": _metrics(content),
            "is_current": revision_row.revision == artifact.current_revision,
            "content_validation": (
                validate_structured_json(content) if artifact.artifact_type == "structured_json" else None
            ),
            "limits": {
                "max_content_bytes": self.settings.max_content_bytes,
                "max_revisions": self.settings.max_revisions,
                "revision_usage": self.storage.count_revisions(artifact_id),
            },
        }

    def read_content(self, scope: Any, artifact_id: str, revision: Optional[int] = None) -> tuple[Artifact, Revision, str]:
        artifact = visible_artifact(self.storage, artifact_id, scope)
        if revision is None:
            _current_artifact, revision_row, content = self.storage.read_current(artifact_id)
        else:
            revision_row, content = self.storage.read_revision_content(artifact_id, revision)
        return artifact, revision_row, content

    # ------------------------------------------------------------------ metadata changes

    def rename(self, scope: Any, artifact_id: str, title: str) -> Artifact:
        artifact = visible_artifact(self.storage, artifact_id, scope)
        clean_title = sanitize_title(title, max_chars=self.settings.max_title_chars)
        return self.storage.update_title(artifact.artifact_id, clean_title, iso_now())

    def delete(self, scope: Any, artifact_id: str, *, purge: bool = False) -> Dict[str, Any]:
        artifact = visible_artifact(self.storage, artifact_id, scope)
        if purge:
            self.storage.purge_artifact(artifact.artifact_id)
            return {"artifact_id": artifact.artifact_id, "deleted": True, "purged": True}
        self.storage.mark_deleted(artifact.artifact_id, iso_now())
        return {"artifact_id": artifact.artifact_id, "deleted": True, "purged": False}

    def link(
        self,
        scope: Any,
        artifact_id: str,
        *,
        workspace_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        artifact = visible_artifact(self.storage, artifact_id, scope)
        if workspace_id is None and conversation_id is None:
            raise CanvasError(CanvasErrorCode.INVALID_REQUEST, "workspace_id or conversation_id is required")

        workspace_sync: Optional[Dict[str, Any]] = None
        if workspace_id is not None:
            workspace_id = validate_identifier(workspace_id, "workspace_id")
            access = self._assert_workspace_access(scope, workspace_id)
            artifact = self.storage.set_links(artifact.artifact_id, workspace_id=workspace_id)
            try:
                reference = self.workspace.register_reference(
                    workspace_id, scope, artifact_id=artifact.artifact_id, kind="artifact",
                    metadata={
                        "title": artifact.title,
                        "artifact_type": artifact.artifact_type,
                        "revision": artifact.current_revision,
                    },
                )
                workspace_sync = {"status": "synced", **reference}
            except CanvasError as exc:
                if exc.code not in (
                    CanvasErrorCode.WORKSPACE_UNAVAILABLE,
                    CanvasErrorCode.WORKSPACE_ACCESS_DENIED,
                ):
                    raise
                workspace_sync = {
                    "status": "pending",
                    "code": exc.code.value,
                    "message": "Artifact link saved; workspace reference is not registered yet.",
                }
            artifact = self.storage.get_artifact(artifact.artifact_id) or artifact
            workspace_sync["access"] = access.model_dump()

        if conversation_id is not None:
            conversation_id = validate_identifier(conversation_id, "conversation_id")
            artifact = self.storage.set_links(artifact.artifact_id, conversation_id=conversation_id)

        return {"artifact": artifact.model_dump(), "workspace_sync": workspace_sync}

    def unlink(self, scope: Any, artifact_id: str, kind: str) -> Artifact:
        artifact = visible_artifact(self.storage, artifact_id, scope)
        if kind == "workspace":
            return self.storage.set_links(artifact.artifact_id, workspace_id=None)
        if kind == "conversation":
            return self.storage.set_links(artifact.artifact_id, conversation_id=None)
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST, "link kind must be 'workspace' or 'conversation'",
            context={"kind": kind},
        )

    def _assert_workspace_access(self, scope: Any, workspace_id: str):
        access = self.workspace.check_access(workspace_id, scope)
        if not access.allowed:
            raise CanvasError(
                CanvasErrorCode.WORKSPACE_ACCESS_DENIED,
                "workspace access check did not grant read/write/admin",
                context={
                    "workspace_id": workspace_id,
                    "level": access.level,
                    "provider_mode": access.provider_mode,
                    "enforced": access.enforced,
                },
            )
        return access

    # ------------------------------------------------------------------ export / preview

    def export(
        self, scope: Any, artifact_id: str, *, revision: Optional[int] = None, format: Optional[str] = None
    ) -> ExportResult:
        artifact, revision_row, content = self.read_content(scope, artifact_id, revision)
        return self.exporter.export(
            artifact=artifact, revision=revision_row, content=content, format=format
        )

    def preview(self, scope: Any, artifact_id: str, *, revision: Optional[int] = None) -> Dict[str, Any]:
        from app.canvas.security import preview_policy, sanitize_html_for_preview

        artifact, revision_row, content = self.read_content(scope, artifact_id, revision)
        spec = self.types.get(artifact.artifact_type)
        payload: Dict[str, Any] = {
            "artifact_id": artifact.artifact_id,
            "revision": revision_row.revision,
            "artifact_type": artifact.artifact_type,
            "language": artifact.language,
            "preview_family": spec.preview_family,
            "html_preview": spec.html_preview,
            "content_metrics": _metrics(content),
            "policy": preview_policy(),
        }
        if spec.html_preview:
            sanitized = sanitize_html_for_preview(content)
            payload["sanitized_html"] = sanitized["html"]
            payload["sanitization"] = {
                "removed_tags": sanitized["removed_tags"],
                "blocked_attributes": sanitized["blocked_attributes"],
                "blocked_urls": sanitized["blocked_urls"],
                "changed": sanitized["changed"],
            }
        else:
            payload["text"] = content
        return payload


def _metrics(content: str) -> Dict[str, int]:
    from app.canvas.offsets import content_metrics

    return content_metrics(content)


def preview_patch_on_content(
    artifact: Artifact, content: str, request: PatchRequest, *, max_lines: int
) -> Dict[str, Any]:
    """In-memory preview used by the patch preview endpoint."""
    from app.canvas.diffing import diff_texts
    from app.canvas.patching import compute_patch

    computation = compute_patch(content, request)
    diff = diff_texts(
        content,
        computation.content,
        from_revision=request.base_revision,
        to_revision=None,
        artifact_id=artifact.artifact_id,
        max_lines=max_lines,
    )
    diff["applied_range_utf16"] = computation.applied_range_utf16
    diff["would_change"] = computation.changed
    diff["content_metrics_after"] = _metrics(computation.content)
    return diff
