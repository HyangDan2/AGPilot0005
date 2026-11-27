"""CP-W00: shared contract freeze — provider interface, mock provider and error mapping."""
from __future__ import annotations

import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.workspace.contracts import (
    WORKSPACE_CONTRACT_VERSION,
    ERROR_STATUS,
    ArtifactReferenceInput,
    WorkspaceErrorCode,
    WorkspaceProvider,
    provider_operations,
)
from app.workspace.mock import MockWorkspaceProvider, build_mock_router


class _Scope:
    tenant_id = "mock_tenant"
    owner_id = "mock_user"
    service_id = "hd2ai-admin"


def test_contract_version_and_operation_names_frozen():
    assert WORKSPACE_CONTRACT_VERSION == "r2-w/v1"
    expected = {
        "get_workspace",
        "check_workspace_access",
        "list_workspace_files",
        "get_file_metadata",
        "read_file",
        "create_file",
        "register_artifact_reference",
        "list_artifact_references",
    }
    assert set(provider_operations()) == expected
    for name in expected:
        assert callable(getattr(WorkspaceProvider, name)), name


def test_error_status_mapping_covers_every_code():
    assert set(ERROR_STATUS) == set(WorkspaceErrorCode)
    assert ERROR_STATUS[WorkspaceErrorCode.WORKSPACE_NOT_FOUND] == 404
    assert ERROR_STATUS[WorkspaceErrorCode.REVISION_CONFLICT] == 409
    assert ERROR_STATUS[WorkspaceErrorCode.EMBEDDING_UNAVAILABLE] == 503


def test_mock_provider_is_a_structural_workspace_provider():
    provider = MockWorkspaceProvider()
    assert isinstance(provider, WorkspaceProvider)
    signature = inspect.signature(provider.get_workspace)
    assert list(signature.parameters)[0] == "workspace_id"


def test_mock_provider_crud_flow_and_revision_conflict():
    provider = MockWorkspaceProvider()
    scope = _Scope()
    workspace = provider.seed_workspace("Canvas draft")
    assert provider.get_workspace(workspace.workspace_id, scope).name == "Canvas draft"

    created = provider.create_file(workspace.workspace_id, scope, name="note.md", content=b"v1")
    assert created.revision == 1
    assert created.index_status.value == "READY"

    content = provider.read_file(workspace.workspace_id, created.file_id, scope)
    assert content.text == "v1"
    assert content.revision_status.value == "current"

    updated = provider.create_file(
        workspace.workspace_id, scope, name="note.md", content=b"v2", overwrite=True, expected_revision=1
    )
    assert updated.revision == 2
    old = provider.read_file(workspace.workspace_id, created.file_id, scope, revision=1)
    assert old.text == "v1"
    assert old.revision_status.value == "superseded"

    with pytest.raises(Exception) as stale:
        provider.create_file(
            workspace.workspace_id, scope, name="note.md", content=b"v3", overwrite=True, expected_revision=1
        )
    assert getattr(stale.value, "status_code") == 409

    ref = provider.register_artifact_reference(
        workspace.workspace_id,
        scope,
        ref=ArtifactReferenceInput(artifact_id="art_1", kind="canvas", file_id=created.file_id),
    )
    assert ref.artifact_id == "art_1"
    assert [r.artifact_ref_id for r in provider.list_artifact_references(workspace.workspace_id, scope)] == [
        ref.artifact_ref_id
    ]


def test_mock_provider_hides_cross_owner_workspace():
    provider = MockWorkspaceProvider()
    workspace = provider.seed_workspace("private")

    class _Other:
        tenant_id = "mock_tenant"
        owner_id = "someone_else"
        service_id = "hd2ai-admin"

    with pytest.raises(Exception) as hidden:
        provider.get_workspace(workspace.workspace_id, _Other())
    assert getattr(hidden.value, "status_code") == 404
    assert provider.check_workspace_access(workspace.workspace_id, _Other()).level.value == "none"


def test_mock_router_roundtrip():
    app = FastAPI()
    app.include_router(build_mock_router(MockWorkspaceProvider()))
    client = TestClient(app)

    created = client.post("/_mock/workspaces", json={"name": "Router mock"})
    assert created.status_code == 200
    workspace_id = created.json()["workspace_id"]

    uploaded = client.post(
        f"/_mock/workspaces/{workspace_id}/files", json={"name": "spec.md", "content": "hello"}
    )
    assert uploaded.status_code == 201
    file_id = uploaded.json()["file_id"]

    listing = client.get(f"/_mock/workspaces/{workspace_id}/files?recursive=true")
    assert listing.json()["total"] == 1
    downloaded = client.get(f"/_mock/workspaces/{workspace_id}/files/{file_id}/content")
    assert downloaded.content == b"hello"
