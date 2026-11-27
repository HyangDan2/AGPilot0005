"""Artifact Manager (WO-C02, WO-C17).

An artifact is a persistent object with its own lifecycle. It *may* carry a ``conversation_id`` and
a ``workspace_id``, but nothing here reads, writes or deletes chat messages: renaming, editing or
deleting a message (or a whole conversation) never touches an artifact, and deleting an artifact
never touches a conversation. Linking is an explicit operation.

Two authorization boundaries are enforced here (WO-C17):

* **Execution scope** — ``tenant_id``, ``owner_id`` and ``service_id`` must all match the stored
  artifact. A mismatch is reported as ``artifact_not_found`` (404), never ``403``, so a caller
  cannot probe for other users' artifact ids.
* **Current workspace access** — every path that touches a workspace-linked artifact asks the
  Workspace contract *now* (never a cached decision). Reads need ``read``; create/link/rename/
  delete/patch/restore/proposal need ``write``. A revoked workspace therefore makes the artifact
  unreadable and unwritable immediately, and ``list`` drops it from the page.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from app.canvas.config import CanvasSettings
from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.export import CompositeExportProvider, ExportResult
from app.canvas.models import Artifact, ArtifactPage, PatchRequest, Revision
from app.canvas.security import sanitize_title, validate_identifier, reject_scope_override
from app.canvas.storage import CanvasStorage, iso_now, new_artifact_id
from app.canvas.types import ArtifactTypeRegistry
from app.canvas.workspace import WorkspaceAccess, WorkspaceGateway, normalise_required


def scope_ids(scope: Any) -> tuple[str, str, str]:
    return (
        str(getattr(scope, "tenant_id", "") or ""),
        str(getattr(scope, "owner_id", "") or ""),
        str(getattr(scope, "service_id", "") or ""),
    )


def assert_workspace_level(
    workspace: Optional[WorkspaceGateway],
    workspace_id: str,
    scope: Any,
    *,
    required: str = "read",
    operation: str = "",
) -> WorkspaceAccess:
    """Ask the Workspace contract for the *current* level and enforce ``required``.

    ``workspace is None`` means the caller has no gateway: a workspace-linked artifact can never be
    served that way (fail closed, ``workspace_unavailable``), so no code path can accidentally skip
    the check.
    """
    required = normalise_required(required)
    if workspace is None:
        raise CanvasError(
            CanvasErrorCode.WORKSPACE_UNAVAILABLE,
            "workspace access cannot be verified without a workspace gateway",
            retryable=True,
            context={"workspace_id": workspace_id, "required_level": required, "operation": operation},
        )
    access = workspace.check_access(workspace_id, scope)  # provider errors surface as 503
    if not access.satisfies(required):
        raise CanvasError(
            CanvasErrorCode.WORKSPACE_ACCESS_DENIED,
            f"workspace {required} access denied",
            context={
                "workspace_id": workspace_id,
                "level": access.level,
                "required_level": required,
                "provider_mode": access.provider_mode,
                "enforced": access.enforced,
                "operation": operation,
            },
        )
    return access


def owned_artifact(
    storage: CanvasStorage, artifact_id: str, scope: Any, *, include_deleted: bool = False
) -> Artifact:
    """Load an artifact and enforce tenant/owner/service isolation only.

    Used by the detach path (``unlink``), which must stay reachable after a workspace grant was
    revoked — otherwise a revoked artifact could never be detached by its owner.
    """
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


def visible_artifact(
    storage: CanvasStorage,
    artifact_id: str,
    scope: Any,
    *,
    include_deleted: bool = False,
    workspace: Optional[WorkspaceGateway] = None,
    required: str = "read",
    operation: str = "",
) -> Artifact:
    """Load an artifact and enforce tenant/owner/service isolation plus current workspace access."""
    artifact = owned_artifact(storage, artifact_id, scope, include_deleted=include_deleted)
    if artifact.workspace_id:
        assert_workspace_level(
            workspace,
            artifact.workspace_id,
            scope,
            required=required,
            operation=operation or "artifact",
        )
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
            # WO-C17 / C-FIX-01: creating *into* a workspace is a write, not a read.
            assert_workspace_level(
                self.workspace, workspace_id, scope,
                required="write", operation="artifact.create",
            )

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
        offset = max(0, int(offset))
        tenant_id, owner_id, service_id = scope_ids(scope)
        if workspace_id is not None:
            # WO-C17 / C-FIX-01: an explicit workspace filter is a read of that workspace.
            assert_workspace_level(
                self.workspace, workspace_id, scope,
                required="read", operation="artifact.list",
            )

        linked = self.storage.count_artifacts(
            tenant_id=tenant_id,
            owner_id=owner_id,
            service_id=service_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            artifact_type=artifact_type,
            has_workspace=True,
        )
        if linked == 0:
            items, total = self.storage.list_artifacts(
                tenant_id=tenant_id,
                owner_id=owner_id,
                service_id=service_id,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                artifact_type=artifact_type,
                limit=page_limit,
                offset=offset,
            )
            return ArtifactPage(items=items, total=total, limit=page_limit, offset=offset)

        return self._list_with_workspace_filter(
            scope,
            tenant_id=tenant_id,
            owner_id=owner_id,
            service_id=service_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            artifact_type=artifact_type,
            page_limit=page_limit,
            offset=offset,
        )

    def _list_with_workspace_filter(
        self,
        scope: Any,
        *,
        tenant_id: str,
        owner_id: str,
        service_id: str,
        conversation_id: Optional[str],
        workspace_id: Optional[str],
        artifact_type: Optional[str],
        page_limit: int,
        offset: int,
    ) -> ArtifactPage:
        """Scan in scope order, dropping artifacts whose workspace access is gone.

        ``total`` is exact when the scan reached the end of the scope; otherwise it is a lower
        bound and ``total_exact`` says so (the response never claims a count it did not establish).
        """
        collected: List[Artifact] = []
        denied = 0
        unavailable = 0
        scanned = 0
        cursor = 0
        chunk = max(page_limit, 25)
        scan_cap = max(int(self.settings.max_list_scan), page_limit + offset)
        exhausted = False
        while True:
            rows, scope_total = self.storage.list_artifacts(
                tenant_id=tenant_id,
                owner_id=owner_id,
                service_id=service_id,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                artifact_type=artifact_type,
                limit=chunk,
                offset=cursor,
            )
            for row in rows:
                scanned += 1
                state = self._workspace_state(scope, row)
                if state == "ok":
                    collected.append(row)
                elif state == "denied":
                    denied += 1
                else:
                    unavailable += 1
            cursor += len(rows)
            if not rows or cursor >= scope_total:
                exhausted = True
                break
            if len(collected) >= offset + page_limit:
                break
            if scanned >= scan_cap:
                break
        items = collected[offset:offset + page_limit]
        return ArtifactPage(
            items=items,
            total=len(collected),
            limit=page_limit,
            offset=offset,
            total_exact=exhausted,
            excluded_workspace_denied=denied,
            excluded_workspace_unavailable=unavailable,
        )

    def _workspace_state(self, scope: Any, artifact: Artifact) -> str:
        if not artifact.workspace_id:
            return "ok"
        try:
            access = assert_workspace_level(
                self.workspace, artifact.workspace_id, scope,
                required="read", operation="artifact.list",
            )
        except CanvasError as exc:
            if exc.code is CanvasErrorCode.WORKSPACE_ACCESS_DENIED:
                return "denied"
            return "unavailable"
        return "ok" if access.can_read else "denied"

    def get(self, scope: Any, artifact_id: str, *, revision: Optional[int] = None) -> Dict[str, Any]:
        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required="read", operation="artifact.get",
        )
        if revision is None:
            _current_artifact, revision_row, content = self.storage.read_current(artifact_id)
        else:
            revision_row, content = self.storage.read_revision_content(artifact_id, revision)
        pruned_total = self._pruned_total(artifact_id)
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
                "revision_retention": {
                    "policy": "prune_oldest_unprotected",
                    "min_retained_revisions": self.settings.effective_min_retained_revisions,
                    "pruned_total": pruned_total,
                    "protected": "current revision, newest retention window, proposal-referenced, baseline",
                    "editing_blocked_at_limit": False,
                },
            },
        }

    def read_content(
        self, scope: Any, artifact_id: str, revision: Optional[int] = None, *, required: str = "read"
    ) -> tuple[Artifact, Revision, str]:
        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required=required, operation="artifact.read_content",
        )
        if revision is None:
            _current_artifact, revision_row, content = self.storage.read_current(artifact_id)
        else:
            revision_row, content = self.storage.read_revision_content(artifact_id, revision)
        return artifact, revision_row, content

    # ------------------------------------------------------------------ metadata changes

    def rename(self, scope: Any, artifact_id: str, title: str) -> Artifact:
        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required="write", operation="artifact.rename",
        )
        clean_title = sanitize_title(title, max_chars=self.settings.max_title_chars)
        return self.storage.update_title(artifact.artifact_id, clean_title, iso_now())

    def delete(self, scope: Any, artifact_id: str, *, purge: bool = False) -> Dict[str, Any]:
        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required="write", operation="artifact.delete",
        )
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
        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required="write" if workspace_id is not None else "read", operation="artifact.link",
        )
        if workspace_id is None and conversation_id is None:
            raise CanvasError(CanvasErrorCode.INVALID_REQUEST, "workspace_id or conversation_id is required")

        workspace_sync: Optional[Dict[str, Any]] = None
        if workspace_id is not None:
            workspace_id = validate_identifier(workspace_id, "workspace_id")
            # WO-C17 / C-FIX-01: linking is a write on the target workspace.
            access = assert_workspace_level(
                self.workspace, workspace_id, scope, required="write", operation="artifact.link",
            )
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
        """Detach a link. Deliberately exempt from the workspace level check: detaching is the
        documented way out of a workspace whose access was revoked, and it neither reads nor
        changes workspace content."""
        artifact = owned_artifact(self.storage, artifact_id, scope)
        if kind == "workspace":
            return self.storage.set_links(artifact.artifact_id, workspace_id=None)
        if kind == "conversation":
            return self.storage.set_links(artifact.artifact_id, conversation_id=None)
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST, "link kind must be 'workspace' or 'conversation'",
            context={"kind": kind},
        )

    def _assert_workspace_access(self, scope: Any, workspace_id: str, required: str = "write"):
        return assert_workspace_level(
            self.workspace, workspace_id, scope, required=required, operation="artifact.create",
        )

    def _pruned_total(self, artifact_id: str) -> int:
        counter = getattr(self.storage, "count_pruned_revisions", None)
        return int(counter(artifact_id)) if callable(counter) else 0

    # ------------------------------------------------------------------ export / preview

    def export(
        self, scope: Any, artifact_id: str, *, revision: Optional[int] = None, format: Optional[str] = None
    ) -> ExportResult:
        artifact, revision_row, content = self.read_content(
            scope, artifact_id, revision, required="read"
        )
        return self.exporter.export(
            artifact=artifact, revision=revision_row, content=content, format=format
        )

    def preview(self, scope: Any, artifact_id: str, *, revision: Optional[int] = None) -> Dict[str, Any]:
        from app.canvas.security import preview_policy, sanitize_html_for_preview

        artifact, revision_row, content = self.read_content(
            scope, artifact_id, revision, required="read"
        )
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
