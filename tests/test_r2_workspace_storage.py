"""CP-W01: workspace manager, file storage and storage security."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import WorkspaceErrorCode
from app.workspace.manager import WorkspaceFailure, WorkspaceManager
from app.workspace.storage import StorageError, WorkspaceStorage
from app.workspace.store import WorkspaceStore


class Scope:
    def __init__(self, tenant_id="tenant_a", owner_id="owner_a", service_id="hd2ai-admin"):
        self.tenant_id = tenant_id
        self.owner_id = owner_id
        self.service_id = service_id


@pytest.fixture()
def env(tmp_path):
    settings = WorkspaceSettings.for_testing(tmp_path)
    store = WorkspaceStore(settings.resolved_db_path())
    storage = WorkspaceStorage(settings.resolved_storage_root(), max_file_bytes=settings.max_file_bytes)
    manager = WorkspaceManager(store, storage, settings)
    return manager, store, storage, settings


def _upload(manager, scope, workspace_id, name="doc.txt", content=b"hello", **kwargs):
    return manager.upload_file(workspace_id, scope, name, [content], **kwargs)


# --------------------------------------------------------------------------- manager CRUD
def test_workspace_crud_and_restart_persistence(env, tmp_path):
    manager, store, storage, settings = env
    scope = Scope()
    workspace = manager.create_workspace(scope, "  첫 워크스페이스  ", "설명")
    assert workspace.name == "첫 워크스페이스"
    assert workspace.revision == 1
    assert workspace.workspace_id.startswith("ws_")

    second = manager.create_workspace(scope, "second")
    names = [w.name for w in manager.list_workspaces(scope)]
    assert names == ["second", "첫 워크스페이스"] or set(names) == {"second", "첫 워크스페이스"}
    assert second.workspace_id != workspace.workspace_id

    updated = manager.update_workspace(workspace.workspace_id, scope, name="renamed", expected_revision=1)
    assert updated.name == "renamed" and updated.revision == 2

    with pytest.raises(WorkspaceFailure) as conflict:
        manager.update_workspace(workspace.workspace_id, scope, name="stale", expected_revision=1)
    assert conflict.value.error.code is WorkspaceErrorCode.REVISION_CONFLICT
    assert manager.get_workspace(workspace.workspace_id, scope).name == "renamed"

    # restart: a fresh store/manager over the same database sees the same state
    store2 = WorkspaceStore(settings.resolved_db_path())
    manager2 = WorkspaceManager(store2, storage, settings)
    assert [w.name for w in manager2.list_workspaces(scope)] == ["renamed", "second"] or set(
        w.name for w in manager2.list_workspaces(scope)
    ) == {"renamed", "second"}


def test_workspace_delete_blocks_access_and_purges_storage(env):
    manager, store, storage, settings = env
    scope = Scope()
    workspace = manager.create_workspace(scope, "ephemeral")
    file = _upload(manager, scope, workspace.workspace_id, content=b"payload")
    assert storage.workspace_usage("tenant_a", workspace.workspace_id) > 0

    result = manager.delete_workspace(workspace.workspace_id, scope)
    assert result["status"] == "deleted"
    assert result["purged_files"] >= 1
    assert not storage.workspace_dir("tenant_a", workspace.workspace_id).exists()

    with pytest.raises(WorkspaceFailure) as gone:
        manager.get_workspace(workspace.workspace_id, scope)
    assert gone.value.error.code is WorkspaceErrorCode.WORKSPACE_NOT_FOUND
    with pytest.raises(WorkspaceFailure):
        manager.read_file(workspace.workspace_id, file.file_id, scope)
    assert manager.list_workspaces(scope) == []


def test_scope_isolation_tenant_owner_service(env):
    manager, store, storage, settings = env
    scope = Scope()
    workspace = manager.create_workspace(scope, "private")

    for intruder in (
        Scope(tenant_id="tenant_b"),
        Scope(owner_id="owner_b"),
        Scope(service_id="hd2ai-public"),
    ):
        with pytest.raises(WorkspaceFailure) as hidden:
            manager.get_workspace(workspace.workspace_id, intruder)
        assert hidden.value.error.code is WorkspaceErrorCode.WORKSPACE_NOT_FOUND, intruder.__dict__
        assert manager.check_workspace_access(workspace.workspace_id, intruder).level.value == "none"
        assert manager.list_workspaces(intruder) == []


def test_file_crud_folder_tree_and_events(env):
    manager, store, storage, settings = env
    scope = Scope()
    workspace_id = manager.create_workspace(scope, "files").workspace_id

    folder = manager.create_folder(workspace_id, scope, "docs")
    nested = manager.create_folder(workspace_id, scope, "nested", parent_id=folder.file_id)
    file = _upload(manager, scope, workspace_id, "spec.md", b"# spec", parent_id=nested.file_id)
    assert file.path == "/docs/nested/spec.md"
    assert file.mime_type == "text/markdown"

    listing = manager.list_files(workspace_id, scope, parent_id=folder.file_id)
    assert [item.name for item in listing] == ["nested"]
    recursive = manager.list_files(workspace_id, scope, recursive=True)
    assert {item.name for item in recursive} == {"docs", "nested", "spec.md"}

    renamed = manager.rename_file(workspace_id, file.file_id, scope, "spec-v2.md")
    assert renamed.name == "spec-v2.md" and renamed.path == "/docs/nested/spec-v2.md"

    moved = manager.move_file(workspace_id, file.file_id, scope, None)
    assert moved.path == "/spec-v2.md"

    with pytest.raises(WorkspaceFailure) as cycle:
        manager.move_file(workspace_id, folder.file_id, scope, nested.file_id)
    assert cycle.value.error.code is WorkspaceErrorCode.INVALID_PATH

    events = manager.list_events(workspace_id, scope)
    types = [event["type"] for event in events["items"]]
    assert "file.created" in types and "file.renamed" in types and "file.moved" in types

    deleted = manager.delete_file(workspace_id, folder.file_id, scope)
    # the moved file now lives at the root, so only the two folders are removed
    assert deleted["removed_files"] == 2
    remaining = manager.list_files(workspace_id, scope, recursive=True)
    assert [item.name for item in remaining] == ["spec-v2.md"]


def test_file_content_revisions_and_conflict(env):
    manager, store, storage, settings = env
    scope = Scope()
    workspace_id = manager.create_workspace(scope, "revisions").workspace_id
    first = _upload(manager, scope, workspace_id, "a.txt", b"v1")

    with pytest.raises(WorkspaceFailure) as exists:
        _upload(manager, scope, workspace_id, "a.txt", b"v1b")
    assert exists.value.error.code is WorkspaceErrorCode.FILE_EXISTS

    with pytest.raises(WorkspaceFailure) as stale:
        _upload(manager, scope, workspace_id, "a.txt", b"v2", overwrite=True, expected_revision=99)
    assert stale.value.error.code is WorkspaceErrorCode.REVISION_CONFLICT

    second = _upload(manager, scope, workspace_id, "a.txt", b"v2", overwrite=True, expected_revision=1)
    assert second.file_id == first.file_id and second.revision == 2

    current = manager.read_file(workspace_id, first.file_id, scope)
    assert current.bytes == b"v2" and current.text == "v2"
    old = manager.read_file(workspace_id, first.file_id, scope, revision=1)
    assert old.bytes == b"v1" and old.revision_status.value == "superseded"

    # keep only the newest revisions on disk
    for index in range(3):
        _upload(manager, scope, workspace_id, "a.txt", f"v{index + 3}".encode(), overwrite=True)
    revisions = [row["revision"] for row in store.list_revisions(first.file_id)]
    assert len(revisions) <= settings.revision_keep


# --------------------------------------------------------------------------- storage security
def test_path_traversal_and_unsafe_names_rejected(env):
    manager, store, storage, settings = env
    scope = Scope()
    workspace_id = manager.create_workspace(scope, "safe").workspace_id

    for bad in ("../evil.txt", "a/b.txt", "..\\evil.txt", "", "   ", "x" * 300):
        with pytest.raises(WorkspaceFailure) as failure:
            _upload(manager, scope, workspace_id, bad, b"data")
        assert failure.value.error.code in {WorkspaceErrorCode.INVALID_PATH, WorkspaceErrorCode.FILE_TOO_LARGE}

    with pytest.raises(StorageError):
        storage.blob_path("tenant_a", "ws_../../etc", "wf_abc12345", 1)
    with pytest.raises(StorageError):
        storage.blob_path("tenant_a", workspace_id, "wf_../../etc", 1)

    # an upload never writes a user-named path
    uploaded = _upload(manager, scope, workspace_id, "정상 문서.txt", b"ok")
    blob = storage.blob_path("tenant_a", workspace_id, uploaded.file_id, 1)
    assert blob.parent.name == uploaded.file_id
    assert "정상" not in str(blob)


def test_symlink_components_are_refused(env, tmp_path):
    manager, store, storage, settings = env
    scope = Scope()
    workspace_id = manager.create_workspace(scope, "symlink").workspace_id
    uploaded = _upload(manager, scope, workspace_id, "a.txt", b"real")

    blob = storage.blob_path("tenant_a", workspace_id, uploaded.file_id, 1)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    blob.unlink()
    os.symlink(outside, blob)

    with pytest.raises(StorageError) as refused:
        storage.read_bytes("tenant_a", workspace_id, uploaded.file_id, 1)
    assert refused.value.code == "invalid_path"

    with pytest.raises(WorkspaceFailure) as api_refused:
        manager.read_file(workspace_id, uploaded.file_id, scope)
    assert api_refused.value.error.code is WorkspaceErrorCode.FILE_NOT_FOUND


def test_file_size_and_workspace_quota(tmp_path):
    settings = WorkspaceSettings.for_testing(
        tmp_path, max_file_bytes=1024 * 1024, max_workspace_bytes=3 * 1024 * 1024
    )
    store = WorkspaceStore(settings.resolved_db_path())
    storage = WorkspaceStorage(settings.resolved_storage_root(), max_file_bytes=settings.max_file_bytes)
    manager = WorkspaceManager(store, storage, settings)
    scope = Scope()
    workspace_id = manager.create_workspace(scope, "quota").workspace_id

    with pytest.raises(WorkspaceFailure) as too_big:
        _upload(manager, scope, workspace_id, "big.bin", b"x" * (settings.max_file_bytes + 1))
    assert too_big.value.error.code is WorkspaceErrorCode.FILE_TOO_LARGE
    assert storage.workspace_usage("tenant_a", workspace_id) == 0
    assert manager.list_files(workspace_id, scope, recursive=True) == []

    block = b"y" * (settings.max_file_bytes)
    for index in range(3):
        _upload(manager, scope, workspace_id, f"filler-{index}.bin", block)
    with pytest.raises(WorkspaceFailure) as quota:
        _upload(manager, scope, workspace_id, "overflow.bin", b"z" * 100)
    assert quota.value.error.code is WorkspaceErrorCode.QUOTA_EXCEEDED
    names = {item.name for item in manager.list_files(workspace_id, scope, recursive=True)}
    assert "overflow.bin" not in names


def test_blocked_upload_types_and_orphan_sweep(env):
    manager, store, storage, settings = env
    scope = Scope()
    workspace_id = manager.create_workspace(scope, "types").workspace_id

    with pytest.raises(WorkspaceFailure) as blocked:
        _upload(manager, scope, workspace_id, "malware.exe", b"MZ")
    assert blocked.value.error.code is WorkspaceErrorCode.UNSUPPORTED_MEDIA_TYPE

    uploaded = _upload(manager, scope, workspace_id, "notes.txt", b"hello")
    tmp_dir = storage.workspace_dir("tenant_a", workspace_id) / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    stale = tmp_dir / f"{uploaded.file_id}.deadbeef.part"
    stale.write_bytes(b"partial")
    assert storage.sweep_temp(older_than_seconds=0) >= 1
    assert not stale.exists()


def test_recovery_requeues_interrupted_jobs_and_removes_orphans(env):
    manager, store, storage, settings = env
    scope = Scope()
    workspace_id = manager.create_workspace(scope, "recovery").workspace_id
    uploaded = _upload(manager, scope, workspace_id, "a.txt", b"data")
    store.create_index_job(
        {
            "job_id": "wj_000000000001",
            "workspace_id": workspace_id,
            "file_id": uploaded.file_id,
            "file_revision": 1,
            "state": "INDEXING",
            "attempts": 1,
            "max_attempts": 3,
            "error_code": None,
            "error_detail": None,
            "content_hash": uploaded.content_hash,
            "created_at": "2026-01-01T00:00:00.000Z",
            "started_at": "2026-01-01T00:00:01.000Z",
            "finished_at": None,
        }
    )
    orphan = storage.workspace_dir("tenant_a", workspace_id) / "wf_deadbeef0000" / "r1.blob"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    report = manager.recover()
    assert report["requeued_jobs"] == 1
    assert report["orphan_blobs_removed"] >= 1
    assert store.get_job("wj_000000000001")["state"] == "PENDING"


def test_display_path_never_used_as_os_path(env):
    manager, store, storage, settings = env
    scope = Scope()
    workspace_id = manager.create_workspace(scope, "display").workspace_id
    with pytest.raises(WorkspaceFailure) as reserved:
        manager.create_folder(workspace_id, scope, "..")
    assert reserved.value.error.code is WorkspaceErrorCode.INVALID_PATH

    folder = manager.create_folder(workspace_id, scope, "..docs")
    nested = _upload(manager, scope, workspace_id, "a.txt", b"x", parent_id=folder.file_id)
    assert nested.path == "/..docs/a.txt"
    blob = storage.blob_path("tenant_a", workspace_id, nested.file_id, 1)
    assert "..docs" not in str(blob)
    assert str(storage.root) in str(blob)
