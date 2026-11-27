"""In-memory Mock Workspace Provider so the Canvas engineer can work without the storage engine.

The mock implements the contract §2 provider interface and a small §3 REST subset that the
preview app mounts under `/api/v1/workspaces/_mock`. It is never mounted in production and
holds no database, no storage root and no authentication state.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from fastapi import APIRouter, Body, HTTPException, Response

from app.workspace.contracts import (
    AccessLevel,
    AccessResult,
    ArtifactReference,
    ArtifactReferenceInput,
    ClientWriteDirective,
    ContentOrigin,
    FileContent,
    FileIdentity,
    FileKind,
    IndexState,
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
    WorkspaceWriteRequest,
    WorkspaceWriteResult,
    validate_relative_path,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Seq:
    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def next(self, prefix: str) -> str:
        return f"{prefix}_{next(self._counter):012x}"


class MockWorkspaceProvider:
    """In-memory implementation of the shared provider interface."""

    def __init__(self) -> None:
        self._ids = _Seq()
        self._workspaces: Dict[str, WorkspaceIdentity] = {}
        self._files: Dict[str, Dict[str, object]] = {}
        self._artifacts: Dict[str, ArtifactReference] = {}
        self._roots: Dict[str, WorkspaceRootInfo] = {}
        self._sync_counts: Dict[str, Dict[str, int]] = {}

    # -- helpers -----------------------------------------------------------------
    def seed_workspace(
        self,
        name: str = "Mock workspace",
        *,
        owner_id: str = "mock_user",
        tenant_id: str = "mock_tenant",
        service_id: str = "hd2ai-admin",
        storage_provider: WorkspaceStorageProvider = WorkspaceStorageProvider.SERVER_MANAGED,
        display_path: str = "",
        read_only: bool = False,
    ) -> WorkspaceIdentity:
        stamp = _now()
        workspace_id = self._ids.next("ws")
        self._roots[workspace_id] = WorkspaceRootInfo(
            storage_provider=storage_provider,
            display_path=display_path,
            read_only=read_only,
            zero_copy=storage_provider is not WorkspaceStorageProvider.SERVER_MANAGED,
            server_side_path_access=storage_provider is not WorkspaceStorageProvider.LOCAL_BROWSER,
            client_handle_id="mock-handle" if storage_provider is WorkspaceStorageProvider.LOCAL_BROWSER else None,
            opened_at=stamp,
        )
        identity = WorkspaceIdentity(
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            service_id=service_id,
            name=name,
            description="",
            status=WorkspaceStatus.ACTIVE,
            revision=1,
            created_at=stamp,
            updated_at=stamp,
            lifecycle_state=WorkspaceLifecycleState.OPEN,
            root=self._roots[workspace_id],
        )
        self._workspaces[identity.workspace_id] = identity
        return identity

    def _visible(self, workspace_id: str, scope: Optional[ScopeView]) -> WorkspaceIdentity:
        workspace = self._workspaces.get(workspace_id)
        if workspace is None or workspace.status is WorkspaceStatus.DELETED:
            raise _error(WorkspaceErrorCode.WORKSPACE_NOT_FOUND, workspace_id=workspace_id)
        if scope is not None and (
            workspace.tenant_id != scope.tenant_id or workspace.owner_id != scope.owner_id
        ):
            # Hide existence exactly like the real engine.
            raise _error(WorkspaceErrorCode.WORKSPACE_NOT_FOUND, workspace_id=workspace_id)
        return workspace

    def _open(self, workspace_id: str, scope: Optional[ScopeView]) -> WorkspaceIdentity:
        workspace = self._visible(workspace_id, scope)
        if workspace.lifecycle_state is not WorkspaceLifecycleState.OPEN:
            raise _error(WorkspaceErrorCode.WORKSPACE_CLOSED, workspace_id=workspace_id)
        return workspace

    def _file(self, workspace_id: str, file_id: str) -> Dict[str, object]:
        record = self._files.get(file_id)
        if record is None or record["workspace_id"] != workspace_id:
            raise _error(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=file_id)
        return record

    @staticmethod
    def _identity(record: Dict[str, object]) -> FileIdentity:
        return FileIdentity(**{k: v for k, v in record.items() if k not in {"content", "revisions"}})

    # -- contract §2 -------------------------------------------------------------
    def get_workspace(self, workspace_id: str, scope: Optional[ScopeView] = None) -> WorkspaceIdentity:
        return self._visible(workspace_id, scope)

    # -- Amendment 01 lifecycle ---------------------------------------------------
    def open_folder(
        self,
        scope: ScopeView,
        path: str,
        *,
        name: Optional[str] = None,
        read_only: bool = False,
        root_id: Optional[str] = None,
    ) -> WorkspaceIdentity:
        if not path:
            raise _error(WorkspaceErrorCode.INVALID_PATH, path=path)
        return self.seed_workspace(
            name or "Mock folder workspace",
            owner_id=scope.owner_id,
            tenant_id=scope.tenant_id,
            service_id=scope.service_id,
            storage_provider=WorkspaceStorageProvider.SERVER_MOUNTED_FOLDER,
            display_path=path,
            read_only=read_only,
        )

    def new_workspace(
        self,
        scope: ScopeView,
        name: str,
        *,
        description: str = "",
        parent_path: Optional[str] = None,
        root_id: Optional[str] = None,
    ) -> WorkspaceIdentity:
        workspace = self.seed_workspace(
            name,
            owner_id=scope.owner_id,
            tenant_id=scope.tenant_id,
            service_id=scope.service_id,
            storage_provider=WorkspaceStorageProvider.SERVER_MANAGED,
            display_path=f"{parent_path or '/mock/workspaces'}/{name}",
        )
        workspace.description = description
        workspace.root = workspace.root.model_copy(update={"zero_copy": True, "root_id": root_id})
        self._workspaces[workspace.workspace_id] = workspace
        return workspace

    def _set_lifecycle(self, workspace_id: str, state: WorkspaceLifecycleState) -> WorkspaceIdentity:
        workspace = self._workspaces[workspace_id]
        stamp = _now()
        updated = workspace.model_copy(
            update={
                "lifecycle_state": state,
                "status": WorkspaceStatus.DELETED if state is WorkspaceLifecycleState.DELETED else workspace.status,
                "revision": workspace.revision + 1,
                "updated_at": stamp,
                "closed_at": stamp if state is WorkspaceLifecycleState.CLOSED else None,
            }
        )
        self._workspaces[workspace_id] = updated
        return updated

    def close_workspace(
        self, workspace_id: str, scope: Optional[ScopeView] = None, reason: str = ""
    ) -> WorkspaceIdentity:
        workspace = self._open(workspace_id, scope)
        return self._set_lifecycle(workspace.workspace_id, WorkspaceLifecycleState.CLOSED)

    def reopen_workspace(self, workspace_id: str, scope: Optional[ScopeView] = None) -> WorkspaceIdentity:
        workspace = self._visible(workspace_id, scope)
        if workspace.lifecycle_state is WorkspaceLifecycleState.DELETED:
            raise _error(WorkspaceErrorCode.WORKSPACE_NOT_FOUND, workspace_id=workspace_id)
        return self._set_lifecycle(workspace_id, WorkspaceLifecycleState.OPEN)

    def disconnect_workspace(
        self,
        workspace_id: str,
        scope: Optional[ScopeView] = None,
        delete_source_files: bool = False,
    ) -> Dict[str, object]:
        self._visible(workspace_id, scope)
        if delete_source_files:
            raise _error(WorkspaceErrorCode.SOURCE_DELETE_NOT_PERMITTED, workspace_id=workspace_id)
        removed = sum(1 for record in self._files.values() if record["workspace_id"] == workspace_id)
        for file_id in [key for key, record in self._files.items() if record["workspace_id"] == workspace_id]:
            self._files.pop(file_id, None)
        self._set_lifecycle(workspace_id, WorkspaceLifecycleState.DELETED)
        return {
            "workspace_id": workspace_id,
            "lifecycle_state": "deleted",
            "purged_files": removed,
            "source_files_deleted": False,
        }

    def sync_workspace(
        self, workspace_id: str, scope: Optional[ScopeView] = None, full: bool = False
    ) -> WorkspaceSyncResult:
        workspace = self._open(workspace_id, scope)
        stamp = _now()
        counts = self._sync_counts.get(workspace_id, {})
        self._sync_counts[workspace_id] = {}
        files_total = sum(
            1 for record in self._files.values() if record["workspace_id"] == workspace_id and record["kind"] == FileKind.FILE
        )
        return WorkspaceSyncResult(
            workspace_id=workspace.workspace_id,
            full=full,
            scanned=files_total,
            created=counts.get("created", 0),
            modified=counts.get("modified", 0),
            moved=counts.get("moved", 0),
            deleted=counts.get("deleted", 0),
            unchanged=files_total,
            started_at=stamp,
            finished_at=stamp,
        )

    def write_file(
        self, workspace_id: str, scope: Optional[ScopeView], request: WorkspaceWriteRequest
    ) -> WorkspaceWriteResult:
        workspace = self._open(workspace_id, scope)
        if workspace.root.read_only:
            raise _error(WorkspaceErrorCode.READ_ONLY_WORKSPACE, workspace_id=workspace_id)
        try:
            relative = validate_relative_path(request.path)
        except InvalidRelativePath as exc:
            raise _error(WorkspaceErrorCode.INVALID_PATH, reason=str(exc)) from exc
        text = request.content or ""
        if workspace.root.storage_provider is WorkspaceStorageProvider.LOCAL_BROWSER:
            return WorkspaceWriteResult(
                workspace_id=workspace_id,
                path=relative,
                storage_provider=workspace.root.storage_provider,
                content_origin=ContentOrigin.EXTERNAL,
                client_write=ClientWriteDirective(
                    client_handle_id=workspace.root.client_handle_id,
                    relative_path=relative,
                ),
            )
        parts = relative.split("/")
        parent_id = self._ensure_folders(workspace_id, parts[:-1])
        identity = self.create_file(
            workspace_id,
            scope,
            name=parts[-1],
            content=text.encode("utf-8"),
            parent_id=parent_id,
            overwrite=request.overwrite,
        )
        return WorkspaceWriteResult(
            workspace_id=workspace_id,
            path=relative,
            file=identity,
            revision=identity.revision,
            content_hash=identity.content_hash,
            written_bytes=identity.size_bytes,
            storage_provider=workspace.root.storage_provider,
            content_origin=(
                ContentOrigin.EXTERNAL
                if workspace.root.storage_provider is WorkspaceStorageProvider.SERVER_MOUNTED_FOLDER
                else ContentOrigin.BLOB
            ),
        )

    def _ensure_folders(self, workspace_id: str, parts: Sequence[str]) -> Optional[str]:
        parent_id: Optional[str] = None
        for part in parts:
            existing = next(
                (
                    record
                    for record in self._files.values()
                    if record["workspace_id"] == workspace_id
                    and record.get("parent_id") == parent_id
                    and record["name"] == part
                    and record["kind"] == FileKind.FOLDER
                    and not record.get("deleted_at")
                ),
                None,
            )
            if existing is not None:
                parent_id = str(existing["file_id"])
                continue
            stamp = _now()
            record: Dict[str, object] = {
                "file_id": self._ids.next("wf"),
                "workspace_id": workspace_id,
                "parent_id": parent_id,
                "kind": FileKind.FOLDER,
                "name": part,
                "path": f"/{part}",
                "mime_type": None,
                "size_bytes": 0,
                "content_hash": None,
                "revision": 1,
                "index_status": None,
                "created_at": stamp,
                "updated_at": stamp,
            }
            self._files[str(record["file_id"])] = record
            parent_id = str(record["file_id"])
        return parent_id

    def check_workspace_access(self, workspace_id: str, scope: Optional[ScopeView] = None) -> AccessResult:
        try:
            self._visible(workspace_id, scope)
        except HTTPException:
            return AccessResult(workspace_id=workspace_id, level=AccessLevel.NONE, reason="not_visible")
        return AccessResult(workspace_id=workspace_id, level=AccessLevel.ADMIN, reason="owner")

    def list_workspace_files(
        self,
        workspace_id: str,
        scope: Optional[ScopeView] = None,
        parent_id: Optional[str] = None,
        recursive: bool = False,
    ) -> Sequence[FileIdentity]:
        self._visible(workspace_id, scope)
        result: List[FileIdentity] = []
        for record in self._files.values():
            if record["workspace_id"] != workspace_id or record.get("deleted_at"):
                continue
            if not recursive and record["parent_id"] != parent_id:
                continue
            result.append(self._identity(record))
        return sorted(result, key=lambda f: (f.kind is FileKind.FILE, f.name))

    def get_file_metadata(
        self, workspace_id: str, file_id: str, scope: Optional[ScopeView] = None
    ) -> FileIdentity:
        self._visible(workspace_id, scope)
        return self._identity(self._file(workspace_id, file_id))

    def read_file(
        self,
        workspace_id: str,
        file_id: str,
        scope: Optional[ScopeView] = None,
        revision: Optional[int] = None,
    ) -> FileContent:
        self._visible(workspace_id, scope)
        record = self._file(workspace_id, file_id)
        revisions: Dict[int, bytes] = record["revisions"]  # type: ignore[assignment]
        current = int(record["revision"])
        target = current if revision is None else int(revision)
        if target not in revisions:
            raise _error(WorkspaceErrorCode.FILE_NOT_FOUND, file_id=file_id, revision=target)
        payload = revisions[target]
        try:
            text: Optional[str] = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        return FileContent(
            metadata=self._identity(record),
            revision=target,
            revision_status=RevisionStatus.CURRENT if target == current else RevisionStatus.SUPERSEDED,
            content_hash=str(record["content_hash"]),
            bytes=payload,
            text=text,
        )

    def create_file(
        self,
        workspace_id: str,
        scope: Optional[ScopeView] = None,
        name: str = "",
        content: bytes = b"",
        parent_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
        overwrite: bool = False,
    ) -> FileIdentity:
        workspace = self._open(workspace_id, scope)
        if workspace.root.read_only:
            raise _error(WorkspaceErrorCode.READ_ONLY_WORKSPACE, workspace_id=workspace_id)
        if not name or "/" in name or name in {".", ".."}:
            raise _error(WorkspaceErrorCode.INVALID_PATH, name=name)
        existing = next(
            (
                record
                for record in self._files.values()
                if record["workspace_id"] == workspace_id
                and record["parent_id"] == parent_id
                and record["name"] == name
                and not record.get("deleted_at")
            ),
            None,
        )
        relative = self._relative_path(parent_id, f"/{name}")
        stamp = _now()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if existing is not None:
            if not overwrite:
                raise _error(WorkspaceErrorCode.FILE_EXISTS, name=name)
            if expected_revision is not None and int(existing["revision"]) != expected_revision:
                raise _error(
                    WorkspaceErrorCode.REVISION_CONFLICT,
                    expected_revision=expected_revision,
                    current_revision=existing["revision"],
                )
            existing["revision"] = int(existing["revision"]) + 1
            existing["content_hash"] = digest
            existing["size_bytes"] = len(content)
            existing["index_status"] = IndexState.PENDING
            existing["updated_at"] = stamp
            existing["revisions"][int(existing["revision"])] = content  # type: ignore[index]
            existing["content"] = content
            return self._identity(existing)

        record: Dict[str, object] = {
            "file_id": self._ids.next("wf"),
            "workspace_id": workspace_id,
            "parent_id": parent_id,
            "kind": FileKind.FILE,
            "name": name,
            "path": f"/{name}",
            "mime_type": "text/plain",
            "size_bytes": len(content),
            "content_hash": digest,
            "revision": 1,
            "index_status": IndexState.READY,
            "created_at": stamp,
            "updated_at": stamp,
            "storage_provider": workspace.root.storage_provider,
            "content_origin": (
                ContentOrigin.EXTERNAL
                if workspace.root.zero_copy
                else ContentOrigin.BLOB
            ),
            "relative_path": relative,
            "content": content,
            "revisions": {1: content},
        }
        self._files[str(record["file_id"])] = record
        return self._identity(record)

    def _relative_path(self, parent_id: Optional[str], name_or_path: str) -> str:
        """Join parent names into a relative path for mock records (bounded walk)."""

        clean = name_or_path.lstrip("/")
        parts = [clean]
        cursor = parent_id
        guard = 0
        while cursor and guard < 32:
            parent = next((r for key, r in self._files.items() if key == cursor), None)
            if parent is None:
                break
            parts.append(str(parent["name"]))
            cursor = parent.get("parent_id")  # type: ignore[assignment]
            guard += 1
        return "/".join(reversed(parts))

    def register_artifact_reference(
        self,
        workspace_id: str,
        scope: Optional[ScopeView] = None,
        ref: Optional[ArtifactReferenceInput] = None,
    ) -> ArtifactReference:
        self._visible(workspace_id, scope)
        if ref is None:
            raise _error(WorkspaceErrorCode.INVALID_REQUEST, reason="missing_reference")
        if ref.file_id is not None:
            self._file(workspace_id, ref.file_id)
        record = ArtifactReference(
            artifact_ref_id=self._ids.next("wref"),
            workspace_id=workspace_id,
            artifact_id=ref.artifact_id,
            kind=ref.kind,
            file_id=ref.file_id,
            metadata=copy.deepcopy(ref.metadata),
            created_at=_now(),
        )
        self._artifacts[record.artifact_ref_id] = record
        return record

    def list_artifact_references(
        self, workspace_id: str, scope: Optional[ScopeView] = None, kind: Optional[str] = None
    ) -> Sequence[ArtifactReference]:
        self._visible(workspace_id, scope)
        return [
            ref
            for ref in self._artifacts.values()
            if ref.workspace_id == workspace_id and (kind is None or ref.kind == kind)
        ]


def _error(code: WorkspaceErrorCode, **context: object) -> HTTPException:
    error = WorkspaceError(code=code, message=code.value, context={k: v for k, v in context.items()})
    return HTTPException(status_code=error.http_status, detail=error.model_dump(mode="json"))


MOCK_WORKSPACES: Dict[str, MockWorkspaceProvider] = {}


def default_mock_provider() -> MockWorkspaceProvider:
    """A shared process-local mock instance with one seeded workspace."""

    provider = MOCK_WORKSPACES.get("default")
    if provider is None:
        provider = MockWorkspaceProvider()
        provider.seed_workspace("Canvas mock workspace")
        MOCK_WORKSPACES["default"] = provider
    return provider


def build_mock_router(provider: Optional[MockWorkspaceProvider] = None) -> APIRouter:
    """REST subset of contract §3 backed by the mock provider (preview only)."""

    mock = provider or default_mock_provider()
    router = APIRouter(prefix="/_mock", tags=["workspace-mock"])

    @router.get("/workspaces")
    def list_workspaces() -> dict:
        return {"items": [ws.model_dump(mode="json") for ws in mock._workspaces.values()], "total": len(mock._workspaces)}

    @router.post("/workspaces")
    def create_workspace(body: dict = Body(default_factory=dict)) -> dict:
        name = str(body.get("name") or "Mock workspace")[:120]
        return mock.seed_workspace(name).model_dump(mode="json")

    @router.get("/workspaces/{workspace_id}")
    def get_workspace(workspace_id: str) -> dict:
        return mock.get_workspace(workspace_id).model_dump(mode="json")

    @router.get("/workspaces/{workspace_id}/access")
    def access(workspace_id: str) -> dict:
        return mock.check_workspace_access(workspace_id).model_dump(mode="json")

    @router.get("/workspaces/{workspace_id}/files")
    def list_files(workspace_id: str, parent_id: Optional[str] = None, recursive: bool = False) -> dict:
        items = mock.list_workspace_files(workspace_id, parent_id=parent_id, recursive=recursive)
        return {"items": [item.model_dump(mode="json") for item in items], "total": len(items)}

    @router.get("/workspaces/{workspace_id}/files/{file_id}")
    def file_metadata(workspace_id: str, file_id: str) -> dict:
        return mock.get_file_metadata(workspace_id, file_id).model_dump(mode="json")

    @router.get("/workspaces/{workspace_id}/files/{file_id}/content")
    def read_file(workspace_id: str, file_id: str, revision: Optional[int] = None) -> Response:
        content = mock.read_file(workspace_id, file_id, revision=revision)
        return Response(content=content.bytes, media_type=content.metadata.mime_type or "application/octet-stream")

    @router.post("/workspaces/{workspace_id}/files", status_code=201)
    def create_file(workspace_id: str, body: dict = Body(default_factory=dict)) -> dict:
        payload = body.get("content", "")
        raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        identity = mock.create_file(
            workspace_id,
            name=str(body.get("name") or "untitled.txt"),
            content=raw,
            parent_id=body.get("parent_id"),
        )
        return identity.model_dump(mode="json")

    @router.post("/workspaces/open-folder", status_code=201)
    def open_folder(body: dict = Body(default_factory=dict)) -> dict:
        scope = _mock_scope()
        return mock.open_folder(
            scope,
            str(body.get("path") or "/mock/folder"),
            name=body.get("name"),
            read_only=bool(body.get("read_only") or False),
            root_id=body.get("root_id"),
        ).model_dump(mode="json")

    @router.post("/workspaces/new", status_code=201)
    def new_workspace(body: dict = Body(default_factory=dict)) -> dict:
        scope = _mock_scope()
        return mock.new_workspace(
            scope,
            str(body.get("name") or "Mock workspace"),
            description=str(body.get("description") or ""),
            parent_path=body.get("parent_path"),
            root_id=body.get("root_id"),
        ).model_dump(mode="json")

    @router.post("/workspaces/{workspace_id}/close")
    def close_workspace(workspace_id: str, body: dict = Body(default_factory=dict)) -> dict:
        return mock.close_workspace(workspace_id, _mock_scope(), reason=str(body.get("reason") or "")).model_dump(
            mode="json"
        )

    @router.post("/workspaces/{workspace_id}/reopen")
    def reopen_workspace(workspace_id: str) -> dict:
        return mock.reopen_workspace(workspace_id, _mock_scope()).model_dump(mode="json")

    @router.post("/workspaces/{workspace_id}/disconnect")
    def disconnect_workspace(workspace_id: str, body: dict = Body(default_factory=dict)) -> dict:
        return mock.disconnect_workspace(
            workspace_id,
            _mock_scope(),
            delete_source_files=bool(body.get("delete_source_files") or False),
        )

    @router.post("/workspaces/{workspace_id}/sync")
    def sync_workspace(workspace_id: str, body: dict = Body(default_factory=dict)) -> dict:
        return mock.sync_workspace(workspace_id, _mock_scope(), full=bool(body.get("full") or False)).model_dump(
            mode="json"
        )

    @router.put("/workspaces/{workspace_id}/file")
    def write_file(workspace_id: str, body: WorkspaceWriteRequest) -> dict:
        return mock.write_file(workspace_id, _mock_scope(), body).model_dump(mode="json")

    @router.get("/workspaces/{workspace_id}/root")
    def workspace_root(workspace_id: str) -> dict:
        return mock.get_workspace(workspace_id).root.model_dump(mode="json")

    @router.get("/workspaces/{workspace_id}/artifact-references")
    def list_artifacts(workspace_id: str, kind: Optional[str] = None) -> dict:
        items = mock.list_artifact_references(workspace_id, kind=kind)
        return {"items": [item.model_dump(mode="json") for item in items], "total": len(items)}

    @router.post("/workspaces/{workspace_id}/artifact-references", status_code=201)
    def add_artifact(workspace_id: str, body: ArtifactReferenceInput) -> dict:
        return mock.register_artifact_reference(workspace_id, ref=body).model_dump(mode="json")

    return router


def _mock_scope() -> ScopeView:
    """Scope of the preview mock: one fixed owner so the UI sees its own workspaces only."""

    class _Scope:
        tenant_id = "mock_tenant"
        owner_id = "mock_user"
        service_id = "hd2ai-admin"

    return _Scope()  # type: ignore[return-value]
