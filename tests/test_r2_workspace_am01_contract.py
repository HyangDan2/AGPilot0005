"""CP-W00 (Amendment 01): folder-based workspace contract surface.

Verifies that Amendment 01 is additive on the frozen v1 contract: the v1 version string and the
v1 operation list stay untouched, the new operation list, provider enum, write contract, error
codes, path gate and the mock provider's folder lifecycle all agree.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.workspace.contracts import (
    ERROR_STATUS,
    WORKSPACE_CONTRACT_AMENDMENT,
    WORKSPACE_CONTRACT_REVISION,
    WORKSPACE_CONTRACT_VERSION,
    ContentOrigin,
    InvalidRelativePath,
    RootStatus,
    WorkspaceErrorCode,
    WorkspaceFolderProvider,
    WorkspaceLifecycleState,
    WorkspaceRootInfo,
    WorkspaceStorageProvider,
    WorkspaceWriteRequest,
    provider_operations,
    provider_operations_a01,
    validate_relative_path,
)
from app.workspace.mock import MockWorkspaceProvider, build_mock_router


class _Scope:
    tenant_id = "mock_tenant"
    owner_id = "mock_user"
    service_id = "hd2ai-admin"


def test_amendment_is_additive_on_the_frozen_v1_surface():
    # v1 stays exactly as frozen for existing consumers
    assert WORKSPACE_CONTRACT_VERSION == "r2-w/v1"
    assert len(provider_operations()) == 8
    # Amendments are tracked separately and each adds its own operation list
    assert WORKSPACE_CONTRACT_AMENDMENT == "A02"
    assert WORKSPACE_CONTRACT_REVISION == "r2-w/v1+a02"
    assert set(provider_operations_a01()) == {
        "open_folder",
        "new_workspace",
        "close_workspace",
        "reopen_workspace",
        "disconnect_workspace",
        "write_file",
        "sync_workspace",
    }
    for name in provider_operations_a01():
        assert callable(getattr(WorkspaceFolderProvider, name)), name
    # no Amendment 01 name leaks into the frozen v1 list
    assert not set(provider_operations_a01()) & set(provider_operations())


def test_storage_providers_lifecycle_and_content_origin_are_frozen():
    assert {p.value for p in WorkspaceStorageProvider} == {
        "server_managed",
        "server_mounted_folder",
        "local_browser",
    }
    assert {s.value for s in WorkspaceLifecycleState} == {"open", "closed", "deleted"}
    assert {o.value for o in ContentOrigin} == {"blob", "external"}
    assert {s.value for s in RootStatus} == {"available", "missing", "not_allowed", "metadata_overlap"}


def test_am01_error_codes_have_http_status():
    for code in (
        WorkspaceErrorCode.WORKSPACE_CLOSED,
        WorkspaceErrorCode.READ_ONLY_WORKSPACE,
        WorkspaceErrorCode.PATH_OUTSIDE_ALLOWED_ROOT,
        WorkspaceErrorCode.SYMLINK_ESCAPE,
        WorkspaceErrorCode.ROOT_UNAVAILABLE,
        WorkspaceErrorCode.PROVIDER_MISMATCH,
        WorkspaceErrorCode.CONTENT_CONFLICT,
        WorkspaceErrorCode.SOURCE_DELETE_NOT_PERMITTED,
        WorkspaceErrorCode.METADATA_OVERLAP,
        WorkspaceErrorCode.WATCH_UNAVAILABLE,
    ):
        assert code in ERROR_STATUS
    assert ERROR_STATUS[WorkspaceErrorCode.WORKSPACE_CLOSED] == 409
    assert ERROR_STATUS[WorkspaceErrorCode.SOURCE_DELETE_NOT_PERMITTED] == 403
    # v1 mapping is unchanged
    assert ERROR_STATUS[WorkspaceErrorCode.WORKSPACE_NOT_FOUND] == 404
    assert ERROR_STATUS[WorkspaceErrorCode.INVALID_PATH] == 400


def test_root_info_defaults_are_a_v1_workspace():
    root = WorkspaceRootInfo()
    assert root.storage_provider is WorkspaceStorageProvider.SERVER_MANAGED
    assert root.zero_copy is False
    assert root.server_side_path_access is True
    assert root.status is RootStatus.AVAILABLE


@pytest.mark.parametrize(
    "bad",
    ["", "/etc/passwd", "../escape", "a/../b", "a//b", "./x", "C:/win", "a\\b", "x\x00y", "a/" * 600],
)
def test_relative_path_gate_rejects_unsafe_paths(bad):
    with pytest.raises(InvalidRelativePath):
        validate_relative_path(bad)


@pytest.mark.parametrize("good", ["a.txt", "a/b/c.md", "한글 문서/요약.md", "dir.with.dots/f-1_2.txt"])
def test_relative_path_gate_accepts_workspace_paths(good):
    assert validate_relative_path(good) == good


def test_mock_provider_folder_lifecycle_and_write_contract():
    provider = MockWorkspaceProvider()
    scope = _Scope()

    opened = provider.open_folder(scope, "/srv/hd2lab/workspaces/lab-docs", name="lab-docs")
    assert opened.root.storage_provider is WorkspaceStorageProvider.SERVER_MOUNTED_FOLDER
    assert opened.root.zero_copy is True
    assert opened.root.server_side_path_access is True
    assert opened.lifecycle_state is WorkspaceLifecycleState.OPEN

    created = provider.new_workspace(scope, "canvas-out")
    assert created.root.storage_provider is WorkspaceStorageProvider.SERVER_MANAGED
    assert created.root.zero_copy is True

    written = provider.write_file(
        opened.workspace_id,
        scope,
        WorkspaceWriteRequest(path="reports/summary.md", content="# summary"),
    )
    assert written.path == "reports/summary.md"
    assert written.content_origin is ContentOrigin.EXTERNAL
    assert written.file is not None and written.file.relative_path == "reports/summary.md"
    created_file = provider.read_file(opened.workspace_id, written.file.file_id, scope)
    assert created_file.text == "# summary"

    closed = provider.close_workspace(opened.workspace_id, scope)
    assert closed.lifecycle_state is WorkspaceLifecycleState.CLOSED
    with pytest.raises(Exception):
        provider.write_file(opened.workspace_id, scope, WorkspaceWriteRequest(path="x.md", content="x"))

    reopened = provider.reopen_workspace(opened.workspace_id, scope)
    assert reopened.lifecycle_state is WorkspaceLifecycleState.OPEN
    # files survived the close/reopen cycle
    assert provider.get_file_metadata(opened.workspace_id, written.file.file_id, scope).name == "summary.md"


def test_mock_provider_refuses_source_deletion_and_serves_client_writes():
    provider = MockWorkspaceProvider()
    scope = _Scope()
    opened = provider.open_folder(scope, "/srv/hd2lab/workspaces/lab-docs")

    with pytest.raises(Exception) as refused:
        provider.disconnect_workspace(opened.workspace_id, scope, delete_source_files=True)
    assert getattr(refused.value, "status_code", None) == 403

    browser = provider.seed_workspace(
        "picked folder", storage_provider=WorkspaceStorageProvider.LOCAL_BROWSER, display_path="내 문서/보고서"
    )
    assert browser.root.server_side_path_access is False
    directive = provider.write_file(
        browser.workspace_id, scope, WorkspaceWriteRequest(path="out/result.md", content="x")
    )
    assert directive.client_write is not None
    assert directive.client_write.relative_path == "out/result.md"
    assert directive.client_write.reason == "local_browser_no_server_path_access"

    result = provider.disconnect_workspace(opened.workspace_id, scope)
    assert result["source_files_deleted"] is False


def test_mock_router_exposes_amendment_lifecycle():
    provider = MockWorkspaceProvider()
    app = FastAPI()
    app.include_router(build_mock_router(provider))
    client = TestClient(app)

    opened = client.post("/_mock/workspaces/open-folder", json={"path": "/srv/hd2lab/workspaces/lab-docs"})
    assert opened.status_code == 201
    workspace_id = opened.json()["workspace_id"]
    assert opened.json()["root"]["storage_provider"] == "server_mounted_folder"

    assert client.post(f"/_mock/workspaces/{workspace_id}/close", json={}).json()["lifecycle_state"] == "closed"
    assert client.post(f"/_mock/workspaces/{workspace_id}/reopen").json()["lifecycle_state"] == "open"

    written = client.put(
        f"/_mock/workspaces/{workspace_id}/file", json={"path": "notes/a.md", "content": "hello"}
    )
    assert written.status_code == 200 and written.json()["written_bytes"] == 5

    synced = client.post(f"/_mock/workspaces/{workspace_id}/sync", json={})
    assert synced.status_code == 200 and synced.json()["workspace_id"] == workspace_id

    disconnected = client.post(f"/_mock/workspaces/{workspace_id}/disconnect", json={})
    assert disconnected.json()["source_files_deleted"] is False
