"""CP-W01..W07 (Amendment 01): folder-based workspace backend.

Covers the work order's required proofs:

* allowed-root confinement (outside root, traversal, symlink escape, metadata overlap)
* zero-copy opening (nothing is copied and the root is not polluted)
* Open / New / Close / Reopen lifecycle and disconnect-vs-delete separation
* file watcher create / modify / move / delete applied to the RAG index
* the Canvas / Coding Agent common write contract on every provider
* metadata & index storage kept outside the workspace root
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.workspace.api import router as workspace_router
from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import (
    FileKind,
    IndexState,
    LocalBrowserEntry,
    WorkspaceErrorCode,
    WorkspaceLifecycleState,
    WorkspaceStorageProvider,
    WorkspaceUpdateRequest,
    WorkspaceWriteRequest,
)
from app.workspace.indexer import WorkspaceIndexer
from app.workspace.ingest import ExtractionResult
from app.workspace.manager import WorkspaceFailure, WorkspaceManager
from app.workspace.runtime import WorkspaceRuntime, set_runtime
from app.workspace.storage import WorkspaceStorage
from app.workspace.store import WorkspaceStore
from app.workspace.textutil import estimate_tokens
from app.workspace.watcher import WorkspaceWatcher


class ParagraphIngestor:
    def for_scope(self, tenant_id, owner_id):
        return self

    def supports(self, name, mime_type):
        return True

    def extract(self, name, mime_type, data):
        from app.workspace.chunking import SourceChunk

        text = data.decode("utf-8", errors="replace")
        chunks = [
            SourceChunk(
                content=paragraph,
                token_count=estimate_tokens(paragraph),
                page_number=index + 1,
                section=f"sec-{index + 1}",
                offset_start=0,
                offset_end=len(paragraph),
            )
            for index, paragraph in enumerate(part.strip() for part in text.split("\n\n"))
            if paragraph
        ]
        return ExtractionResult(chunks=chunks, parser_version="stub", chunking_version="stub", extraction_source="stub")


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
    indexer = WorkspaceIndexer(manager, settings, ParagraphIngestor(), embedder=None)
    watcher = WorkspaceWatcher(manager, settings)
    return {
        "settings": settings,
        "store": store,
        "storage": storage,
        "manager": manager,
        "indexer": indexer,
        "watcher": watcher,
        "scope": Scope(),
        "allowed": tmp_path / "allowed",
        "tmp": tmp_path,
    }


def _make_folder(base: Path, name="docs", files=None) -> Path:
    folder = base / name
    folder.mkdir(parents=True, exist_ok=True)
    for rel, text in (files or {}).items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return folder


def _open(env, folder: Path, **kwargs):
    return env["manager"].open_folder(env["scope"], str(folder), **kwargs)


def _files(env, workspace_id, recursive=True):
    return env["manager"].list_files(workspace_id, env["scope"], recursive=recursive)


def _by_rel(env, workspace_id):
    return {item.relative_path: item for item in _files(env, workspace_id) if item.kind is FileKind.FILE}


# --------------------------------------------------------------------- open / zero-copy
def test_open_folder_mounts_zero_copy_and_keeps_metadata_outside(env):
    folder = _make_folder(env["allowed"], "lab-docs", {"spec.md": "# spec\n\nbody", "sub/notes.txt": "note"})
    before = {str(p.relative_to(folder)) for p in folder.rglob("*")}

    workspace = _open(env, folder, name="lab-docs")
    assert workspace.root.storage_provider is WorkspaceStorageProvider.SERVER_MOUNTED_FOLDER
    assert workspace.root.zero_copy is True
    assert workspace.root.server_side_path_access is True
    assert workspace.lifecycle_state is WorkspaceLifecycleState.OPEN
    assert workspace.root.display_path == str(folder.resolve())

    files = _by_rel(env, workspace.workspace_id)
    assert set(files) == {"spec.md", "sub/notes.txt"}
    assert files["spec.md"].content_origin.value == "external"
    assert files["spec.md"].relative_path == "spec.md"
    assert files["sub/notes.txt"].parent_id is not None

    # Zero-copy: nothing landed in the blob storage root.
    assert env["storage"].workspace_usage(env["scope"].tenant_id, workspace.workspace_id) == 0
    usage = env["manager"].workspace_usage(workspace.workspace_id, env["scope"])
    assert usage["usage_bytes"] == 0 and usage["zero_copy"] is True

    # Metadata/index separation: the root gained no engine bookkeeping file.
    after = {str(p.relative_to(folder)) for p in folder.rglob("*")}
    assert after == before
    info = env["manager"].workspace_root_info(workspace.workspace_id, env["scope"])
    assert info["index_storage"]["inside_workspace_root"] is False
    assert info["index_storage"]["separate_from_workspace_root"] is True

    # Content is read in place, not from a copy.
    content = env["manager"].read_file(workspace.workspace_id, files["spec.md"].file_id, env["scope"])
    assert content.text == "# spec\n\nbody"

    # Opening the same folder again is idempotent for the same owner.
    again = _open(env, folder)
    assert again.workspace_id == workspace.workspace_id


def test_open_folder_rejects_outside_root_traversal_and_symlink(env):
    outside = env["tmp"] / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(WorkspaceFailure) as denied:
        env["manager"].open_folder(env["scope"], str(outside))
    assert denied.value.error.code is WorkspaceErrorCode.PATH_OUTSIDE_ALLOWED_ROOT

    with pytest.raises(WorkspaceFailure) as traversal:
        env["manager"].open_folder(env["scope"], str(env["allowed"] / ".." / "outside"))
    assert traversal.value.error.code is WorkspaceErrorCode.INVALID_PATH

    with pytest.raises(WorkspaceFailure) as relative:
        env["manager"].open_folder(env["scope"], "relative/path")
    assert relative.value.error.code is WorkspaceErrorCode.INVALID_PATH

    inside = _make_folder(env["allowed"], "real", {"a.txt": "a"})
    link = env["allowed"] / "link"
    os.symlink(outside, link)
    with pytest.raises(WorkspaceFailure) as symlinked:
        env["manager"].open_folder(env["scope"], str(link))
    assert symlinked.value.error.code is WorkspaceErrorCode.SYMLINK_ESCAPE

    with pytest.raises(WorkspaceFailure) as missing:
        env["manager"].open_folder(env["scope"], str(env["allowed"] / "does-not-exist"))
    assert missing.value.error.code is WorkspaceErrorCode.ROOT_UNAVAILABLE
    assert inside.exists()


def test_symlink_inside_root_is_skipped_and_write_through_symlink_is_refused(env):
    folder = _make_folder(env["allowed"], "mixed", {"a.txt": "a"})
    secret_dir = env["tmp"] / "secret-dir"
    secret_dir.mkdir()
    (secret_dir / "hidden.txt").write_text("hidden", encoding="utf-8")
    os.symlink(secret_dir, folder / "escape")

    workspace = _open(env, folder)
    rels = set(_by_rel(env, workspace.workspace_id))
    assert "a.txt" in rels
    assert not any(rel.startswith("escape") for rel in rels)

    with pytest.raises(WorkspaceFailure) as refused:
        env["manager"].write_file(
            workspace.workspace_id,
            env["scope"],
            WorkspaceWriteRequest(path="escape/leak.txt", content="leak"),
        )
    assert refused.value.error.code in {
        WorkspaceErrorCode.SYMLINK_ESCAPE,
        WorkspaceErrorCode.PATH_OUTSIDE_ALLOWED_ROOT,
    }
    assert not (secret_dir / "leak.txt").exists()


def test_metadata_overlap_is_refused(tmp_path):
    # An allowed root that contains the metadata database must be refused outright.
    settings = WorkspaceSettings.for_testing(tmp_path)
    store = WorkspaceStore(settings.resolved_db_path())
    storage = WorkspaceStorage(settings.resolved_storage_root(), max_file_bytes=settings.max_file_bytes)
    from app.workspace.rootfs import AllowedRoot

    settings.allowed_roots = (AllowedRoot(root_id="state", path=str(tmp_path), scope="global", is_default=True),)
    manager = WorkspaceManager(store, storage, settings)
    with pytest.raises(WorkspaceFailure) as overlap:
        manager.open_folder(Scope(), str(tmp_path))
    assert overlap.value.error.code is WorkspaceErrorCode.METADATA_OVERLAP


# --------------------------------------------------------------------- close / reopen
def test_close_keeps_metadata_and_reopen_catches_up(env):
    folder = _make_folder(env["allowed"], "lifecycle", {"a.txt": "alpha"})
    workspace = _open(env, folder)
    workspace_id = workspace.workspace_id
    file_id = _by_rel(env, workspace_id)["a.txt"].file_id
    env["indexer"].run_pending(limit=5)
    assert env["store"].count_chunks(workspace_id) > 0

    closed = env["manager"].close_workspace(workspace_id, env["scope"], reason="lunch")
    assert closed.lifecycle_state is WorkspaceLifecycleState.CLOSED
    assert closed.closed_at is not None
    # metadata survives
    assert env["store"].get_file(workspace_id, file_id) is not None
    assert env["store"].count_chunks(workspace_id) > 0
    # closed refuses reads, writes and sync
    with pytest.raises(WorkspaceFailure) as read_blocked:
        env["manager"].read_file(workspace_id, file_id, env["scope"])
    assert read_blocked.value.error.code is WorkspaceErrorCode.WORKSPACE_CLOSED
    with pytest.raises(WorkspaceFailure) as write_blocked:
        env["manager"].write_file(workspace_id, env["scope"], WorkspaceWriteRequest(path="b.txt", content="b"))
    assert write_blocked.value.error.code is WorkspaceErrorCode.WORKSPACE_CLOSED
    with pytest.raises(WorkspaceFailure) as sync_blocked:
        env["manager"].sync_workspace(workspace_id, env["scope"])
    assert sync_blocked.value.error.code is WorkspaceErrorCode.WORKSPACE_CLOSED
    # writes never reached the disk
    assert not (folder / "b.txt").exists()

    # a change made while closed is picked up by reopen
    (folder / "while-closed.txt").write_text("late", encoding="utf-8")
    reopened = env["manager"].reopen_workspace(workspace_id, env["scope"])
    assert reopened.lifecycle_state is WorkspaceLifecycleState.OPEN
    assert "while-closed.txt" in _by_rel(env, workspace_id)
    assert env["manager"].read_file(workspace_id, file_id, env["scope"]).text == "alpha"


def test_reopen_requires_the_root_to_still_exist(env):
    folder = _make_folder(env["allowed"], "gone", {"a.txt": "a"})
    workspace = _open(env, folder)
    env["manager"].close_workspace(workspace.workspace_id, env["scope"])
    for child in folder.iterdir():
        child.unlink()
    folder.rmdir()
    with pytest.raises(WorkspaceFailure) as failure:
        env["manager"].reopen_workspace(workspace.workspace_id, env["scope"])
    assert failure.value.error.code in {WorkspaceErrorCode.ROOT_UNAVAILABLE, WorkspaceErrorCode.PATH_OUTSIDE_ALLOWED_ROOT}


# --------------------------------------------------------------------- disconnect
def test_disconnect_never_deletes_the_real_folder(env):
    folder = _make_folder(env["allowed"], "keep-me", {"a.txt": "alpha", "sub/b.txt": "beta"})
    workspace = _open(env, folder)
    workspace_id = workspace.workspace_id

    with pytest.raises(WorkspaceFailure) as refused:
        env["manager"].disconnect_workspace(workspace_id, env["scope"], delete_source_files=True)
    assert refused.value.error.code is WorkspaceErrorCode.SOURCE_DELETE_NOT_PERMITTED
    assert (folder / "a.txt").exists()

    result = env["manager"].disconnect_workspace(workspace_id, env["scope"])
    assert result["source_files_deleted"] == 0
    assert (folder / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (folder / "sub" / "b.txt").exists()
    with pytest.raises(WorkspaceFailure) as gone:
        env["manager"].get_workspace(workspace_id, env["scope"])
    assert gone.value.error.code is WorkspaceErrorCode.WORKSPACE_NOT_FOUND


def test_source_deletion_requires_operator_opt_in(env, monkeypatch):
    folder = _make_folder(env["allowed"], "delete-me", {"a.txt": "alpha"})
    workspace = _open(env, folder)
    env["settings"].allow_source_delete = True

    with pytest.raises(WorkspaceFailure) as no_confirm:
        env["manager"].disconnect_workspace(workspace.workspace_id, env["scope"], delete_source_files=True)
    assert no_confirm.value.error.code is WorkspaceErrorCode.SOURCE_DELETE_NOT_PERMITTED
    assert (folder / "a.txt").exists()

    result = env["manager"].disconnect_workspace(
        workspace.workspace_id, env["scope"], delete_source_files=True, confirm=True
    )
    assert result["source_files_deleted"] == 1
    assert not (folder / "a.txt").exists()
    assert folder.exists()  # the folder itself is not blindly removed


# --------------------------------------------------------------------- watcher → index
def test_watcher_applies_create_modify_move_delete_to_the_index(env):
    folder = _make_folder(env["allowed"], "watch", {"a.txt": "alpha paragraph"})
    workspace = _open(env, folder)
    workspace_id = workspace.workspace_id
    env["indexer"].run_pending(limit=5)
    before = _by_rel(env, workspace_id)["a.txt"]
    assert before.index_status is IndexState.READY

    # create
    (folder / "b.txt").write_text("beta paragraph", encoding="utf-8")
    result = env["manager"].sync_workspace(workspace_id, env["scope"])
    assert result.created == 1
    after_create = _by_rel(env, workspace_id)
    assert "b.txt" in after_create
    env["indexer"].run_pending(limit=5)
    assert _by_rel(env, workspace_id)["b.txt"].index_status is IndexState.READY
    assert env["store"].count_chunks_for_file(after_create["b.txt"].file_id) > 0

    # modify
    time.sleep(0.01)
    (folder / "a.txt").write_text("alpha revisited with more words", encoding="utf-8")
    result = env["manager"].sync_workspace(workspace_id, env["scope"])
    assert result.modified == 1
    modified = _by_rel(env, workspace_id)["a.txt"]
    assert modified.file_id == before.file_id
    assert modified.revision == before.revision + 1
    assert modified.content_hash != before.content_hash
    env["indexer"].run_pending(limit=5)
    chunks = env["store"].chunks_for_file(modified.file_id)
    assert chunks and "revisited" in chunks[0]["content"]

    # move (same bytes, new path): identity and index are kept
    (folder / "nested").mkdir()
    os.replace(folder / "b.txt", folder / "nested" / "b.txt")
    result = env["manager"].sync_workspace(workspace_id, env["scope"])
    assert result.moved == 1
    moved = _by_rel(env, workspace_id)
    moved_file = moved["nested/b.txt"]
    assert moved_file.file_id == after_create["b.txt"].file_id
    assert "b.txt" not in moved
    assert env["store"].count_chunks_for_file(moved_file.file_id) > 0  # index kept

    # delete
    (folder / "nested" / "b.txt").unlink()
    result = env["manager"].sync_workspace(workspace_id, env["scope"])
    assert result.deleted >= 1
    remaining = _by_rel(env, workspace_id)
    assert "nested/b.txt" not in remaining
    assert env["store"].count_chunks_for_file(after_create["b.txt"].file_id) == 0


def test_watcher_thread_picks_up_external_change(env):
    folder = _make_folder(env["allowed"], "thread-watch", {"a.txt": "alpha"})
    workspace = _open(env, folder)
    workspace_id = workspace.workspace_id
    watcher: WorkspaceWatcher = env["watcher"]
    env["settings"].watch_interval_seconds = 0.05
    watcher.start(interval=0.05)
    try:
        (folder / "appeared.txt").write_text("appeared", encoding="utf-8")
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if "appeared.txt" in _by_rel(env, workspace_id):
                break
            time.sleep(0.05)
        assert "appeared.txt" in _by_rel(env, workspace_id)
    finally:
        watcher.stop()
    assert watcher.running is False


def test_watch_status_reports_running_watcher(env):
    folder = _make_folder(env["allowed"], "status", {"a.txt": "a"})
    workspace = _open(env, folder)
    env["manager"].sync_workspace(workspace.workspace_id, env["scope"])
    status = env["manager"].watch_status(workspace.workspace_id, env["scope"])
    assert status.watching is True
    assert status.storage_provider is WorkspaceStorageProvider.SERVER_MOUNTED_FOLDER
    assert status.last_scan_at is not None
    assert status.interval_seconds > 0

    env["manager"].close_workspace(workspace.workspace_id, env["scope"])
    closed_status = env["manager"].watch_status(workspace.workspace_id, env["scope"])
    assert closed_status.watching is False


# --------------------------------------------------------------------- new workspace
def test_new_workspace_creates_a_real_folder_under_the_allowed_root(env):
    workspace = env["manager"].new_workspace(env["scope"], "canvas-project", description="canvas")
    assert workspace.root.storage_provider is WorkspaceStorageProvider.SERVER_MANAGED
    assert workspace.root.zero_copy is True
    root = Path(workspace.root.display_path)
    assert root.is_dir()
    assert root.parent == env["allowed"].resolve()

    with pytest.raises(WorkspaceFailure) as duplicate:
        env["manager"].new_workspace(env["scope"], "canvas-project")
    assert duplicate.value.error.code in {WorkspaceErrorCode.FILE_EXISTS, WorkspaceErrorCode.QUOTA_EXCEEDED}


# --------------------------------------------------------------------- concurrency


def test_watcher_and_opening_scan_do_not_race(env):
    """A regression for the unique-name clash when the watcher scanned a workspace mid-open."""

    from concurrent.futures import ThreadPoolExecutor

    files = {f"note-{index}.txt": f"body {index}" for index in range(12)}
    folder = _make_folder(env["allowed"], "race", files)
    watcher: WorkspaceWatcher = env["watcher"]
    watcher.start(interval=0.01)
    try:
        workspace = _open(env, folder)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _index: env["manager"].sync_workspace(workspace.workspace_id, env["scope"]),
                    range(8),
                )
            )
        assert all(result.workspace_id == workspace.workspace_id for result in results)
        rows = _files(env, workspace.workspace_id)
        names = [row.name for row in rows]
        assert len(names) == len(set(names)), names
        assert len(_by_rel(env, workspace.workspace_id)) == len(files)
    finally:
        watcher.stop()


# --------------------------------------------------------------------- write contract
def test_write_contract_is_identical_on_both_folder_providers(env):
    mounted = _open(env, _make_folder(env["allowed"], "mounted", {"seed.txt": "seed"}))
    managed = env["manager"].new_workspace(env["scope"], "managed-out")

    for workspace in (mounted, managed):
        result = env["manager"].write_file(
            workspace.workspace_id,
            env["scope"],
            WorkspaceWriteRequest(path="reports/summary.md", content="# summary", mime_type="text/markdown"),
        )
        assert result.path == "reports/summary.md"
        assert result.revision == 1
        assert result.file is not None
        assert result.file.relative_path == "reports/summary.md"
        root = Path(workspace.root.display_path)
        written = root / "reports" / "summary.md"
        assert written.read_text(encoding="utf-8") == "# summary"
        assert result.storage_provider is workspace.root.storage_provider
        # zero-copy providers never touch the blob store
        assert env["storage"].workspace_usage(env["scope"].tenant_id, workspace.workspace_id) == 0

        # a second write is a new revision of the same file, and the real file changes
        second = env["manager"].write_file(
            workspace.workspace_id, env["scope"], WorkspaceWriteRequest(path="reports/summary.md", content="# v2")
        )
        assert second.file is not None and second.file.file_id == result.file.file_id
        assert second.revision == 2
        assert written.read_text(encoding="utf-8") == "# v2"

        # optimistic guard
        with pytest.raises(WorkspaceFailure) as conflict:
            env["manager"].write_file(
                workspace.workspace_id,
                env["scope"],
                WorkspaceWriteRequest(
                    path="reports/summary.md", content="# v3", expected_content_hash="sha256:stale"
                ),
            )
        assert conflict.value.error.code is WorkspaceErrorCode.CONTENT_CONFLICT
        assert written.read_text(encoding="utf-8") == "# v2"

        # the new file is indexed like an uploaded one
        env["indexer"].run_pending(limit=10)
        rels = _by_rel(env, workspace.workspace_id)
        assert rels["reports/summary.md"].index_status is IndexState.READY


def test_write_contract_path_gate_and_read_only(env):
    folder = _make_folder(env["allowed"], "readonly", {"a.txt": "a"})
    read_only = env["manager"].open_folder(env["scope"], str(folder), name="ro", read_only=True)
    assert read_only.root.read_only is True
    with pytest.raises(WorkspaceFailure) as blocked:
        env["manager"].write_file(read_only.workspace_id, env["scope"], WorkspaceWriteRequest(path="x.txt", content="x"))
    assert blocked.value.error.code is WorkspaceErrorCode.READ_ONLY_WORKSPACE

    writable = _open(env, _make_folder(env["allowed"], "writable", {"a.txt": "a"}))
    for bad in ("/etc/passwd", "../escape.txt", "a/../../b.txt", "C:/x.txt"):
        with pytest.raises(WorkspaceFailure) as rejected:
            env["manager"].write_file(writable.workspace_id, env["scope"], WorkspaceWriteRequest(path=bad, content="x"))
        assert rejected.value.error.code is WorkspaceErrorCode.INVALID_PATH

    with pytest.raises(WorkspaceFailure) as needs_content:
        env["manager"].write_file(writable.workspace_id, env["scope"], WorkspaceWriteRequest(path="empty.txt"))


def test_local_browser_workspace_never_uses_a_server_path(env):
    workspace = env["manager"].create_local_browser_workspace(
        env["scope"],
        "browser-docs",
        client_handle_id="handle-42",
        display_path="내 문서/보고서",
        entries=[
            LocalBrowserEntry(path="notes.md", size_bytes=11, text="browser note"),
            LocalBrowserEntry(path="folder", kind=FileKind.FOLDER),
            LocalBrowserEntry(path="folder/deep.md", text="deep text"),
        ],
    )
    assert workspace.root.storage_provider is WorkspaceStorageProvider.LOCAL_BROWSER
    assert workspace.root.server_side_path_access is False
    assert workspace.root.client_handle_id == "handle-42"
    assert workspace.root.zero_copy is True
    assert workspace.root.read_only is True

    listed = _by_rel(env, workspace.workspace_id)
    assert set(listed) == {"notes.md", "folder/deep.md"}

    # the captured text is indexed (the server holds no file body and no path)
    env["indexer"].run_pending(limit=10)
    assert env["store"].count_chunks_for_file(listed["notes.md"].file_id) > 0
    assert env["manager"].read_file(workspace.workspace_id, listed["notes.md"].file_id, env["scope"]).text == "browser note"

    # read-only browser roots refuse writes at the server (contract A1.5)
    with pytest.raises(WorkspaceFailure) as refused:
        env["manager"].write_file(
            workspace.workspace_id, env["scope"], WorkspaceWriteRequest(path="out/result.md", content="x")
        )
    assert refused.value.error.code is WorkspaceErrorCode.READ_ONLY_WORKSPACE

    # a writable browser workspace answers with a client directive, never a server write
    writable = env["manager"].create_local_browser_workspace(
        env["scope"], "browser-rw", client_handle_id="handle-43", read_only=False
    )
    result = env["manager"].write_file(
        writable.workspace_id, env["scope"], WorkspaceWriteRequest(path="out/result.md", content="x")
    )
    assert result.client_write is not None
    assert result.client_write.client_handle_id == "handle-43"
    assert result.client_write.relative_path == "out/result.md"
    assert result.file is None
    with pytest.raises(WorkspaceFailure) as provider_mismatch:
        env["manager"].sync_workspace(workspace.workspace_id, env["scope"])
    assert provider_mismatch.value.error.code is WorkspaceErrorCode.PROVIDER_MISMATCH


# --------------------------------------------------------------------- HTTP surface
@pytest.fixture()
def api(env, monkeypatch):
    store: WorkspaceStore = env["store"]
    runtime = WorkspaceRuntime(
        settings=env["settings"],
        store=store,
        storage=env["storage"],
        manager=env["manager"],
        indexer=env["indexer"],
        retriever=None,  # type: ignore[arg-type]
        assembler=None,  # type: ignore[arg-type]
        citations=None,  # type: ignore[arg-type]
        embedder=None,
        tools=None,  # type: ignore[arg-type]
        watcher=env["watcher"],
    )
    set_runtime(runtime)
    app = FastAPI()
    app.include_router(workspace_router)
    monkeypatch.setattr("app.workspace.api.get_scope", lambda request: env["scope"])
    yield TestClient(app), env
    set_runtime(None)


def test_http_open_close_reopen_disconnect_and_write(api):
    client, env = api

    roots = client.get("/api/v1/workspaces/allowed-roots").json()
    assert roots["total"] >= 1
    assert roots["items"][0]["root_id"] == "test"

    folder = _make_folder(env["allowed"], "http-docs", {"readme.md": "hello"})
    opened = client.post("/api/v1/workspaces/open-folder", json={"path": str(folder), "name": "http-docs"})
    assert opened.status_code == 201, opened.text
    workspace_id = opened.json()["workspace_id"]
    assert opened.json()["root"]["storage_provider"] == "server_mounted_folder"

    listing = client.get(f"/api/v1/workspaces/{workspace_id}/files?recursive=true").json()
    assert listing["total"] >= 1

    written = client.put(
        f"/api/v1/workspaces/{workspace_id}/file",
        json={"path": "out/answer.md", "content": "answer"},
    )
    assert written.status_code == 200, written.text
    assert written.json()["content_origin"] == "external"

    root_info = client.get(f"/api/v1/workspaces/{workspace_id}/root").json()
    assert root_info["index_storage"]["inside_workspace_root"] is False

    closed = client.post(f"/api/v1/workspaces/{workspace_id}/close", json={"reason": "test"})
    assert closed.json()["lifecycle_state"] == "closed"
    blocked = client.get(f"/api/v1/workspaces/{workspace_id}/search", params={"q": "hello"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "workspace_closed"
    reopened = client.post(f"/api/v1/workspaces/{workspace_id}/reopen")
    assert reopened.json()["lifecycle_state"] == "open"

    watch = client.get(f"/api/v1/workspaces/{workspace_id}/watch").json()
    assert watch["storage_provider"] == "server_mounted_folder"

    synced = client.post(f"/api/v1/workspaces/{workspace_id}/sync", json={"full": False})
    assert synced.status_code == 200 and synced.json()["workspace_id"] == workspace_id

    refused = client.post(
        f"/api/v1/workspaces/{workspace_id}/disconnect", json={"delete_source_files": True}
    )
    assert refused.status_code == 403
    assert (folder / "readme.md").exists()

    released = client.post(f"/api/v1/workspaces/{workspace_id}/disconnect", json={})
    assert released.status_code == 200
    assert released.json()["source_files_deleted"] == 0
    assert (folder / "readme.md").read_text(encoding="utf-8") == "hello"
    assert client.get(f"/api/v1/workspaces/{workspace_id}").status_code == 404


def test_http_new_workspace_and_local_browser(api):
    client, env = api
    created = client.post("/api/v1/workspaces/new", json={"name": "http-managed"})
    assert created.status_code == 201, created.text
    assert created.json()["root"]["zero_copy"] is True
    assert Path(created.json()["root"]["display_path"]).is_dir()

    browser = client.post(
        "/api/v1/workspaces/local-browser",
        json={"name": "picked", "client_handle_id": "h-1", "entries": [{"path": "a.md", "text": "browser a"}]},
    )
    assert browser.status_code == 201, browser.text
    workspace_id = browser.json()["workspace_id"]
    write = client.put(f"/api/v1/workspaces/{workspace_id}/file", json={"path": "x.md", "content": "x"})
    assert write.status_code == 403  # read-only browser workspace
    browser_readonly_off = client.post(
        "/api/v1/workspaces/local-browser",
        json={"name": "picked-rw", "client_handle_id": "h-2", "read_only": False, "entries": []},
    )
    rw_id = browser_readonly_off.json()["workspace_id"]
    directive = client.put(f"/api/v1/workspaces/{rw_id}/file", json={"path": "x.md", "content": "x"})
    assert directive.status_code == 200
    assert directive.json()["client_write"]["relative_path"] == "x.md"
