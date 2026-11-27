"""Workspace manager: identity, file lifecycle, access policy and storage orchestration."""
from __future__ import annotations

import base64
import binascii
import json
import hashlib
import mimetypes
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from app.workspace import folder as folderfs
from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import (
    AccessLevel,
    AccessResult,
    AllowedRootInfo,
    ArtifactReference,
    ArtifactReferenceInput,
    ClientWriteDirective,
    ContentOrigin,
    FileContent,
    FileIdentity,
    FileKind,
    IndexState,
    IndexStorageInfo,
    InvalidRelativePath,
    RevisionStatus,
    RootStatus,
    ScopeView,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceIdentity,
    WorkspaceLifecycleState,
    WorkspaceRootInfo,
    WorkspaceStatus,
    WorkspaceStorageProvider,
    WorkspaceSyncResult,
    WorkspaceWatchStatus,
    WorkspaceWriteRequest,
    WorkspaceWriteResult,
    validate_relative_path,
)
from app.workspace.rootfs import AllowedRootPolicy, RootPolicyError
from app.workspace.storage import StorageError, WorkspaceStorage
from app.workspace.store import WorkspaceStore
from app.workspace.sync import FolderSyncService

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


def _collect(chunks: Iterable[bytes], limit: int) -> bytes:
    """Join an upload stream, refusing anything over the per-file limit."""

    total = 0
    blocks: List[bytes] = []
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if limit and total > limit:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_TOO_LARGE, limit_bytes=limit)
        blocks.append(chunk)
    if not blocks:
        raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "empty file")
    return b"".join(blocks)


