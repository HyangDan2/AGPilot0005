"""Workspace manager: identity, file lifecycle, access policy and storage orchestration."""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import (
    AccessLevel,
    AccessResult,
    ArtifactReference,
    ArtifactReferenceInput,
    FileContent,
    FileIdentity,
    FileKind,
    IndexState,
    RevisionStatus,
    ScopeView,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceIdentity,
    WorkspaceStatus,
)
from app.workspace.storage import StorageError, WorkspaceStorage
from app.workspace.store import WorkspaceStore

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED_NAMES = {"", ".", ".."}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WorkspaceFailure(Exception):
    """Domain failure carrying a contract error code and HTTP status."""

    def __init__(self, code: WorkspaceErrorCode, message: str = "", **context: Any) -> None:
        self.error = WorkspaceError(code=code, message=message or code.value, context=context)
        super().__init__(self.error.message)

    @property
    def status_code(self) -> int:
        return self.error.http_status


def validate_name(name: str, *, field: str = "name", max_length: int = 255) -> str:
    cleaned = (name or "").strip()
    if cleaned in _RESERVED_NAMES:
        raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, f"invalid {field}")
    if len(cleaned) > max_length:
        raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, f"{field} too long")
    if _CONTROL_CHARS.search(cleaned) or "/" in cleaned or "\\" in cleaned:
        raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, f"invalid characters in {field}")
    if cleaned.startswith(".") and cleaned in {".", ".."}:
        raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, f"invalid {field}")
    return cleaned


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class WorkspaceManager:
    """Implements the shared provider interface plus the file/index facing operations."""

    def __init__(
        self,
        store: WorkspaceStore,
        storage: WorkspaceStorage,
        settings: WorkspaceSettings,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.store = store
        self.storage = storage
        self.settings = settings
        self.clock = clock
        self._file_listeners: List[Callable[[str, Optional[Dict[str, Any]]], None]] = []

    # ------------------------------------------------------------------ helpers
    def add_file_listener(self, listener: Callable[[str, Optional[Dict[str, Any]]], None]) -> None:
        """Register a change sink (the indexer) without a hard dependency on it."""

        self._file_listeners.append(listener)

    def _notify_file(self, workspace_id: str, file_id: str) -> None:
        record = self.store.get_file(workspace_id, file_id)
        for listener in list(self._file_listeners):
            try:
                listener(workspace_id, record)
            except Exception:
                continue

    @staticmethod
    def _scope_ids(scope: ScopeView) -> tuple[str, str, str]:
        tenant = (getattr(scope, "tenant_id", "") or "").strip()
        owner = (getattr(scope, "owner_id", "") or "").strip()
        service = (getattr(scope, "service_id", "") or "").strip()
        if not tenant or not owner or tenant == "unauthenticated":
            raise WorkspaceFailure(WorkspaceErrorCode.FORBIDDEN, "unauthenticated scope")
        return tenant, owner, service

    def _workspace_record(self, workspace_id: str, scope: ScopeView) -> Dict[str, Any]:
        tenant, owner, service = self._scope_ids(scope)
        record = self.store.get_workspace(workspace_id)
        if record is None or record["status"] == WorkspaceStatus.DELETED.value:
            raise WorkspaceFailure(WorkspaceErrorCode.WORKSPACE_NOT_FOUND, workspace_id=workspace_id)
        if record["tenant_id"] != tenant or record["owner_id"] != owner:
            # Cross-tenant/owner reads must look like a missing workspace.
            raise WorkspaceFailure(WorkspaceErrorCode.WORKSPACE_NOT_FOUND, workspace_id=workspace_id)
        if self.settings.service_isolation and record["service_id"] != service:
            raise WorkspaceFailure(WorkspaceErrorCode.WORKSPACE_NOT_FOUND, workspace_id=workspace_id)
        return record

    def workspace_identity(self, record: Dict[str, Any]) -> WorkspaceIdentity:
        return WorkspaceIdentity(
            workspace_id=record["workspace_id"],
            tenant_id=record["tenant_id"],
            owner_id=record["owner_id"],
            service_id=record["service_id"],
            name=record["name"],
            description=record["description"] or "",
            status=WorkspaceStatus(record["status"]),
            revision=int(record["revision"]),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )

    def _display_path(self, workspace_id: str, record: Dict[str, Any]) -> str:
        parts = [record["name"]]
        parent_id = record["parent_id"]
        guard = 0
        while parent_id and guard < 64:
            parent = self.store.get_file(workspace_id, parent_id, include_deleted=True)
            if parent is None:
                break
            parts.append(parent["name"])
            parent_id = parent["parent_id"]
            guard += 1
        return "/" + "/".join(reversed(parts))

    def file_identity(self, record: Dict[str, Any]) -> FileIdentity:
        workspace_id = record["workspace_id"]
        status = record.get("index_status")
        return FileIdentity(
            file_id=record["file_id"],
            workspace_id=workspace_id,
            parent_id=record["parent_id"],
            kind=FileKind(record["kind"]),
            name=record["name"],
            path=self._display_path(workspace_id, record),
            mime_type=record.get("mime_type"),
            size_bytes=int(record.get("size_bytes") or 0),
            content_hash=record.get("content_hash"),
            revision=int(record.get("revision") or 0),
            index_status=IndexState(status) if status else None,
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )

    def _emit(
        self,
        workspace_id: str,
        file_id: Optional[str],
        event_type: str,
        revision: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            self.store.append_event(workspace_id, file_id, event_type, revision, self.clock(), payload)
        except Exception:  # pragma: no cover - event log must never break the operation
            pass

    # ------------------------------------------------------------------ workspace CRUD
    def create_workspace(self, scope: ScopeView, name: str, description: str = "") -> WorkspaceIdentity:
        tenant, owner, service = self._scope_ids(scope)
        clean_name = validate_name(name, field="workspace name", max_length=120)
        if self.store.count_workspaces(tenant, owner) >= self.settings.max_workspaces_per_owner:
            raise WorkspaceFailure(WorkspaceErrorCode.QUOTA_EXCEEDED, "workspace limit reached")
        stamp = self.clock()
        record = {
            "workspace_id": _new_id("ws"),
            "tenant_id": tenant,
            "owner_id": owner,
            "service_id": service,
            "name": clean_name,
            "description": (description or "").strip()[:2000],
            "status": WorkspaceStatus.ACTIVE.value,
            "revision": 1,
            "created_at": stamp,
            "updated_at": stamp,
        }
        self.store.create_workspace(record)
        self.storage.workspace_dir(tenant, record["workspace_id"]).mkdir(parents=True, exist_ok=True)
        self._emit(record["workspace_id"], None, "workspace.created", 1)
        return self.workspace_identity(record)

    def list_workspaces(self, scope: ScopeView) -> List[WorkspaceIdentity]:
        tenant, owner, _service = self._scope_ids(scope)
        service = None
        if self.settings.service_isolation:
            service = (getattr(scope, "service_id", "") or "").strip()
        return [self.workspace_identity(r) for r in self.store.list_workspaces(tenant, owner, service)]

    def get_workspace(self, workspace_id: str, scope: ScopeView) -> WorkspaceIdentity:
        return self.workspace_identity(self._workspace_record(workspace_id, scope))

    def check_workspace_access(self, workspace_id: str, scope: ScopeView) -> AccessResult:
        try:
            self._workspace_record(workspace_id, scope)
        except WorkspaceFailure as failure:
            return AccessResult(workspace_id=workspace_id, level=AccessLevel.NONE, reason=failure.error.code.value)
        return AccessResult(workspace_id=workspace_id, level=AccessLevel.ADMIN, reason="owner")

    def update_workspace(
        self,
        workspace_id: str,
        scope: ScopeView,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> WorkspaceIdentity:
        record = self._workspace_record(workspace_id, scope)
        if expected_revision is not None and int(record["revision"]) != int(expected_revision):
            raise WorkspaceFailure(
                WorkspaceErrorCode.REVISION_CONFLICT,
                expected_revision=expected_revision,
                current_revision=record["revision"],
            )
        fields: Dict[str, Any] = {"updated_at": self.clock(), "revision": int(record["revision"]) + 1}
        if name is not None:
            fields["name"] = validate_name(name, field="workspace name", max_length=120)
        if description is not None:
            fields["description"] = (description or "").strip()[:2000]
        self.store.update_workspace(workspace_id, fields)
        self._emit(workspace_id, None, "workspace.updated", int(fields["revision"]))
        return self.get_workspace(workspace_id, scope)

    def delete_workspace(self, workspace_id: str, scope: ScopeView) -> Dict[str, Any]:
        record = self._workspace_record(workspace_id, scope)
        stamp = self.clock()
        for file_row in self.store.list_files(workspace_id, recursive=True, include_deleted=False):
            self.store.cancel_jobs_for_file(file_row["file_id"], stamp, reason="workspace_deleted")
        purged = self.store.purge_workspace_files(workspace_id)
        self.store.mark_workspace_deleted(workspace_id, stamp)
        removed_blobs = self.storage.delete_workspace(record["tenant_id"], workspace_id)
        self._emit(workspace_id, None, "workspace.deleted", int(record["revision"]))
        return {
            "workspace_id": workspace_id,
            "status": "deleted",
            "purged_files": purged,
            "removed_blobs": removed_blobs,
        }

    # ------------------------------------------------------------------ files
    def _file_record(self, workspace_id: str, file_id: str, scope: ScopeView, include_deleted: bool = False) -> Dict[str, Any]:
        self._workspace_record(workspace_id, scope)
        record = self.store.get_file(workspace_id, file_id, include_deleted=include_deleted)
        if record is None:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=file_id)
        return record

    def _folder_exists(self, workspace_id: str, folder_id: Optional[str]) -> None:
        if folder_id is None:
            return
        folder = self.store.get_file(workspace_id, folder_id)
        if folder is None or folder["kind"] != FileKind.FOLDER.value:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, folder_id=folder_id)

    def list_files(
        self,
        workspace_id: str,
        scope: ScopeView,
        parent_id: Optional[str] = None,
        recursive: bool = False,
        include_deleted: bool = False,
    ) -> List[FileIdentity]:
        self._workspace_record(workspace_id, scope)
        rows = self.store.list_files(workspace_id, parent_id, recursive, include_deleted)
        return [self.file_identity(row) for row in rows]

    list_workspace_files = list_files

    def get_file_metadata(self, workspace_id: str, file_id: str, scope: ScopeView) -> FileIdentity:
        return self.file_identity(self._file_record(workspace_id, file_id, scope))

    def create_folder(self, workspace_id: str, scope: ScopeView, name: str, parent_id: Optional[str] = None) -> FileIdentity:
        self._workspace_record(workspace_id, scope)
        clean = validate_name(name, field="folder name")
        self._folder_exists(workspace_id, parent_id)
        if self.store.find_file_by_name(workspace_id, parent_id, clean) is not None:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=clean)
        if self.store.count_files(workspace_id, kind=FileKind.FOLDER.value) >= self.settings.max_folders_per_workspace:
            raise WorkspaceFailure(WorkspaceErrorCode.QUOTA_EXCEEDED, "folder limit reached")
        stamp = self.clock()
        record = {
            "file_id": _new_id("wf"),
            "workspace_id": workspace_id,
            "parent_id": parent_id,
            "kind": FileKind.FOLDER.value,
            "name": clean,
            "mime_type": None,
            "size_bytes": 0,
            "content_hash": None,
            "revision": 1,
            "index_status": None,
            "index_error": None,
            "index_updated_at": None,
            "created_at": stamp,
            "updated_at": stamp,
            "deleted_at": None,
        }
        self.store.insert_file(record)
        self._emit(workspace_id, record["file_id"], "file.created", 1)
        return self.file_identity(record)

    def _extension_blocked(self, name: str) -> bool:
        lowered = name.lower()
        return any(lowered.endswith(ext) for ext in self.settings.blocked_extensions)

    def upload_file(
        self,
        workspace_id: str,
        scope: ScopeView,
        filename: str,
        chunks: Iterable[bytes],
        *,
        parent_id: Optional[str] = None,
        overwrite: bool = False,
        expected_revision: Optional[int] = None,
        mime_type: Optional[str] = None,
    ) -> FileIdentity:
        self._workspace_record(workspace_id, scope)
        clean = validate_name(filename, field="file name")
        if self._extension_blocked(clean):
            raise WorkspaceFailure(WorkspaceErrorCode.UNSUPPORTED_MEDIA_TYPE, name=clean)
        self._folder_exists(workspace_id, parent_id)

        existing = self.store.find_file_by_name(workspace_id, parent_id, clean)
        if existing is not None and existing["kind"] == FileKind.FOLDER.value:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=clean)
        if existing is not None and not overwrite:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=clean, file_id=existing["file_id"])
        if existing is not None and expected_revision is not None and int(existing["revision"]) != int(expected_revision):
            raise WorkspaceFailure(
                WorkspaceErrorCode.REVISION_CONFLICT,
                expected_revision=expected_revision,
                current_revision=existing["revision"],
            )
        if existing is None:
            if self.store.count_files(workspace_id, kind=FileKind.FILE.value) >= self.settings.max_files_per_workspace:
                raise WorkspaceFailure(WorkspaceErrorCode.QUOTA_EXCEEDED, "file limit reached")

        tenant = self._workspace_record(workspace_id, scope)["tenant_id"]
        usage = self.storage.workspace_usage(tenant, workspace_id)
        reclaim = int(existing["size_bytes"]) if existing is not None else 0
        allowance = self.settings.max_workspace_bytes - (usage - reclaim)
        if allowance <= 0:
            raise WorkspaceFailure(
                WorkspaceErrorCode.QUOTA_EXCEEDED,
                usage_bytes=usage,
                limit_bytes=self.settings.max_workspace_bytes,
            )
        file_id = existing["file_id"] if existing is not None else _new_id("wf")
        revision = int(existing["revision"]) + 1 if existing is not None else 1
        limit = min(self.settings.max_file_bytes, allowance)
        try:
            size, content_hash = self.storage.write_stream(
                tenant, workspace_id, file_id, revision, chunks, max_bytes=limit
            )
        except StorageError as exc:
            if exc.code == "file_too_large":
                if limit < self.settings.max_file_bytes:
                    raise WorkspaceFailure(
                        WorkspaceErrorCode.QUOTA_EXCEEDED,
                        usage_bytes=usage,
                        limit_bytes=self.settings.max_workspace_bytes,
                    ) from exc
                raise WorkspaceFailure(WorkspaceErrorCode.FILE_TOO_LARGE, limit_bytes=self.settings.max_file_bytes) from exc
            raise WorkspaceFailure(WorkspaceErrorCode(str(exc.code)) if exc.code in WorkspaceErrorCode._value2member_map_ else WorkspaceErrorCode.INVALID_REQUEST, str(exc)) from exc

        stamp = self.clock()
        resolved_mime = mime_type or mimetypes.guess_type(clean)[0] or "application/octet-stream"
        revision_record = {
            "file_id": file_id,
            "workspace_id": workspace_id,
            "revision": revision,
            "content_hash": content_hash,
            "size_bytes": size,
            "blob_key": f"r{revision}",
            "parser_version": None,
            "created_at": stamp,
        }
        if existing is None:
            self.store.insert_file(
                {
                    "file_id": file_id,
                    "workspace_id": workspace_id,
                    "parent_id": parent_id,
                    "kind": FileKind.FILE.value,
                    "name": clean,
                    "mime_type": resolved_mime,
                    "size_bytes": size,
                    "content_hash": content_hash,
                    "revision": revision,
                    "index_status": IndexState.PENDING.value,
                    "index_error": None,
                    "index_updated_at": stamp,
                    "created_at": stamp,
                    "updated_at": stamp,
                    "deleted_at": None,
                }
            )
            try:
                self.store.add_revision(revision_record)
            except Exception:
                # metadata must never outlive a failed revision row
                self.store.hard_delete_file(file_id)
                raise
            self._emit(workspace_id, file_id, "file.created", revision)
        else:
            self.store.add_revision(revision_record)
            # A new revision invalidates the running index job before any worker can write.
            self.store.cancel_jobs_for_file(file_id, stamp, reason="file_changed")
            self.store.delete_chunks(file_id)
            self.store.update_file(
                workspace_id,
                file_id,
                {
                    "name": clean,
                    "mime_type": resolved_mime,
                    "size_bytes": size,
                    "content_hash": content_hash,
                    "revision": revision,
                    "index_status": IndexState.PENDING.value,
                    "index_error": None,
                    "index_updated_at": stamp,
                    "updated_at": stamp,
                },
            )
            self._emit(workspace_id, file_id, "file.updated", revision)
        self._prune_revisions(tenant, workspace_id, file_id)
        self._notify_file(workspace_id, file_id)
        return self.get_file_metadata(workspace_id, file_id, scope)

    def _prune_revisions(self, tenant_id: str, workspace_id: str, file_id: str) -> None:
        keep = max(1, int(self.settings.revision_keep))
        revisions = self.store.list_revisions(file_id)
        for row in revisions[keep:]:
            self.storage.delete_blob(tenant_id, workspace_id, file_id, int(row["revision"]))
            self.store.delete_revision(file_id, int(row["revision"]))

    # Alias used by the provider interface.
    def create_file(
        self,
        workspace_id: str,
        scope: ScopeView,
        name: str,
        content: bytes,
        parent_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
        overwrite: bool = False,
    ) -> FileIdentity:
        return self.upload_file(
            workspace_id,
            scope,
            name,
            [content],
            parent_id=parent_id,
            overwrite=overwrite,
            expected_revision=expected_revision,
        )

    def read_file(
        self,
        workspace_id: str,
        file_id: str,
        scope: ScopeView,
        revision: Optional[int] = None,
        *,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> FileContent:
        record = self._file_record(workspace_id, file_id, scope)
        if record["kind"] != FileKind.FILE.value:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "folder has no content")
        current_revision = int(record["revision"])
        target = current_revision if revision is None else int(revision)
        if target < 1:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, revision=target)
        row = self.store.get_revision(file_id, target)
        if row is None:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=file_id, revision=target)
        tenant = self._workspace_record(workspace_id, scope)["tenant_id"]
        try:
            data = self.storage.read_bytes(tenant, workspace_id, file_id, target)
        except StorageError as exc:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=file_id, revision=target) from exc
        if len(data) > max_bytes:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_TOO_LARGE, limit_bytes=max_bytes)
        try:
            text: Optional[str] = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        status = RevisionStatus.CURRENT if target == current_revision else RevisionStatus.SUPERSEDED
        return FileContent(
            metadata=self.file_identity(record),
            revision=target,
            revision_status=status,
            content_hash=row["content_hash"],
            bytes=data,
            text=text,
        )

    def iter_file(self, workspace_id: str, file_id: str, scope: ScopeView, revision: Optional[int] = None):
        record = self._file_record(workspace_id, file_id, scope)
        if record["kind"] != FileKind.FILE.value:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "folder has no content")
        target = int(record["revision"]) if revision is None else int(revision)
        if self.store.get_revision(file_id, target) is None:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=file_id, revision=target)
        tenant = self._workspace_record(workspace_id, scope)["tenant_id"]
        return self.storage.iter_content(tenant, workspace_id, file_id, target)

    def rename_file(self, workspace_id: str, file_id: str, scope: ScopeView, new_name: str, expected_revision: Optional[int] = None) -> FileIdentity:
        record = self._file_record(workspace_id, file_id, scope)
        if expected_revision is not None and int(record["revision"]) != int(expected_revision):
            raise WorkspaceFailure(
                WorkspaceErrorCode.REVISION_CONFLICT,
                expected_revision=expected_revision,
                current_revision=record["revision"],
            )
        clean = validate_name(new_name, field="name")
        if self._extension_blocked(clean):
            raise WorkspaceFailure(WorkspaceErrorCode.UNSUPPORTED_MEDIA_TYPE, name=clean)
        clash = self.store.find_file_by_name(workspace_id, record["parent_id"], clean)
        if clash is not None and clash["file_id"] != file_id:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=clean)
        fields: Dict[str, Any] = {"name": clean, "updated_at": self.clock()}
        if record["kind"] == FileKind.FILE.value:
            fields["mime_type"] = mimetypes.guess_type(clean)[0] or record.get("mime_type")
        self.store.update_file(workspace_id, file_id, fields)
        self._emit(workspace_id, file_id, "file.renamed", int(record["revision"]), {"name": clean})
        return self.get_file_metadata(workspace_id, file_id, scope)

    def move_file(self, workspace_id: str, file_id: str, scope: ScopeView, parent_id: Optional[str], expected_revision: Optional[int] = None) -> FileIdentity:
        record = self._file_record(workspace_id, file_id, scope)
        if expected_revision is not None and int(record["revision"]) != int(expected_revision):
            raise WorkspaceFailure(
                WorkspaceErrorCode.REVISION_CONFLICT,
                expected_revision=expected_revision,
                current_revision=record["revision"],
            )
        self._folder_exists(workspace_id, parent_id)
        if parent_id == file_id:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, "cannot move into itself")
        if record["kind"] == FileKind.FOLDER.value and parent_id is not None:
            if parent_id in set(self.store.list_file_ids_under(workspace_id, file_id)):
                raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, "cannot move folder into its descendant")
        clash = self.store.find_file_by_name(workspace_id, parent_id, record["name"])
        if clash is not None and clash["file_id"] != file_id:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=record["name"])
        self.store.update_file(
            workspace_id, file_id, {"parent_id": parent_id, "updated_at": self.clock()}
        )
        self._emit(workspace_id, file_id, "file.moved", int(record["revision"]), {"parent_id": parent_id})
        return self.get_file_metadata(workspace_id, file_id, scope)

    def update_file(
        self,
        workspace_id: str,
        file_id: str,
        scope: ScopeView,
        *,
        name: Optional[str] = None,
        parent_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
        move_parent: bool = False,
    ) -> FileIdentity:
        record = self._file_record(workspace_id, file_id, scope)
        if name is not None and name.strip() != record["name"]:
            updated = self.rename_file(workspace_id, file_id, scope, name, expected_revision)
        else:
            updated = self.file_identity(record)
        if move_parent and (parent_id != record["parent_id"]):
            updated = self.move_file(workspace_id, file_id, scope, parent_id, expected_revision)
        return updated

    def delete_file(self, workspace_id: str, file_id: str, scope: ScopeView) -> Dict[str, Any]:
        record = self._file_record(workspace_id, file_id, scope)
        tenant = self._workspace_record(workspace_id, scope)["tenant_id"]
        targets = [record]
        if record["kind"] == FileKind.FOLDER.value:
            for descendant_id in self.store.list_file_ids_under(workspace_id, file_id):
                row = self.store.get_file(workspace_id, descendant_id)
                if row is not None:
                    targets.append(row)
        stamp = self.clock()
        removed_blobs = 0
        for target in targets:
            self.store.cancel_jobs_for_file(target["file_id"], stamp, reason="file_deleted")
            self.store.delete_chunks(target["file_id"])
            self.store.delete_revisions(target["file_id"])
            removed_blobs += self.storage.delete_file(tenant, workspace_id, target["file_id"])
            self.store.soft_delete_file(workspace_id, target["file_id"], stamp)
            self._emit(workspace_id, target["file_id"], "file.deleted", int(target["revision"]))
        return {"file_id": file_id, "deleted": True, "removed_files": len(targets), "removed_blobs": removed_blobs}

    # ------------------------------------------------------------------ references
    def register_artifact_reference(
        self, workspace_id: str, scope: ScopeView, ref: ArtifactReferenceInput
    ) -> ArtifactReference:
        self._workspace_record(workspace_id, scope)
        if ref.file_id is not None:
            self._file_record(workspace_id, ref.file_id, scope)
        if not ref.artifact_id or len(ref.artifact_id) > 128:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "artifact_id required")
        existing = [
            row
            for row in self.store.list_artifact_refs(workspace_id, ref.kind)
            if row["artifact_id"] == ref.artifact_id
        ]
        if existing:
            row = existing[0]
            return ArtifactReference(
                artifact_ref_id=row["artifact_ref_id"],
                workspace_id=workspace_id,
                artifact_id=row["artifact_id"],
                kind=row["kind"],
                file_id=row["file_id"],
                metadata=json.loads(row["metadata"] or "{}"),
                created_at=row["created_at"],
            )
        stamp = self.clock()
        record = {
            "artifact_ref_id": _new_id("wref"),
            "workspace_id": workspace_id,
            "artifact_id": ref.artifact_id,
            "kind": ref.kind or "artifact",
            "file_id": ref.file_id,
            "metadata": json.dumps(ref.metadata or {}, ensure_ascii=False),
            "created_at": stamp,
        }
        self.store.add_artifact_ref(record)
        return ArtifactReference(
            artifact_ref_id=record["artifact_ref_id"],
            workspace_id=workspace_id,
            artifact_id=record["artifact_id"],
            kind=record["kind"],
            file_id=record["file_id"],
            metadata=ref.metadata or {},
            created_at=stamp,
        )

    def list_artifact_references(
        self, workspace_id: str, scope: ScopeView, kind: Optional[str] = None
    ) -> List[ArtifactReference]:
        self._workspace_record(workspace_id, scope)
        return [
            ArtifactReference(
                artifact_ref_id=row["artifact_ref_id"],
                workspace_id=workspace_id,
                artifact_id=row["artifact_id"],
                kind=row["kind"],
                file_id=row["file_id"],
                metadata=json.loads(row["metadata"] or "{}"),
                created_at=row["created_at"],
            )
            for row in self.store.list_artifact_refs(workspace_id, kind)
        ]

    def delete_artifact_reference(self, workspace_id: str, scope: ScopeView, artifact_ref_id: str) -> bool:
        self._workspace_record(workspace_id, scope)
        if self.store.get_artifact_ref(workspace_id, artifact_ref_id) is None:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, artifact_ref_id=artifact_ref_id)
        return self.store.delete_artifact_ref(workspace_id, artifact_ref_id)

    def add_conversation_reference(self, workspace_id: str, scope: ScopeView, conversation_id: str) -> Dict[str, Any]:
        self._workspace_record(workspace_id, scope)
        clean = (conversation_id or "").strip()
        if not clean or len(clean) > 128 or _CONTROL_CHARS.search(clean):
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "invalid conversation_id")
        self.store.add_conversation_ref(workspace_id, clean, self.clock())
        return {"workspace_id": workspace_id, "conversation_id": clean}

    def list_conversation_references(self, workspace_id: str, scope: ScopeView) -> List[Dict[str, Any]]:
        self._workspace_record(workspace_id, scope)
        return [
            {"workspace_id": row["workspace_id"], "conversation_id": row["conversation_id"], "created_at": row["created_at"]}
            for row in self.store.list_conversation_refs(workspace_id)
        ]

    def list_events(self, workspace_id: str, scope: ScopeView, since: int = 0, limit: int = 100) -> Dict[str, Any]:
        self._workspace_record(workspace_id, scope)
        rows = self.store.list_events(workspace_id, since, limit)
        items = [
            {
                "event_id": row["event_id"],
                "type": row["type"],
                "workspace_id": row["workspace_id"],
                "file_id": row["file_id"],
                "revision": row["revision"],
                "at": row["at"],
            }
            for row in rows
        ]
        next_since = rows[-1]["event_id"] if rows else since
        return {"items": items, "next_since": next_since}

    # ------------------------------------------------------------------ recovery
    def recover(self) -> Dict[str, int]:
        """Restart recovery: requeue interrupted jobs, sweep temp files, drop orphan blobs."""

        requeued = self.store.stale_indexing_jobs(self.clock())
        temp_removed = self.storage.sweep_temp(older_than_seconds=0)
        known: List[Path] = []
        for workspace in self.store.list_all_workspaces():
            for row in self.store.list_files(workspace["workspace_id"], recursive=True, include_deleted=True):
                for revision in self.store.list_revisions(row["file_id"]):
                    try:
                        known.append(
                            self.storage.blob_path(workspace["tenant_id"], workspace["workspace_id"], row["file_id"], int(revision["revision"]))
                        )
                    except StorageError:
                        continue
        orphans = 0
        for blob in self.storage.orphan_blobs(known):
            try:
                blob.unlink()
                orphans += 1
            except OSError:
                continue
        return {"requeued_jobs": requeued, "temp_removed": temp_removed, "orphan_blobs_removed": orphans}

    def workspace_usage(self, workspace_id: str, scope: ScopeView) -> Dict[str, int]:
        record = self._workspace_record(workspace_id, scope)
        return {
            "usage_bytes": self.storage.workspace_usage(record["tenant_id"], workspace_id),
            "limit_bytes": self.settings.max_workspace_bytes,
            "files": self.store.count_files(workspace_id, kind=FileKind.FILE.value),
            "file_limit": self.settings.max_files_per_workspace,
        }
