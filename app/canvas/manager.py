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
from typing import Any, Dict, List, Optional

from app.canvas.config import CanvasSettings
from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.export import CompositeExportProvider, ExportResult
from app.canvas.models import (
    Artifact,
    ArtifactPage,
    PatchRequest,
    Revision,
    WorkspaceFileLink,
)
from app.canvas.security import sanitize_title, validate_identifier, reject_scope_override
from app.canvas.storage import CanvasStorage, iso_now, new_artifact_id
from app.canvas.types import ArtifactTypeRegistry
from app.canvas.workspace import (
    WORKSPACE_CONTRACT_REVISION,
    WorkspaceAccess,
    WorkspaceGateway,
    enforce_workspace_state,
    normalise_required,
    read_workspace_file,
    write_workspace_file,
)
from app.core.auth import ConversationWorkspace, current_run_workspace_binding


def scope_ids(scope: Any) -> tuple[str, str, str]:
    return (
        str(getattr(scope, "tenant_id", "") or ""),
        str(getattr(scope, "owner_id", "") or ""),
        str(getattr(scope, "service_id", "") or ""),
    )


def _conversation_binding(scope: Any, conversation_id: Optional[str]) -> Optional[ConversationWorkspace]:
    from app.workspace.binding import find_conversation_workspace

    running = current_run_workspace_binding.get()
    if running is not None and running.conversation_id == conversation_id:
        return running
    return find_conversation_workspace(scope, conversation_id)


def _resolve_workspace(binding: Optional[ConversationWorkspace], workspace_id: Optional[str]) -> Optional[str]:
    if binding is None:
        return workspace_id
    if workspace_id is not None and binding.workspace_id != workspace_id:
        raise CanvasError(CanvasErrorCode.CONVERSATION_WORKSPACE_CONFLICT)
    return binding.workspace_id


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
    # Amendment 01 (r2-w/v1+a01): the level alone is not enough — a closed workspace refuses every
    # read/write/export (409 workspace_closed) and a read-only root refuses writes (403).
    return enforce_workspace_state(access, required=required, operation=operation)


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
    running = current_run_workspace_binding.get()
    if running is not None and (artifact.conversation_id, artifact.workspace_id) != (
        running.conversation_id, running.workspace_id,
    ):
        raise CanvasError(CanvasErrorCode.CONVERSATION_WORKSPACE_CONFLICT)
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


#: Workspace file extension → Canvas artifact type used when a load does not name one (WO-C18-04).
LOAD_ARTIFACT_TYPES: Dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "plain_text",
    ".text": "plain_text",
    ".log": "plain_text",
    ".csv": "plain_text",
    ".html": "html",
    ".htm": "html",
    ".json": "structured_json",
}

#: Workspace file extension → language for a ``source_code`` artifact.
LOAD_SOURCE_LANGUAGES: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sql": "sql",
    ".sh": "bash",
    ".bash": "bash",
    ".ps1": "powershell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".css": "css",
    ".scss": "scss",
    ".xml": "xml",
}


