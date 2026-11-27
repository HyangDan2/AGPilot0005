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

    # -- helpers -----------------------------------------------------------------
    def seed_workspace(
        self,
        name: str = "Mock workspace",
        *,
        owner_id: str = "mock_user",
        tenant_id: str = "mock_tenant",
        service_id: str = "hd2ai-admin",
    ) -> WorkspaceIdentity:
        stamp = _now()
        identity = WorkspaceIdentity(
            workspace_id=self._ids.next("ws"),
            tenant_id=tenant_id,
            owner_id=owner_id,
            service_id=service_id,
            name=name,
            description="",
            status=WorkspaceStatus.ACTIVE,
            revision=1,
            created_at=stamp,
            updated_at=stamp,
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
        self._visible(workspace_id, scope)
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
            "content": content,
            "revisions": {1: content},
        }
        self._files[str(record["file_id"])] = record
        return self._identity(record)

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

    @router.get("/workspaces/{workspace_id}/artifact-references")
    def list_artifacts(workspace_id: str, kind: Optional[str] = None) -> dict:
        items = mock.list_artifact_references(workspace_id, kind=kind)
        return {"items": [item.model_dump(mode="json") for item in items], "total": len(items)}

    @router.post("/workspaces/{workspace_id}/artifact-references", status_code=201)
    def add_artifact(workspace_id: str, body: ArtifactReferenceInput) -> dict:
        return mock.register_artifact_reference(workspace_id, ref=body).model_dump(mode="json")

    return router