def _write_payload(request: WorkspaceWriteRequest) -> bytes:
    if request.content is not None and request.content_base64 is not None:
        raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "provide content or content_base64, not both")
    if request.content is not None:
        return request.content.encode("utf-8")
    if request.content_base64 is not None:
        try:
            return base64.b64decode(request.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "content_base64 is not valid base64") from exc
    raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "content required")


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
        self._policy: Optional[AllowedRootPolicy] = None
        self._sync_service: Optional[FolderSyncService] = None

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
            lifecycle_state=WorkspaceLifecycleState(record.get("lifecycle_state") or "open"),
            root=self._root_info(record),
            closed_at=record.get("closed_at"),
        )

    def _root_info(self, record: Dict[str, Any]) -> WorkspaceRootInfo:
        provider = WorkspaceStorageProvider(record.get("storage_provider") or WorkspaceStorageProvider.SERVER_MANAGED.value)
        root_path = record.get("root_path") or ""
        status = RootStatus.AVAILABLE
        if record.get("lifecycle_state") == WorkspaceLifecycleState.DELETED.value:
            status = RootStatus.MISSING
        elif provider is not WorkspaceStorageProvider.LOCAL_BROWSER and root_path:
            candidate = Path(root_path)
            if not candidate.exists() or not candidate.is_dir():
                status = RootStatus.MISSING
        return WorkspaceRootInfo(
            storage_provider=provider,
            root_id=record.get("root_id"),
            display_path=record.get("display_path") or root_path or "",
            allowed_root_id=record.get("root_id") if provider is not WorkspaceStorageProvider.SERVER_MANAGED else None,
            read_only=bool(record.get("read_only")),
            zero_copy=bool(record.get("zero_copy")) or provider is not WorkspaceStorageProvider.SERVER_MANAGED,
            server_side_path_access=provider is not WorkspaceStorageProvider.LOCAL_BROWSER,
            client_handle_id=record.get("client_handle_id"),
            opened_at=record.get("opened_at"),
            status=status,
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

    def file_identity(self, record: Dict[str, Any], provider: Optional[WorkspaceStorageProvider] = None) -> FileIdentity:
        workspace_id = record["workspace_id"]
        status = record.get("index_status")
        origin = record.get("origin") or ContentOrigin.BLOB.value
        if provider is None:
            workspace = self.store.get_workspace(workspace_id)
            provider = WorkspaceStorageProvider(
                (workspace or {}).get("storage_provider") or WorkspaceStorageProvider.SERVER_MANAGED.value
            )
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
            storage_provider=provider,
            content_origin=ContentOrigin(origin),
            relative_path=record.get("rel_path"),
            synced_at=record.get("synced_at"),
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
            "storage_provider": WorkspaceStorageProvider.SERVER_MANAGED.value,
            "root_id": None,
            "root_path": None,
            "display_path": "",
            "lifecycle_state": WorkspaceLifecycleState.OPEN.value,
            "read_only": 0,
            "zero_copy": 0,
            "client_handle_id": None,
            "opened_at": stamp,
            "closed_at": None,
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
        """Delete a workspace. For an external root this only releases the binding (§A1.6)."""

        return self.disconnect_workspace(workspace_id, scope, delete_source_files=False)

    # ------------------------------------------------------------------ Amendment 01
    # Allowed roots, folder lifecycle, zero-copy sync and the common write contract.
    @property
    def allowed_root_policy(self) -> AllowedRootPolicy:
        if self._policy is None:
            protected = [self.settings.resolved_db_path(), self.settings.resolved_storage_root()]
            self._policy = AllowedRootPolicy(self.settings.allowed_roots, protected_paths=protected)
        return self._policy

    @property
    def sync_service(self) -> FolderSyncService:
        if self._sync_service is None:
            self._sync_service = FolderSyncService(self, self.settings)
        return self._sync_service

    def allowed_roots(self, scope: ScopeView) -> List[AllowedRootInfo]:
        tenant, owner, _service = self._scope_ids(scope)
        return self.allowed_root_policy.describe(tenant, owner)

    @staticmethod
    def _root_failure(exc: RootPolicyError) -> WorkspaceFailure:
        try:
            code = WorkspaceErrorCode(exc.code)
        except ValueError:
            code = WorkspaceErrorCode.INVALID_PATH
        return WorkspaceFailure(code, str(exc))

    @staticmethod
    def _folder_failure(exc: Exception) -> WorkspaceFailure:
        code = getattr(exc, "code", "invalid_path")
        try:
            mapped = WorkspaceErrorCode(code)
        except ValueError:
            mapped = WorkspaceErrorCode.INVALID_REQUEST
        return WorkspaceFailure(mapped, str(exc))

    @staticmethod
    def _is_folder_backed(record: Dict[str, Any]) -> bool:
        return bool(record.get("root_path")) and WorkspaceStorageProvider(
            record.get("storage_provider") or WorkspaceStorageProvider.SERVER_MANAGED.value
        ) in {
            WorkspaceStorageProvider.SERVER_MANAGED,
            WorkspaceStorageProvider.SERVER_MOUNTED_FOLDER,
        }

    @staticmethod
    def _is_local_browser(record: Dict[str, Any]) -> bool:
        return (
            WorkspaceStorageProvider(record.get("storage_provider") or WorkspaceStorageProvider.SERVER_MANAGED.value)
            is WorkspaceStorageProvider.LOCAL_BROWSER
        )

    def _require_open(self, record: Dict[str, Any]) -> None:
        if record.get("lifecycle_state") == WorkspaceLifecycleState.CLOSED.value:
            raise WorkspaceFailure(WorkspaceErrorCode.WORKSPACE_CLOSED, workspace_id=record.get("workspace_id"))

    def ensure_open(self, workspace_id: str, scope: ScopeView) -> WorkspaceIdentity:
        """Access check plus lifecycle gate, for read surfaces outside the file API."""

        record = self._workspace_record(workspace_id, scope)
        self._require_open(record)
        return self.workspace_identity(record)

    def _require_writable(self, record: Dict[str, Any]) -> None:
        self._require_open(record)
        if int(record.get("read_only") or 0):
            raise WorkspaceFailure(WorkspaceErrorCode.READ_ONLY_WORKSPACE, workspace_id=record.get("workspace_id"))

    def _root_path(self, record: Dict[str, Any]) -> Path:
        root_path = record.get("root_path") or ""
        if not root_path:
            raise WorkspaceFailure(WorkspaceErrorCode.ROOT_UNAVAILABLE, workspace_id=record.get("workspace_id"))
        return Path(root_path)

    # -- open / new / local browser ------------------------------------------------
    def open_folder(
        self,
        scope: ScopeView,
        path: str,
        *,
        name: Optional[str] = None,
        read_only: bool = False,
        root_id: Optional[str] = None,
    ) -> WorkspaceIdentity:
        tenant, owner, service = self._scope_ids(scope)
        try:
            resolved = self.allowed_root_policy.resolve(
                tenant, owner, path, root_id=root_id, must_exist=True, for_write=not read_only
            )
        except RootPolicyError as exc:
            raise self._root_failure(exc) from exc

        # Idempotent open: the same folder for the same owner returns the live workspace.
        for existing in self.store.list_workspaces(tenant, owner, None if not self.settings.service_isolation else service):
            if existing.get("root_path") == str(resolved.path) and existing.get("lifecycle_state") != "deleted":
                return self.workspace_identity(existing)

        if self.store.count_workspaces(tenant, owner) >= self.settings.max_workspaces_per_owner:
            raise WorkspaceFailure(WorkspaceErrorCode.QUOTA_EXCEEDED, "workspace limit reached")

        clean_name = validate_name(
            name or Path(resolved.path).name or resolved.root.root_id,
            field="workspace name",
            max_length=120,
        )
        stamp = self.clock()
        record = {
            "workspace_id": _new_id("ws"),
            "tenant_id": tenant,
            "owner_id": owner,
            "service_id": service,
            "name": clean_name,
            "description": "",
            "status": WorkspaceStatus.ACTIVE.value,
            "revision": 1,
            "created_at": stamp,
            "updated_at": stamp,
            "storage_provider": WorkspaceStorageProvider.SERVER_MOUNTED_FOLDER.value,
            "root_id": resolved.root.root_id,
            "root_path": str(resolved.path),
            "display_path": resolved.display_path,
            "lifecycle_state": WorkspaceLifecycleState.OPEN.value,
            "read_only": 1 if (read_only or not resolved.writable) else 0,
            "zero_copy": 1,
            "client_handle_id": None,
            "opened_at": stamp,
            "closed_at": None,
        }
        self.store.create_workspace(record)
        self._emit(record["workspace_id"], None, "workspace.opened", 1, {"provider": record["storage_provider"], "path": str(resolved.path)})
        self._initial_sync(record)
        return self.get_workspace(record["workspace_id"], scope)

    def new_workspace(
        self,
        scope: ScopeView,
        name: str,
        *,
        description: str = "",
        parent_path: Optional[str] = None,
        root_id: Optional[str] = None,
    ) -> WorkspaceIdentity:
        tenant, owner, service = self._scope_ids(scope)
        if not self.settings.allow_new_workspace_folder:
            raise WorkspaceFailure(WorkspaceErrorCode.FORBIDDEN, "new folder workspace disabled")
        clean_name = validate_name(name, field="workspace name", max_length=120)
        policy = self.allowed_root_policy
        if parent_path:
            try:
                resolved = policy.resolve(
                    tenant, owner, parent_path, root_id=root_id, must_exist=False, for_write=True
                )
            except RootPolicyError as exc:
                raise self._root_failure(exc) from exc
            base = resolved.path
        else:
            roots = policy.visible_roots(tenant, owner)
            if not roots:
                raise WorkspaceFailure(WorkspaceErrorCode.PATH_OUTSIDE_ALLOWED_ROOT, "no allowed root configured")
            default = next((item for item in roots if item.root.is_default), roots[0])
            if not default.writable:
                raise WorkspaceFailure(WorkspaceErrorCode.READ_ONLY_WORKSPACE, "default root is read-only")
            base = default.path
            resolved = default
        try:
            policy.assert_inside(base, clean_name)
        except RootPolicyError as exc:
            raise self._root_failure(exc) from exc
        target = Path(base) / clean_name
        if target.exists():
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=clean_name)
        try:
            base.mkdir(parents=True, exist_ok=True)
            target.mkdir(mode=0o755, exist_ok=True)
        except OSError as exc:
            raise WorkspaceFailure(WorkspaceErrorCode.ROOT_UNAVAILABLE, str(exc)[:200]) from exc
        if not target.is_dir():
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=clean_name)

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
            "storage_provider": WorkspaceStorageProvider.SERVER_MANAGED.value,
            "root_id": resolved.root.root_id,
            "root_path": str(target),
            "display_path": str(target),
            "lifecycle_state": WorkspaceLifecycleState.OPEN.value,
            "read_only": 0,
            "zero_copy": 1,
            "client_handle_id": None,
            "opened_at": stamp,
            "closed_at": None,
        }
        self.store.create_workspace(record)
        self._emit(record["workspace_id"], None, "workspace.created", 1, {"provider": record["storage_provider"], "path": str(target)})
        self._initial_sync(record)
        return self.get_workspace(record["workspace_id"], scope)

    def create_local_browser_workspace(
        self,
        scope: ScopeView,
        name: str,
        *,
        client_handle_id: str,
        entries: Sequence[Any] = (),
        display_path: str = "",
        description: str = "",
        read_only: bool = True,
    ) -> WorkspaceIdentity:
        """Register a browser-picked folder. The server never learns a server-side path (§A1.8)."""

        tenant, owner, service = self._scope_ids(scope)
        clean_name = validate_name(name, field="workspace name", max_length=120)
        handle = (client_handle_id or "").strip()
        if not handle or len(handle) > 128 or _CONTROL_CHARS.search(handle):
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "invalid client_handle_id")
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
            "storage_provider": WorkspaceStorageProvider.LOCAL_BROWSER.value,
            "root_id": None,
            "root_path": None,
            "display_path": (display_path or clean_name)[:512],
            "lifecycle_state": WorkspaceLifecycleState.OPEN.value,
            "read_only": 1 if read_only else 0,
            "zero_copy": 1,
            "client_handle_id": handle,
            "opened_at": stamp,
            "closed_at": None,
        }
        self.store.create_workspace(record)
        self._emit(record["workspace_id"], None, "workspace.opened", 1, {"provider": record["storage_provider"]})
        for entry in entries:
            self._register_browser_entry(record, entry)
        return self.get_workspace(record["workspace_id"], scope)

    def _register_browser_entry(self, workspace: Dict[str, Any], entry: Any) -> Optional[str]:
        rel_raw = getattr(entry, "path", None) if not isinstance(entry, dict) else entry.get("path")
        try:
            rel = validate_relative_path(str(rel_raw or ""))
        except InvalidRelativePath:
            return None
        kind = (getattr(entry, "kind", None) if not isinstance(entry, dict) else entry.get("kind")) or FileKind.FILE
        if hasattr(kind, "value"):
            kind = kind.value
        if len(rel) > 1024:
            return None
        parent_id = self._ensure_folder_rows(workspace["workspace_id"], rel.rsplit("/", 1)[0] if "/" in rel else "")
        if kind == FileKind.FOLDER.value:
            if self.store.find_file_by_name(workspace["workspace_id"], parent_id, rel.rsplit("/", 1)[-1]) is None:
                return self._insert_browser_row(workspace, rel, parent_id, FileKind.FOLDER, 0, None)
            return None
        text = getattr(entry, "text", None) if not isinstance(entry, dict) else entry.get("text")
        size = int((getattr(entry, "size_bytes", 0) if not isinstance(entry, dict) else entry.get("size_bytes")) or 0)
        digest = (getattr(entry, "content_hash", None) if not isinstance(entry, dict) else entry.get("content_hash")) or None
        payload = text.encode("utf-8") if isinstance(text, str) else b""
        if not digest and payload:
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if not size:
            size = len(payload)
        file_id = self._insert_browser_row(workspace, rel, parent_id, FileKind.FILE, size, digest)
        if payload:
            self.store.put_capture(workspace["workspace_id"], file_id, 1, payload.decode("utf-8", errors="replace"), self.clock())
        return file_id

    def _insert_browser_row(
        self,
        workspace: Dict[str, Any],
        rel: str,
        parent_id: Optional[str],
        kind: FileKind,
        size: int,
        digest: Optional[str],
    ) -> str:
        stamp = self.clock()
        file_id = _new_id("wf")
        name = rel.rsplit("/", 1)[-1]
        self.store.insert_file(
            {
                "file_id": file_id,
                "workspace_id": workspace["workspace_id"],
                "parent_id": parent_id,
                "kind": kind.value,
                "name": name,
                "mime_type": mimetypes.guess_type(name)[0] if kind is FileKind.FILE else None,
                "size_bytes": size,
                "content_hash": digest,
                "revision": 1,
                "index_status": IndexState.PENDING.value if kind is FileKind.FILE and digest else None,
                "index_error": None,
                "index_updated_at": stamp,
                "created_at": stamp,
                "updated_at": stamp,
                "deleted_at": None,
                "origin": "external",
                "rel_path": rel,
                "ext_mtime": None,
                "synced_at": stamp,
            }
        )
        if kind is FileKind.FILE and digest:
            self.store.add_revision(
                {
                    "file_id": file_id,
                    "workspace_id": workspace["workspace_id"],
                    "revision": 1,
                    "content_hash": digest,
                    "size_bytes": size,
                    "blob_key": "browser",
                    "parser_version": None,
                    "created_at": stamp,
                }
            )
        self._emit(workspace["workspace_id"], file_id, "file.created", 1, {"provider": "local_browser", "path": rel})
        if kind is FileKind.FILE:
            self._notify_file(workspace["workspace_id"], file_id)
        return file_id

    def _ensure_folder_rows(self, workspace_id: str, rel_dir: str) -> Optional[str]:
        """Create folder rows for `rel_dir` and return the deepest folder id (external layout)."""

        if not rel_dir:
            return None
        parent_id: Optional[str] = None
        prefix = ""
        for part in rel_dir.split("/"):
            prefix = f"{prefix}/{part}" if prefix else part
            row = self.store.get_file_by_rel_path(workspace_id, prefix)
            if row is not None and row["kind"] == FileKind.FOLDER.value:
                parent_id = row["file_id"]
                continue
            existing = self.store.find_file_by_name(workspace_id, parent_id, part)
            if existing is not None and existing["kind"] == FileKind.FOLDER.value:
                parent_id = existing["file_id"]
                continue
            stamp = self.clock()
            file_id = _new_id("wf")
            self.store.insert_file(
                {
                    "file_id": file_id,
                    "workspace_id": workspace_id,
                    "parent_id": parent_id,
                    "kind": FileKind.FOLDER.value,
                    "name": part,
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
                    "origin": "external",
                    "rel_path": prefix,
                    "ext_mtime": None,
                    "synced_at": stamp,
                }
            )
            parent_id = file_id
        return parent_id

    def _initial_sync(self, record: Dict[str, Any]) -> None:
        """First scan after open/new. A failure closes the workspace instead of leaving it half-open."""

        try:
            result = self.sync_service.sync(record, full=True)
        except Exception as exc:  # folder unreadable or race
            stamp = self.clock()
            self.store.update_workspace(
                record["workspace_id"],
                {
                    "lifecycle_state": WorkspaceLifecycleState.CLOSED.value,
                    "closed_at": stamp,
                    "updated_at": stamp,
                    "revision": int(record["revision"]) + 1,
                },
            )
            self.store.upsert_watch_state(
                record["workspace_id"],
                {"last_scan_at": stamp, "last_error": str(exc)[:300], "watching": 0, "updated_at": stamp},
            )
            self._emit(record["workspace_id"], None, "workspace.open_failed", 1, {"error": str(exc)[:300]})
            raise self._folder_failure(exc) from exc
        self._record_sync(record["workspace_id"], result)

    def _record_sync(self, workspace_id: str, result: WorkspaceSyncResult, *, error: Optional[str] = None) -> None:
        current = self.store.get_watch_state(workspace_id) or {}
        delta = result.created + result.modified + result.moved + result.deleted
        stamp = self.clock()
        self.store.upsert_watch_state(
            workspace_id,
            {
                "last_scan_at": result.finished_at or stamp,
                "last_change_at": stamp if delta else current.get("last_change_at"),
                "changes_total": int(current.get("changes_total") or 0) + delta,
                "last_error": error,
                "watching": 1,
                "updated_at": stamp,
            },
        )

    # -- close / reopen / disconnect ------------------------------------------------
    def close_workspace(self, workspace_id: str, scope: ScopeView, reason: str = "") -> WorkspaceIdentity:
        record = self._workspace_record(workspace_id, scope)
        if record.get("lifecycle_state") == WorkspaceLifecycleState.CLOSED.value:
            return self.workspace_identity(record)
        stamp = self.clock()
        for file_row in self.store.list_files(workspace_id, recursive=True, include_deleted=False):
            self.store.cancel_jobs_for_file(file_row["file_id"], stamp, reason="workspace_closed")
        self.store.update_workspace(
            workspace_id,
            {
                "lifecycle_state": WorkspaceLifecycleState.CLOSED.value,
                "closed_at": stamp,
                "updated_at": stamp,
                "revision": int(record["revision"]) + 1,
            },
        )
        self.store.upsert_watch_state(workspace_id, {"watching": 0, "updated_at": stamp})
        self._emit(workspace_id, None, "workspace.closed", int(record["revision"]) + 1, {"reason": (reason or "")[:200]})
        return self.get_workspace(workspace_id, scope)

    def reopen_workspace(self, workspace_id: str, scope: ScopeView) -> WorkspaceIdentity:
        record = self._workspace_record(workspace_id, scope)
        tenant, owner, _service = self._scope_ids(scope)
        provider = WorkspaceStorageProvider(record.get("storage_provider") or "server_managed")
        if provider is not WorkspaceStorageProvider.LOCAL_BROWSER:
            root_path = record.get("root_path") or ""
            if not root_path:
                # a v1 blob workspace has nothing to re-attach
                raise WorkspaceFailure(WorkspaceErrorCode.PROVIDER_MISMATCH, workspace_id=workspace_id)
            try:
                self.allowed_root_policy.resolve(
                    tenant, owner, root_path, root_id=record.get("root_id"), must_exist=True
                )
            except RootPolicyError as exc:
                raise self._root_failure(exc) from exc
        stamp = self.clock()
        self.store.update_workspace(
            workspace_id,
            {
                "lifecycle_state": WorkspaceLifecycleState.OPEN.value,
                "closed_at": None,
                "opened_at": stamp,
                "updated_at": stamp,
                "revision": int(record["revision"]) + 1,
            },
        )
        self._emit(workspace_id, None, "workspace.reopened", int(record["revision"]) + 1)
        refreshed = self.store.get_workspace(workspace_id) or record
        if self._is_folder_backed(refreshed):
            self.sync_workspace_record(refreshed, full=False)
        return self.get_workspace(workspace_id, scope)

    def disconnect_workspace(
        self,
        workspace_id: str,
        scope: ScopeView,
        *,
        delete_source_files: bool = False,
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """Release the workspace. The real folder is never touched unless explicitly allowed."""

        record = self._workspace_record(workspace_id, scope)
        source_files_deleted = 0
        if delete_source_files:
            if not self.settings.allow_source_delete or not confirm:
                raise WorkspaceFailure(
                    WorkspaceErrorCode.SOURCE_DELETE_NOT_PERMITTED,
                    "source file deletion is disabled; disconnect only",
                    workspace_id=workspace_id,
                )
            if self._is_folder_backed(record):
                source_files_deleted = self._delete_source_tree(record)
        stamp = self.clock()
        for file_row in self.store.list_files(workspace_id, recursive=True, include_deleted=False):
            self.store.cancel_jobs_for_file(file_row["file_id"], stamp, reason="workspace_disconnected")
        purged = self.store.purge_workspace_files(workspace_id)
        self.store.mark_workspace_deleted(workspace_id, stamp)
        self.store.update_workspace(
            workspace_id,
            {
                "lifecycle_state": WorkspaceLifecycleState.DELETED.value,
                "updated_at": stamp,
            },
        )
        self.store.upsert_watch_state(workspace_id, {"watching": 0, "updated_at": stamp})
        removed_blobs = self.storage.delete_workspace(record["tenant_id"], workspace_id)
        self._emit(
            workspace_id,
            None,
            "workspace.disconnected",
            int(record["revision"]),
            {"source_files_deleted": source_files_deleted},
        )
        return {
            "workspace_id": workspace_id,
            "status": "deleted",
            "lifecycle_state": WorkspaceLifecycleState.DELETED.value,
            "purged_files": purged,
            "removed_blobs": removed_blobs,
            "source_files_deleted": source_files_deleted,
        }

    def _delete_source_tree(self, record: Dict[str, Any]) -> int:
        """Delete only the files this workspace tracks, then empty folders (never a blind rmtree)."""

        root = Path(record.get("root_path") or "")
        removed = 0
        rows = sorted(
            self.store.list_files(record["workspace_id"], recursive=True, include_deleted=False),
            key=lambda row: str(row.get("rel_path") or "").count("/"),
            reverse=True,
        )
        for row in rows:
            rel = row.get("rel_path")
            if not rel:
                continue
            try:
                if row["kind"] == FileKind.FILE.value:
                    removed += folderfs.remove_relative(root, rel)
                else:
                    folderfs.try_remove_empty_directory(root, rel)
            except Exception:
                continue
        return removed

    # -- watcher-facing sync --------------------------------------------------------
    def sync_workspace(self, workspace_id: str, scope: ScopeView, full: bool = False) -> WorkspaceSyncResult:
        record = self._workspace_record(workspace_id, scope)
        self._require_open(record)
        return self.sync_workspace_record(record, full=full)

    def sync_workspace_record(self, record: Dict[str, Any], *, full: bool = False) -> WorkspaceSyncResult:
        workspace_id = record["workspace_id"]
        if not self._is_folder_backed(record):
            raise WorkspaceFailure(WorkspaceErrorCode.PROVIDER_MISMATCH, workspace_id=workspace_id)
        try:
            result = self.sync_service.sync(record, full=full)
        except folderfs.FolderError as exc:
            self.store.upsert_watch_state(
                workspace_id, {"last_scan_at": self.clock(), "last_error": str(exc)[:300], "updated_at": self.clock()}
            )
            raise self._folder_failure(exc) from exc
        self._record_sync(workspace_id, result, error=("; ".join(item.get("code", "") for item in result.errors[:3]) or None))
        return result

    def watch_status(self, workspace_id: str, scope: ScopeView) -> WorkspaceWatchStatus:
        record = self._workspace_record(workspace_id, scope)
        state = self.store.get_watch_state(workspace_id) or {}
        provider = WorkspaceStorageProvider(record.get("storage_provider") or "server_managed")
        watching = (
            record.get("lifecycle_state") == WorkspaceLifecycleState.OPEN.value
            and not int(record.get("read_only") or 0)
            and provider is not WorkspaceStorageProvider.LOCAL_BROWSER
            and bool(self.settings.watch_enabled)
        )
        return WorkspaceWatchStatus(
            workspace_id=workspace_id,
            watching=watching,
            storage_provider=provider,
            interval_seconds=float(self.settings.watch_interval_seconds) if watching else 0.0,
            last_scan_at=state.get("last_scan_at"),
            last_change_at=state.get("last_change_at"),
            changes_total=int(state.get("changes_total") or 0),
            last_error=state.get("last_error"),
        )

    def workspace_root_info(self, workspace_id: str, scope: ScopeView) -> Dict[str, Any]:
        record = self._workspace_record(workspace_id, scope)
        root = self._root_info(record)
        index_info = IndexStorageInfo()
        db_path = Path(self.settings.resolved_db_path()).expanduser()
        if record.get("root_path"):
            root_path = Path(record["root_path"]).resolve()
            try:
                inside = str(db_path.resolve()).startswith(str(root_path) + os.sep) or db_path.resolve() == root_path
            except OSError:  # pragma: no cover
                inside = False
            index_info = IndexStorageInfo(separate_from_workspace_root=not inside, inside_workspace_root=inside)
        return {"workspace_id": workspace_id, "root": root.model_dump(mode="json"), "index_storage": index_info.model_dump(mode="json")}

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

    def _rel_dir(self, workspace_id: str, parent_id: Optional[str]) -> str:
        parts: List[str] = []
        cursor = parent_id
        guard = 0
        while cursor and guard < 64:
            row = self.store.get_file(workspace_id, cursor, include_deleted=True)
            if row is None:
                break
            parts.append(row["name"])
            cursor = row.get("parent_id")
            guard += 1
        return "/".join(reversed(parts))

    def list_files(
        self,
        workspace_id: str,
        scope: ScopeView,
        parent_id: Optional[str] = None,
        recursive: bool = False,
        include_deleted: bool = False,
    ) -> List[FileIdentity]:
        record = self._workspace_record(workspace_id, scope)
        provider = WorkspaceStorageProvider(record.get("storage_provider") or "server_managed")
        rows = self.store.list_files(workspace_id, parent_id, recursive, include_deleted)
        return [self.file_identity(row, provider) for row in rows]

    list_workspace_files = list_files

    def get_file_metadata(self, workspace_id: str, file_id: str, scope: ScopeView) -> FileIdentity:
        record = self._file_record(workspace_id, file_id, scope)
        workspace = self.store.get_workspace(workspace_id) or {}
        return self.file_identity(
            record, WorkspaceStorageProvider(workspace.get("storage_provider") or "server_managed")
        )

    def create_folder(self, workspace_id: str, scope: ScopeView, name: str, parent_id: Optional[str] = None) -> FileIdentity:
        workspace = self._workspace_record(workspace_id, scope)
        self._require_writable(workspace)
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

    def create_directory(
        self, workspace_id: str, scope: ScopeView, path: str
    ) -> FileIdentity:
        """Create every missing folder of a relative `path` and return the deepest folder.

        Amendment 02 addition: the agent `workspace_create_directory` tool needs nested folders.
        For folder-backed workspaces the real directories are created too; a read-only or closed
        workspace fails before anything is touched.
        """

        from app.workspace import folder as folderfs
        from app.workspace.contracts import validate_relative_path, InvalidRelativePath

        workspace = self._workspace_record(workspace_id, scope)
        self._require_writable(workspace)
        try:
            rel = validate_relative_path(path)
        except InvalidRelativePath as exc:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, str(exc)) from exc
        provider = WorkspaceStorageProvider(workspace.get("storage_provider") or "server_managed")
        with self.sync_service.workspace_lock(workspace_id):
            existing = self.store.get_file_by_rel_path(workspace_id, rel) if self._is_folder_backed(workspace) else None
            if existing is not None:
                if existing["kind"] != FileKind.FOLDER.value:
                    raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=rel, file_id=existing["file_id"])
                return self.file_identity(existing, provider)
            if self._is_folder_backed(workspace):
                folderfs.ensure_directory(self._root_path(workspace), rel)
                folder_id = self._ensure_folder_rows(workspace_id, rel)
                row = self.store.get_file(workspace_id, folder_id or "")
                if row is None:
                    raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, rel)
                self._emit(workspace_id, row["file_id"], "folder.created", 1, {"path": rel})
                return self.file_identity(row, provider)
            parent_id: Optional[str] = None
            for part in rel.split("/"):
                found = self.store.find_file_by_name(workspace_id, parent_id, part)
                if found is not None:
                    if found["kind"] != FileKind.FOLDER.value:
                        raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=part, file_id=found["file_id"])
                    parent_id = found["file_id"]
                    continue
                parent_id = self.create_folder(workspace_id, scope, part, parent_id).file_id
            return self.get_file_metadata(workspace_id, parent_id, scope)

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
        workspace = self._workspace_record(workspace_id, scope)
        self._require_writable(workspace)
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

        if self._is_folder_backed(workspace):
            # Upload into a folder workspace writes the real file (zero-copy), never a blob.
            rel_dir = self._rel_dir(workspace_id, parent_id)
            rel = f"{rel_dir}/{clean}" if rel_dir else clean
            data = _collect(chunks, self.settings.max_file_bytes)
            row = self._write_external_bytes(workspace, rel, data, mime_type=mime_type)
            return self.file_identity(
                row, WorkspaceStorageProvider(workspace.get("storage_provider") or "server_managed")
            )

        tenant = workspace["tenant_id"]
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

    def _prune_revisions(
        self, tenant_id: str, workspace_id: str, file_id: str, *, origin: str = "blob"
    ) -> None:
        if origin == ContentOrigin.EXTERNAL.value:
            # External content is not retained per revision: the file is the single source.
            keep = 1
        else:
            keep = max(1, int(self.settings.revision_keep))
        revisions = self.store.list_revisions(file_id)
        for row in revisions[keep:]:
            if origin != ContentOrigin.EXTERNAL.value:
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
        workspace = self._workspace_record(workspace_id, scope)
        self._require_open(workspace)
        current_revision = int(record["revision"])
        target = current_revision if revision is None else int(revision)
        if target < 1:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, revision=target)
        row = self.store.get_revision(file_id, target)
        if row is None:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=file_id, revision=target)
        data = self._read_content(workspace, record, target, max_bytes=max_bytes)
        try:
            text: Optional[str] = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        status = RevisionStatus.CURRENT if target == current_revision else RevisionStatus.SUPERSEDED
        return FileContent(
            metadata=self.file_identity(
                record, WorkspaceStorageProvider(workspace.get("storage_provider") or "server_managed")
            ),
            revision=target,
            revision_status=status,
            content_hash=row["content_hash"],
            bytes=data,
            text=text,
        )

    def _read_content(
        self, workspace: Dict[str, Any], record: Dict[str, Any], revision: int, *, max_bytes: int = 0
    ) -> bytes:
        """Bytes of one revision: blob copy, real file in the root, or browser capture."""

        if (record.get("origin") or ContentOrigin.BLOB.value) == ContentOrigin.EXTERNAL.value:
            if self._is_local_browser(workspace):
                captured = self.store.get_capture(record["file_id"], revision) or self.store.get_capture_latest(
                    record["file_id"]
                )
                if captured is None:
                    raise WorkspaceFailure(
                        WorkspaceErrorCode.FILE_NOT_FOUND, file_id=record["file_id"], revision=revision
                    )
                return captured.encode("utf-8")
            root = self._root_path(workspace)
            rel = record.get("rel_path")
            if not rel:
                raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=record["file_id"])
            try:
                data, _digest = folderfs.read_relative(root, rel, max_bytes=max_bytes)
            except folderfs.FolderError as exc:
                raise self._folder_failure(exc) from exc
            return data
        tenant = workspace["tenant_id"]
        try:
            data = self.storage.read_bytes(tenant, workspace["workspace_id"], record["file_id"], revision)
        except StorageError as exc:
            raise WorkspaceFailure(
                WorkspaceErrorCode.FILE_NOT_FOUND, file_id=record["file_id"], revision=revision
            ) from exc
        if max_bytes and len(data) > max_bytes:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_TOO_LARGE, limit_bytes=max_bytes)
        return data

    def read_revision_bytes(
        self, workspace: Dict[str, Any], record: Dict[str, Any], revision: int
    ) -> Tuple[bytes, str]:
        """Content and its actual hash for the indexer; the hash guards against a stale read."""

        origin = record.get("origin") or ContentOrigin.BLOB.value
        if origin == ContentOrigin.EXTERNAL.value and not self._is_local_browser(workspace):
            rel = record.get("rel_path")
            if not rel:
                raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=record["file_id"])
            try:
                return folderfs.read_relative(self._root_path(workspace), rel, max_bytes=self.settings.max_file_bytes)
            except folderfs.FolderError as exc:
                raise self._folder_failure(exc) from exc
        data = self._read_content(workspace, record, revision)
        digest = hashlib.sha256(data).hexdigest()
        return data, "sha256:" + digest

    def iter_file(self, workspace_id: str, file_id: str, scope: ScopeView, revision: Optional[int] = None):
        record = self._file_record(workspace_id, file_id, scope)
        if record["kind"] != FileKind.FILE.value:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_REQUEST, "folder has no content")
        workspace = self._workspace_record(workspace_id, scope)
        self._require_open(workspace)
        target = int(record["revision"]) if revision is None else int(revision)
        if self.store.get_revision(file_id, target) is None:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=file_id, revision=target)
        if (record.get("origin") or ContentOrigin.BLOB.value) == ContentOrigin.EXTERNAL.value:
            if self._is_local_browser(workspace):
                return iter([self._read_content(workspace, record, target)])
            rel = record.get("rel_path") or ""
            try:
                return folderfs.iter_relative(self._root_path(workspace), rel)
            except folderfs.FolderError as exc:
                raise self._folder_failure(exc) from exc
        tenant = workspace["tenant_id"]
        return self.storage.iter_content(tenant, workspace_id, file_id, target)

    def rename_file(self, workspace_id: str, file_id: str, scope: ScopeView, new_name: str, expected_revision: Optional[int] = None) -> FileIdentity:
        workspace = self._workspace_record(workspace_id, scope)
        self._require_writable(workspace)
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
        if self._is_folder_backed(workspace):
            self._rename_on_disk(workspace, record, clean)
        self._emit(workspace_id, file_id, "file.renamed", int(record["revision"]), {"name": clean})
        return self.get_file_metadata(workspace_id, file_id, scope)

    def move_file(self, workspace_id: str, file_id: str, scope: ScopeView, parent_id: Optional[str], expected_revision: Optional[int] = None) -> FileIdentity:
        workspace = self._workspace_record(workspace_id, scope)
        self._require_writable(workspace)
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
        if self._is_folder_backed(workspace):
            rel_dir = self._rel_dir(workspace_id, parent_id)
            rel = f"{rel_dir}/{record['name']}" if rel_dir else record["name"]
            self.store.update_file(workspace_id, file_id, {"rel_path": rel, "synced_at": self.clock()})
            self._move_on_disk(workspace, record, rel)
        self._emit(workspace_id, file_id, "file.moved", int(record["revision"]), {"parent_id": parent_id})
        return self.get_file_metadata(workspace_id, file_id, scope)

    def _rename_on_disk(self, workspace: Dict[str, Any], record: Dict[str, Any], new_name: str) -> None:
        """Keep the real file aligned with a metadata rename (external content only)."""

        old_rel = record.get("rel_path") if record else None
        if not old_rel:
            return
        rel_dir = old_rel.rsplit("/", 1)[0] if "/" in old_rel else ""
        new_rel = f"{rel_dir}/{new_name}" if rel_dir else new_name
        self._move_on_disk(workspace, record, new_rel)

    def _move_on_disk(self, workspace: Dict[str, Any], record: Dict[str, Any], new_rel: str) -> None:
        old_rel = record.get("rel_path")
        if not old_rel or old_rel == new_rel:
            return
        root = self._root_path(workspace)
        try:
            source, _clean = folderfs._secure_join(root, old_rel)
            target, clean = folderfs._secure_join(root, new_rel)
            if not source.exists():
                return
            if clean.count("/"):
                folderfs.ensure_directory(root, clean.rsplit("/", 1)[0])
            source.replace(target)
        except Exception:
            # The watcher reconciles the difference on its next pass.
            return
        self.store.update_file(
            workspace["workspace_id"], record["file_id"], {"rel_path": new_rel, "synced_at": self.clock()}
        )

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
        workspace = self._workspace_record(workspace_id, scope)
        self._require_writable(workspace)
        with self.sync_service.workspace_lock(workspace_id):
            return self._delete_file_locked(workspace, workspace_id, file_id, scope)

    def _delete_file_locked(
        self, workspace: Dict[str, Any], workspace_id: str, file_id: str, scope: ScopeView
    ) -> Dict[str, Any]:
        record = self._file_record(workspace_id, file_id, scope)
        tenant = workspace["tenant_id"]
        targets = [record]
        if record["kind"] == FileKind.FOLDER.value:
            for descendant_id in self.store.list_file_ids_under(workspace_id, file_id):
                row = self.store.get_file(workspace_id, descendant_id)
                if row is not None:
                    targets.append(row)
        stamp = self.clock()
        removed_blobs = 0
        removed_source = 0
        for target in targets:
            self.store.cancel_jobs_for_file(target["file_id"], stamp, reason="file_deleted")
            self.store.delete_chunks(target["file_id"])
            self.store.delete_revisions(target["file_id"])
            if (target.get("origin") or "blob") == ContentOrigin.EXTERNAL.value:
                # A file the user deletes in the explorer is a real file in the root; a folder is
                # only removed when it is empty (untracked content is never destroyed).
                rel = target.get("rel_path")
                if rel:
                    try:
                        if target["kind"] == FileKind.FILE.value:
                            removed_source += folderfs.remove_relative(self._root_path(workspace), rel)
                        else:
                            folderfs.try_remove_empty_directory(self._root_path(workspace), rel)
                    except Exception:
                        pass
            else:
                removed_blobs += self.storage.delete_file(tenant, workspace_id, target["file_id"])
            self.store.soft_delete_file(workspace_id, target["file_id"], stamp)
            self._emit(workspace_id, target["file_id"], "file.deleted", int(target["revision"]))
        return {
            "file_id": file_id,
            "deleted": True,
            "removed_files": len(targets),
            "removed_blobs": removed_blobs,
            "removed_source_files": removed_source,
        }

    # ------------------------------------------------------------------ write contract
    def write_file(
        self, workspace_id: str, scope: ScopeView, request: WorkspaceWriteRequest
    ) -> WorkspaceWriteResult:
        """Canvas / Coding Agent common write: one call, every provider (§A1.5).""" 

        workspace = self._workspace_record(workspace_id, scope)
        self._require_writable(workspace)
        try:
            rel = validate_relative_path(request.path)
        except InvalidRelativePath as exc:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, str(exc)) from exc
        provider = WorkspaceStorageProvider(workspace.get("storage_provider") or "server_managed")

        if provider is WorkspaceStorageProvider.LOCAL_BROWSER:
            return WorkspaceWriteResult(
                workspace_id=workspace_id,
                path=rel,
                storage_provider=provider,
                content_origin=ContentOrigin.EXTERNAL,
                client_write=ClientWriteDirective(
                    client_handle_id=workspace.get("client_handle_id"),
                    relative_path=rel,
                ),
            )

        data = _write_payload(request)
        if self._extension_blocked(rel.rsplit("/", 1)[-1]):
            raise WorkspaceFailure(WorkspaceErrorCode.UNSUPPORTED_MEDIA_TYPE, name=rel)
        existing = self.store.get_file_by_rel_path(workspace_id, rel) if self._is_folder_backed(workspace) else None
        if request.expected_content_hash is not None:
            current_hash = existing.get("content_hash") if existing else None
            if current_hash != request.expected_content_hash:
                raise WorkspaceFailure(
                    WorkspaceErrorCode.CONTENT_CONFLICT,
                    expected_content_hash=request.expected_content_hash,
                    current_content_hash=current_hash,
                )
        if existing is not None and not request.overwrite:
            raise WorkspaceFailure(WorkspaceErrorCode.FILE_EXISTS, name=rel, file_id=existing["file_id"])

        if self._is_folder_backed(workspace):
            row = self._write_external_bytes(
                workspace,
                rel,
                data,
                mime_type=request.mime_type,
                create_parents=request.create_parents,
                overwrite=request.overwrite,
            )
            identity = self.file_identity(row, provider)
        else:
            rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
            parent_id = self._ensure_folder_rows(workspace_id, rel_dir) if rel_dir else None
            identity = self.create_file(
                workspace_id,
                scope,
                rel.rsplit("/", 1)[-1],
                data,
                parent_id=parent_id,
                overwrite=request.overwrite,
            )
        origin = ContentOrigin.EXTERNAL if self._is_folder_backed(workspace) else ContentOrigin.BLOB
        return WorkspaceWriteResult(
            workspace_id=workspace_id,
            path=rel,
            file=identity,
            revision=identity.revision,
            content_hash=identity.content_hash,
            written_bytes=identity.size_bytes,
            storage_provider=provider,
            content_origin=origin,
        )

    def _write_external_bytes(
        self,
        workspace: Dict[str, Any],
        rel: str,
        data: bytes,
        *,
        mime_type: Optional[str] = None,
        create_parents: bool = True,
        overwrite: bool = True,
    ) -> Dict[str, Any]:
        # The watcher thread may be scanning the same root; the per-workspace lock keeps the
        # disk write and its metadata row a single step.
        with self.sync_service.workspace_lock(workspace["workspace_id"]):
            return self._write_external_bytes_locked(
                workspace,
                rel,
                data,
                mime_type=mime_type,
                create_parents=create_parents,
                overwrite=overwrite,
            )

    def _write_external_bytes_locked(
        self,
        workspace: Dict[str, Any],
        rel: str,
        data: bytes,
        *,
        mime_type: Optional[str] = None,
        create_parents: bool = True,
        overwrite: bool = True,
    ) -> Dict[str, Any]:
        workspace_id = workspace["workspace_id"]
        root = self._root_path(workspace)
        existing = self.store.get_file_by_rel_path(workspace_id, rel)
        if existing is not None and existing["kind"] != FileKind.FILE.value:
            raise WorkspaceFailure(WorkspaceErrorCode.INVALID_PATH, "target is a folder")
        if existing is None and (
            self.store.count_files(workspace_id, kind=FileKind.FILE.value) >= self.settings.max_files_per_workspace
        ):
            raise WorkspaceFailure(WorkspaceErrorCode.QUOTA_EXCEEDED, "file limit reached")
        try:
            size, digest = folderfs.write_relative(
                root,
                rel,
                data,
                create_parents=create_parents,
                overwrite=overwrite,
                max_bytes=self.settings.max_file_bytes,
            )
        except folderfs.FolderError as exc:
            raise self._folder_failure(exc) from exc
        stamp = self.clock()
        parent_id = self._ensure_folder_rows(workspace_id, rel.rsplit("/", 1)[0] if "/" in rel else "")
        name = rel.rsplit("/", 1)[-1]
        resolved_mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        if existing is None:
            file_id = _new_id("wf")
            self.store.insert_file(
                {
                    "file_id": file_id,
                    "workspace_id": workspace_id,
                    "parent_id": parent_id,
                    "kind": FileKind.FILE.value,
                    "name": name,
                    "mime_type": resolved_mime,
                    "size_bytes": size,
                    "content_hash": digest,
                    "revision": 1,
                    "index_status": IndexState.PENDING.value,
                    "index_error": None,
                    "index_updated_at": stamp,
                    "created_at": stamp,
                    "updated_at": stamp,
                    "deleted_at": None,
                    "origin": ContentOrigin.EXTERNAL.value,
                    "rel_path": rel,
                    "ext_mtime": None,
                    "synced_at": stamp,
                }
            )
            self.store.add_revision(
                {
                    "file_id": file_id,
                    "workspace_id": workspace_id,
                    "revision": 1,
                    "content_hash": digest,
                    "size_bytes": size,
                    "blob_key": "external",
                    "parser_version": None,
                    "created_at": stamp,
                }
            )
            self._emit(workspace_id, file_id, "file.created", 1, {"path": rel, "writer": "workspace_write_contract"})
        else:
            file_id = existing["file_id"]
            revision = int(existing["revision"]) + 1
            self.store.add_revision(
                {
                    "file_id": file_id,
                    "workspace_id": workspace_id,
                    "revision": revision,
                    "content_hash": digest,
                    "size_bytes": size,
                    "blob_key": "external",
                    "parser_version": None,
                    "created_at": stamp,
                }
            )
            self.store.cancel_jobs_for_file(file_id, stamp, reason="file_changed")
            self.store.delete_chunks(file_id)
            self.store.update_file(
                workspace_id,
                file_id,
                {
                    "parent_id": parent_id,
                    "name": name,
                    "rel_path": rel,
                    "mime_type": resolved_mime,
                    "size_bytes": size,
                    "content_hash": digest,
                    "revision": revision,
                    "index_status": IndexState.PENDING.value,
                    "index_error": None,
                    "index_updated_at": stamp,
                    "ext_mtime": None,
                    "synced_at": stamp,
                    "updated_at": stamp,
                },
            )
            self._prune_revisions(workspace["tenant_id"], workspace_id, file_id, origin="external")
            self._emit(workspace_id, file_id, "file.updated", revision, {"path": rel, "writer": "workspace_write_contract"})
        self._notify_file(workspace_id, file_id)
        return self.store.get_file(workspace_id, file_id) or existing or {}

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
        """Restart recovery: requeue interrupted jobs, sweep temp files, drop orphan blobs.

        Folder-backed workspaces are re-validated: a root that vanished while the server was down
        closes its workspace instead of leaving a watcher pointed at nothing.
        """

        requeued = self.store.stale_indexing_jobs(self.clock())
        temp_removed = self.storage.sweep_temp(older_than_seconds=0)
        closed = 0
        for workspace in self.store.list_all_workspaces():
            if workspace.get("status") == "deleted" or workspace.get("lifecycle_state") != "open":
                continue
            if not self._is_folder_backed(workspace):
                continue
            root = Path(workspace.get("root_path") or "")
            if root.exists() and root.is_dir():
                continue
            stamp = self.clock()
            self.store.update_workspace(
                workspace["workspace_id"],
                {"lifecycle_state": WorkspaceLifecycleState.CLOSED.value, "closed_at": stamp, "updated_at": stamp},
            )
            self.store.upsert_watch_state(
                workspace["workspace_id"], {"watching": 0, "last_error": "root_unavailable", "updated_at": stamp}
            )
            self._emit(workspace["workspace_id"], None, "workspace.closed", 0, {"reason": "root_unavailable"})
            closed += 1
        known: List[Path] = []
        for workspace in self.store.list_all_workspaces():
            for row in self.store.list_files(workspace["workspace_id"], recursive=True, include_deleted=True):
                if (row.get("origin") or "blob") == ContentOrigin.EXTERNAL.value:
                    continue
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
        return {
            "requeued_jobs": requeued,
            "temp_removed": temp_removed,
            "orphan_blobs_removed": orphans,
            "workspaces_closed": closed,
        }

    def workspace_usage(self, workspace_id: str, scope: ScopeView) -> Dict[str, Any]:
        record = self._workspace_record(workspace_id, scope)
        provider = WorkspaceStorageProvider(record.get("storage_provider") or "server_managed")
        if self._is_folder_backed(record) or provider is WorkspaceStorageProvider.LOCAL_BROWSER:
            # Zero-copy: the workspace holds metadata, not file bodies.
            return {
                "usage_bytes": 0,
                "limit_bytes": 0,
                "files": self.store.count_files(workspace_id, kind=FileKind.FILE.value),
                "file_limit": self.settings.max_files_per_workspace,
                "zero_copy": True,
                "storage_provider": provider.value,
            }
        return {
            "usage_bytes": self.storage.workspace_usage(record["tenant_id"], workspace_id),
            "limit_bytes": self.settings.max_workspace_bytes,
            "files": self.store.count_files(workspace_id, kind=FileKind.FILE.value),
            "file_limit": self.settings.max_files_per_workspace,
            "zero_copy": False,
            "storage_provider": provider.value,
        }