def inferred_artifact_type(path: str, mime_type: Optional[str] = None) -> tuple[str, Optional[str]]:
    """Artifact type (and language) inferred from a workspace file path.

    Canvas edits text only; anything it cannot name becomes ``plain_text`` so the loaded buffer is
    always editable instead of silently failing on an unknown extension.
    """

    suffix = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path.rsplit("/", 1)[-1] else ""
    if suffix in LOAD_SOURCE_LANGUAGES:
        return "source_code", LOAD_SOURCE_LANGUAGES[suffix]
    if suffix in LOAD_ARTIFACT_TYPES:
        return LOAD_ARTIFACT_TYPES[suffix], None
    mime = (mime_type or "").lower()
    if mime.startswith("text/markdown"):
        return "markdown", None
    if "html" in mime:
        return "html", None
    if "json" in mime:
        return "structured_json", None
    return "plain_text", None


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
        _workspace_required_level: str = "write",
    ) -> tuple[Artifact, Revision, bool, Dict[str, Any]]:
        spec = self.types.get(artifact_type)
        normalized_language = self.types.normalize_language(spec.key, language)
        clean_title = sanitize_title(title, max_chars=self.settings.max_title_chars)
        if conversation_id is not None:
            conversation_id = validate_identifier(conversation_id, "conversation_id")
        binding = _conversation_binding(scope, conversation_id)
        workspace_id = _resolve_workspace(binding, workspace_id)
        if workspace_id is not None:
            workspace_id = validate_identifier(workspace_id, "workspace_id")
            # WO-C17 / C-FIX-01 & CP-02 #4: creating directly into a workspace requires write;
            # loading an existing file from a read-only workspace into a Canvas edit buffer requires read.
            assert_workspace_level(
                self.workspace, workspace_id, scope,
                required=_workspace_required_level, operation="artifact.create",
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
        if binding is not None and (stored.conversation_id, stored.workspace_id) != (conversation_id, workspace_id):
            raise CanvasError(CanvasErrorCode.CONVERSATION_WORKSPACE_CONFLICT)
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
        binding = _conversation_binding(scope, conversation_id)
        workspace_id = _resolve_workspace(binding, workspace_id)
        if workspace_id is not None:
            # WO-C17 / C-FIX-01: an explicit workspace filter is a read of that workspace.
            assert_workspace_level(
                self.workspace, workspace_id, scope,
                required="read", operation="artifact.list",
            )

        general = binding is not None and binding.workspace_id is None
        linked = 0 if general else self.storage.count_artifacts(
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
                has_workspace=False if general else None,
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

        original_binding = _conversation_binding(scope, artifact.conversation_id)
        if original_binding is not None and conversation_id not in (None, original_binding.conversation_id):
            raise CanvasError(CanvasErrorCode.CONVERSATION_WORKSPACE_CONFLICT)
        target_binding = original_binding or _conversation_binding(scope, conversation_id)
        if target_binding is not None:
            _resolve_workspace(target_binding, artifact.workspace_id)
            workspace_id = _resolve_workspace(target_binding, workspace_id)

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
        if _conversation_binding(scope, artifact.conversation_id) is not None:
            raise CanvasError(CanvasErrorCode.CONVERSATION_WORKSPACE_CONFLICT)
        if kind == "workspace":
            # WO-C18-04: detaching also drops the save state; the workspace file itself is untouched.
            self.storage.delete_workspace_link(artifact.artifact_id)
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

    def export_to_workspace(
        self,
        scope: Any,
        artifact_id: str,
        *,
        workspace_id: str,
        path: Optional[str] = None,
        revision: Optional[int] = None,
        format: Optional[str] = None,
        expected_content_hash: Optional[str] = None,
        create_parents: bool = True,
        overwrite: bool = True,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write an artifact (or one of its revisions) into a workspace root (Amendment 01 §A1.5).

        The bytes are produced by the same export provider the download endpoint uses, so an
        export-to-workspace file is exactly what the user would have downloaded. The workspace
        engine — not Canvas — performs the write, which keeps the file system, the atomic replace,
        the revision row and the index job in its ownership.

        Failure order is deliberate: the artifact is read first (``read`` on its own workspace, if
        any), then the *target* workspace is asked for ``write`` access, whose Amendment 01 state
        answers ``409 workspace_closed`` / ``403 read_only_workspace`` before any byte is produced.
        """

        artifact, revision_row, content = self.read_content(
            scope, artifact_id, revision, required="read"
        )
        workspace_id = validate_identifier(workspace_id, "workspace_id")
        _resolve_workspace(_conversation_binding(scope, artifact.conversation_id), workspace_id)
        access = assert_workspace_level(
            self.workspace, workspace_id, scope,
            required="write", operation="artifact.export_to_workspace",
        )
        exported = self.exporter.export(
            artifact=artifact, revision=revision_row, content=content, format=format
        )
        target_path = path or exported.filename
        written = write_workspace_file(
            self.workspace,
            scope,
            access,
            path=target_path,
            content=exported.content,
            mime_type=mime_type or exported.media_type,
            expected_content_hash=expected_content_hash,
            create_parents=create_parents,
            overwrite=overwrite,
        )
        return {
            "artifact_id": artifact.artifact_id,
            "artifact_revision": revision_row.revision,
            "artifact_type": artifact.artifact_type,
            "format": exported.format,
            "extension": exported.extension,
            "media_type": exported.media_type,
            "workspace_id": workspace_id,
            "path": written["path"],
            "storage_provider": written["storage_provider"],
            "content_origin": written["content_origin"],
            "client_write": written["client_write"],
            "write_mode": (
                "client_write" if written["client_write"] is not None else "server_write"
            ),
            "server_side_path_access": access.server_side_path_access,
            "workspace_root_path": access.root_display_path or None,
            "workspace_file": written["file"],
            "workspace_file_revision": written["revision"],
            "content_hash": written["content_hash"],
            "written_bytes": written["written_bytes"],
            "export_warnings": exported.warnings,
            "content_metrics": _metrics(content),
            "workspace_access": access.model_dump(mode="json"),
            "contract_revision": WORKSPACE_CONTRACT_REVISION,
        }

    # ------------------------------------------------------------------ workspace save / load (WO-C18-04)

    def workspace_link(self, scope: Any, artifact_id: str) -> Dict[str, Any]:
        """Current workspace save state of an artifact (``workspace_link: null`` when unlinked)."""

        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required="read", operation="artifact.workspace_link",
        )
        link = self.storage.get_workspace_link(artifact.artifact_id)
        return {
            "artifact_id": artifact.artifact_id,
            "workspace_id": artifact.workspace_id,
            "workspace_link": link.model_dump(mode="json") if link else None,
            "contract_revision": WORKSPACE_CONTRACT_REVISION,
        }

    def load_from_workspace(
        self,
        scope: Any,
        *,
        workspace_id: str,
        path: Optional[str] = None,
        workspace_file_id: Optional[str] = None,
        title: Optional[str] = None,
        artifact_type: Optional[str] = None,
        language: Optional[str] = None,
        conversation_id: Optional[str] = None,
        revision: Optional[int] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Open an **existing** workspace file as a Canvas editing session (WO-C18-04 load).

        The workspace file stays the source of truth: Canvas records a :class:`WorkspaceFileLink`
        (workspace file id, relative path, hash) and keeps only an edit buffer (its revisions).
        Re-loading the same file reuses the same artifact instead of making a second copy; the
        buffer is refreshed only when the Canvas side has no unsaved edits (or ``force`` is set).

        ``write`` access is required: the session is workspace-linked (a revoked workspace hides it
        immediately, C-FIX-02) and it is opened to be saved back. A read-only root is therefore
        refused with ``403 read_only_workspace``.
        """

        workspace_id = validate_identifier(workspace_id, "workspace_id")
        binding = _conversation_binding(scope, conversation_id)
        _resolve_workspace(binding, workspace_id)
        # CP-02 #4: Read-only workspace allows loading into a Canvas edit buffer;
        # saving/writing back to the workspace still enforces required="write" (403 read_only_workspace).
        access = assert_workspace_level(
            self.workspace, workspace_id, scope,
            required="read", operation="artifact.load_from_workspace",
        )
        content = read_workspace_file(
            self.workspace, scope, access,
            workspace_file_id=workspace_file_id, path=path, revision=revision,
        )
        source = content.metadata
        tenant_id, owner_id, service_id = scope_ids(scope)
        existing = self.storage.find_workspace_link(
            tenant_id=tenant_id, owner_id=owner_id, service_id=service_id,
            workspace_id=workspace_id, relative_path=source.relative_path,
            conversation_id=binding.conversation_id if binding is not None else None,
        )
        if existing is not None:
            linked_artifact = self.storage.get_artifact(existing.artifact_id)
            if linked_artifact is None:
                self.storage.delete_workspace_link(existing.artifact_id)
                existing = None
            elif (linked_artifact.conversation_id != conversation_id
                  and _conversation_binding(scope, linked_artifact.conversation_id) is not None):
                existing = None

        source_payload = {
            "workspace_id": workspace_id,
            "workspace_file_id": source.workspace_file_id,
            "relative_path": source.relative_path,
            "content_hash": content.content_hash,
            "revision": content.revision,
            "mime_type": source.mime_type,
            "size_bytes": content.size_bytes,
            "storage_provider": access.storage_provider,
            "content_origin": source.content_origin,
        }

        if existing is not None:
            owned_artifact(self.storage, existing.artifact_id, scope)  # scope isolation first
            artifact, revision_row, buffer = self.storage.read_current(existing.artifact_id)
            if buffer == content.text:
                link = self.storage.upsert_workspace_link(
                    self._link_update(
                        existing, workspace_id=workspace_id, source=source,
                        content_hash=content.content_hash, save_state=existing.save_state,
                    )
                )
                return self._load_payload(
                    artifact, revision_row, buffer, link, source_payload,
                    reused=True, refreshed=False,
                )
            unsaved_edits = revision_row.content_hash != existing.last_saved_content_hash
            if unsaved_edits and not force:
                raise CanvasError(
                    CanvasErrorCode.CONTENT_CONFLICT,
                    "Canvas has edits that were not saved to the workspace file; nothing was overwritten",
                    context={
                        "artifact_id": artifact.artifact_id,
                        "workspace_id": workspace_id,
                        "path": source.relative_path,
                        "canvas_content_hash": revision_row.content_hash,
                        "workspace_content_hash": content.content_hash,
                        "recovery": "save to workspace first, or reload with force=true to discard the edits",
                    },
                )
            artifact, revision_row = self._replace_buffer(
                scope, artifact.artifact_id, revision_row.revision, content.text,
                change_reason=f"reloaded from workspace {source.relative_path}",
            )
            link = self.storage.upsert_workspace_link(
                self._link_update(
                    existing, workspace_id=workspace_id, source=source,
                    content_hash=content.content_hash, save_state=existing.save_state,
                )
            )
            return self._load_payload(
                artifact, revision_row, content.text, link, source_payload,
                reused=True, refreshed=True,
            )

        type_key = artifact_type
        inferred_language = language
        if type_key is None:
            type_key, inferred_language = inferred_artifact_type(
                source.relative_path, source.mime_type
            )
        created, revision_row, _duplicate, meta = self.create(
            scope,
            title=title or source.name or source.relative_path,
            artifact_type=type_key,
            content=content.text,
            language=inferred_language,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            creation_source="import",
            change_reason=f"loaded from workspace {source.relative_path}",
            actor="import",
            actor_id=owner_id,
            _workspace_required_level="read",
        )
        link = self.storage.upsert_workspace_link(
            WorkspaceFileLink(
                artifact_id=created.artifact_id,
                workspace_id=workspace_id,
                workspace_file_id=source.workspace_file_id,
                relative_path=source.relative_path,
                tenant_id=tenant_id,
                owner_id=owner_id,
                service_id=service_id,
                source_content_hash=content.content_hash,
                last_saved_content_hash=content.content_hash,
                last_saved_revision=None,
                last_saved_at=None,
                save_state="linked",
                storage_provider=access.storage_provider,
                content_origin=source.content_origin,
                created_at=iso_now(),
                updated_at=iso_now(),
            )
        )
        payload = self._load_payload(
            created, revision_row, content.text, link, source_payload,
            reused=False, refreshed=False,
        )
        payload.update(meta)
        return payload

    def save_to_workspace(
        self,
        scope: Any,
        artifact_id: str,
        *,
        workspace_id: Optional[str] = None,
        path: Optional[str] = None,
        revision: Optional[int] = None,
        format: Optional[str] = None,
        expected_content_hash: Optional[str] = None,
        create_parents: bool = True,
        overwrite: bool = True,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """*Save to Workspace* with link state (WO-C18-04 save semantics).

        Reuses :meth:`export_to_workspace` byte-for-byte (same export provider, same write
        contract), then records ``workspace_file_id`` / ``relative_path`` /
        ``last_saved_content_hash``. A later save to the same file defaults
        ``expected_content_hash`` to that hash, so a file changed by another agent or the user is
        refused with ``409 content_conflict`` rather than overwritten.

        A failed save changes nothing in Canvas: the revisions are read only here, so the user's
        editing content survives every refusal.
        """

        artifact = owned_artifact(self.storage, artifact_id, scope)
        link = self.storage.get_workspace_link(artifact.artifact_id)
        target_workspace = (
            validate_identifier(workspace_id, "workspace_id") if workspace_id else (link.workspace_id if link else None)
        )
        target_workspace = _resolve_workspace(_conversation_binding(scope, artifact.conversation_id), target_workspace)
        if not target_workspace:
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "workspace_id is required for the first save of an artifact",
                context={"artifact_id": artifact.artifact_id},
            )
        same_target = link is not None and link.workspace_id == target_workspace
        target_path = path if path is not None else (link.relative_path if same_target else None)
        defaulted = False
        if (
            expected_content_hash is None
            and same_target
            and link.last_saved_content_hash
            and (path is None or path == link.relative_path)
            and overwrite
        ):
            # Optimistic concurrency: the hash Canvas last reconciled with is what it expects to
            # find. This is what blocks a silent overwrite by another agent or the user.
            expected_content_hash = link.last_saved_content_hash
            defaulted = True

        result = self.export_to_workspace(
            scope,
            artifact_id,
            workspace_id=target_workspace,
            path=target_path,
            revision=revision,
            format=format,
            expected_content_hash=expected_content_hash,
            create_parents=create_parents,
            overwrite=overwrite,
            mime_type=mime_type,
        )

        # A saved artifact is a workspace-linked artifact: the C-FIX-02 re-check must apply to it.
        current_artifact = self.storage.get_artifact(artifact.artifact_id)
        if current_artifact is not None and current_artifact.workspace_id != target_workspace:
            self.link(scope, artifact.artifact_id, workspace_id=target_workspace)

        client_write = result.get("client_write")
        written_file = result.get("workspace_file") or {}
        save_state = "client_write_pending" if client_write else "saved"
        now = iso_now()
        previous_hash = link.last_saved_content_hash if link else None
        previous_revision = link.last_saved_revision if link else None
        previous_saved_at = link.last_saved_at if link else None
        stored = self.storage.upsert_workspace_link(
            WorkspaceFileLink(
                artifact_id=artifact.artifact_id,
                workspace_id=target_workspace,
                workspace_file_id=written_file.get("file_id") or (link.workspace_file_id if link else None),
                relative_path=result["path"],
                tenant_id=artifact.tenant_id,
                owner_id=artifact.owner_id,
                service_id=artifact.service_id,
                source_content_hash=(link.source_content_hash if link else None),
                # A client write is not confirmed by the server, so the previous hash stays in force.
                last_saved_content_hash=previous_hash if client_write else result.get("content_hash"),
                last_saved_revision=previous_revision if client_write else result["artifact_revision"],
                last_saved_at=previous_saved_at if client_write else now,
                save_state=save_state,
                storage_provider=result["storage_provider"],
                content_origin=result["content_origin"],
                created_at=now,
                updated_at=now,
            )
        )
        return {
            **result,
            "save_state": stored.save_state,
            "workspace_link": stored.model_dump(mode="json"),
            "expected_content_hash_used": expected_content_hash,
            "expected_content_hash_defaulted": defaulted,
            "canvas_content_preserved": True,
            "completion_contract": (
                "unverified: r2-w/v1+a01 defines no client write completion report; the browser "
                "must confirm the write and Canvas cannot verify it"
                if client_write
                else "server_write"
            ),
        }

    def _link_update(
        self,
        link: WorkspaceFileLink,
        *,
        workspace_id: str,
        source: Any,
        content_hash: str,
        save_state: str,
    ) -> WorkspaceFileLink:
        """Reload bookkeeping: the buffer now equals the workspace file, so the expected hash follows."""

        return link.model_copy(
            update={
                "workspace_id": workspace_id,
                "workspace_file_id": source.workspace_file_id,
                "relative_path": source.relative_path,
                "source_content_hash": content_hash,
                "last_saved_content_hash": content_hash,
                "save_state": save_state,
                "storage_provider": source.storage_provider,
                "content_origin": source.content_origin,
                "updated_at": iso_now(),
            }
        )

    def _replace_buffer(self, scope: Any, artifact_id: str, base_revision: int, text: str, *, change_reason: str):
        """Append a revision holding the workspace file's content (the edit buffer is replaced)."""

        artifact, revision, _computation = self.storage.apply_patch(
            PatchRequest(
                artifact_id=artifact_id,
                base_revision=base_revision,
                operation="replace_content",
                replacement_text=text,
                change_reason=change_reason,
                actor="import",
            ),
            actor="import",
            actor_id=scope_ids(scope)[1],
        )
        return artifact, revision

    def _load_payload(
        self,
        artifact: Artifact,
        revision: Revision,
        content: str,
        link: WorkspaceFileLink,
        source: Dict[str, Any],
        *,
        reused: bool,
        refreshed: bool,
    ) -> Dict[str, Any]:
        from app.canvas.offsets import content_metrics

        return {
            "artifact": artifact.model_dump(),
            "revision": revision.model_dump(),
            "content": content,
            "content_metrics": content_metrics(content),
            "reused": reused,
            "refreshed": refreshed,
            "source": source,
            "workspace_link": link.model_dump(mode="json"),
            "copy_semantics": "external_reference_with_edit_buffer",
            "contract_revision": WORKSPACE_CONTRACT_REVISION,
        }

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
