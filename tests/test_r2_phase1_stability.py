import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.auth import ExecutionScope
from app.main import app, lifespan
from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import IndexState, WorkspaceLifecycleState
from app.workspace.manager import WorkspaceFailure
from app.workspace.rootfs import AllowedRoot, _safe_scope_segment
from app.workspace.runtime import WorkspaceRuntime


def _scope(tenant="tenant-a", owner="owner-a", service="product-a"):
    return ExecutionScope(
        tenant_id=tenant, owner_id=owner, service_id=service,
        authenticated=True, auth_source="test", role="user",
    )


@pytest.mark.asyncio
async def test_chat_mode_separates_workspace_and_document_runs(monkeypatch):
    scope = _scope()
    monkeypatch.setenv("AGCORE_DURABLE_ANALYSIS_SERVICES", "product-a")
    monkeypatch.setattr("app.api.agent.require_authenticated_caller", lambda _request: scope)
    state = SimpleNamespace(run_id="run-workspace", session_id=None, status="queued")
    create_run = AsyncMock(return_value=SimpleNamespace(state=state, created_at=1.0))
    monkeypatch.setattr("app.api.agent.runner.create_run", create_run)
    monkeypatch.setattr("app.api.agent.runner.start_run", AsyncMock())
    create_analysis = AsyncMock(return_value={"job_id": "ana_" + "1" * 32, "status": "queued"})
    monkeypatch.setattr("app.api.agent.services", lambda _app: SimpleNamespace(create=create_analysis))
    refs = {"context_refs": ["doc_1"], "analysis_refs": ["ana_1"]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for mode in ({}, {"execution_mode": "workspace"}):
            workspace = await client.post("/api/v1/agent/runs", json={
                "message": "Read my workspace", "workspace_id": "ws_1234", **refs, **mode,
            })
            assert workspace.status_code == 202
            assert workspace.json()["run_id"] == "run-workspace"
            selected = create_run.call_args.args[0]
            assert selected.workspace_id == "ws_1234"
            assert selected.context_refs == selected.analysis_refs == []
        create_analysis.assert_not_called()

        for mode in ({}, {"execution_mode": "document_analysis"}):
            deferred = await client.post("/api/v1/agent/runs", json={
                "message": "Read the document", "stream": True, **refs, **mode,
            })
            assert deferred.status_code == 200
            assert "event: run.deferred" in deferred.text
            assert create_analysis.call_args.args[3] == refs["context_refs"]
            assert create_analysis.call_args.args[5] == refs["analysis_refs"]
            assert create_analysis.call_args.kwargs["tenant_id"] == scope.tenant_id

        chat = await client.post("/api/v1/agent/runs", json={
            "message": "Chat with a reference", "execution_mode": "chat", **refs,
        })
        assert chat.status_code == 202
        assert create_run.call_args.args[0].context_refs == refs["context_refs"]
        assert create_analysis.await_count == 2
        for body, error in (
            ({"execution_mode": "workspace"}, "workspace_id_required"),
            ({"execution_mode": "document_analysis"}, "document_references_required"),
            ({"execution_mode": "document_analysis", "workspace_id": "ws_1234", **refs},
             "workspace_execution_mode_required"),
        ):
            invalid = await client.post("/api/v1/agent/runs", json={"message": "test", **body})
            assert invalid.status_code == 422
            assert invalid.json()["detail"] == error

        monkeypatch.setenv("AGCORE_DURABLE_ANALYSIS_SERVICES", "")
        denied = await client.post("/api/v1/agent/runs", json={
            "message": "Read the document", "execution_mode": "document_analysis", **refs,
        })
        assert denied.status_code == 403
        assert create_analysis.await_count == 2


@pytest.mark.asyncio
async def test_app_lifespan_recovers_index_job_and_runs_watcher(monkeypatch, tmp_path):
    config = WorkspaceSettings.for_testing(tmp_path)
    seed = WorkspaceRuntime.build(config)
    scope = _scope()
    workspace = seed.manager.new_workspace(scope, "notes")
    folder = tmp_path / "allowed" / "notes"
    (folder / "pending.md").write_text("Earlier pending content", encoding="utf-8")
    seed.manager.sync_workspace(workspace.workspace_id, scope)
    job = seed.store.claim_next_job(seed.indexer.clock())
    assert job["state"] == IndexState.INDEXING.value

    monkeypatch.setattr("app.workspace.runtime._RUNTIME", None)
    monkeypatch.setattr("app.workspace.runtime.get_settings", lambda: config)
    monkeypatch.setattr("app.main.sync_contracts_from_gateway", AsyncMock())
    monkeypatch.setattr("app.main.tool_policy.sync_all", AsyncMock())
    monkeypatch.setattr("app.tracing.store.shutdown_default_trajectory_logger", lambda **_: None)
    monkeypatch.setenv("AGCORE_WORKSPACE_ENABLED", "true")
    from app.workspace.runtime import get_runtime

    async with lifespan(app):
        runtime = get_runtime()
        assert runtime.watcher.running
        (folder / "new.md").write_text("Newly watched content", encoding="utf-8")
        async with asyncio.timeout(5):
            while runtime.indexer.status(workspace.workspace_id, scope).ready < 2:
                await asyncio.sleep(0.02)
        assert runtime.store.get_job(job["job_id"])["state"] == IndexState.READY.value
        assert runtime.store.count_chunks(workspace.workspace_id) == 2
    assert not runtime.watcher.running
    assert runtime.indexer._worker is None


def test_scoped_root_binding_and_parser_identity(tmp_path):
    base = tmp_path / "allowed"
    config = WorkspaceSettings.for_testing(tmp_path, allowed_roots=(
        AllowedRoot("personal", str(base), scope="owner", is_default=True),
        AllowedRoot("team", str(tmp_path / "shared"), scope="tenant"),
    ))
    runtime = WorkspaceRuntime.build(config)
    alice = _scope()
    bob = _scope(owner="owner-b")
    other_tenant = _scope(tenant="tenant-b")
    workspace = runtime.manager.new_workspace(alice, "notes")
    root = base / alice.tenant_id / alice.owner_id / "notes"
    assert workspace.root.display_path == str(root)
    assert [item.display_path for item in runtime.manager.allowed_roots(bob)] == [
        str(base / bob.tenant_id / bob.owner_id), str(tmp_path / "shared" / bob.tenant_id),
    ]
    for outsider in (bob, other_tenant):
        with pytest.raises(WorkspaceFailure, match="outside every allowed root"):
            runtime.manager.open_folder(outsider, str(root))
    assert _safe_scope_segment("ALICE") != _safe_scope_segment("alice")
    assert _safe_scope_segment("tenant / name") != _safe_scope_segment("tenant_name")

    (root / "notes.md").write_text("Scoped content for parser", encoding="utf-8")
    runtime.manager.sync_workspace(workspace.workspace_id, alice)
    ingestor = runtime.indexer.ingestor
    seen = []

    class RecordingIngestor:
        def for_scope(self, tenant_id, owner_id):
            seen.append((tenant_id, owner_id))
            return ingestor.for_scope(tenant_id, owner_id)

    runtime.indexer.ingestor = RecordingIngestor()
    assert runtime.indexer.run_pending() == 1
    assert seen == [(alice.tenant_id, alice.owner_id)]
    assert runtime.store.count_chunks(workspace.workspace_id) == 1

    # An unavailable binding preserves both data and existing index rows.
    runtime.store.update_workspace(workspace.workspace_id, {"root_path": str(base)})
    with pytest.raises(WorkspaceFailure, match="outside every allowed root"):
        runtime.manager.ensure_open(workspace.workspace_id, alice)
    assert runtime.manager.recover()["workspaces_closed"] == 1
    assert runtime.manager.get_workspace(workspace.workspace_id, alice).lifecycle_state is WorkspaceLifecycleState.CLOSED
    assert runtime.store.count_chunks(workspace.workspace_id) == 1
    assert (root / "notes.md").read_text(encoding="utf-8") == "Scoped content for parser"


def test_truncated_watcher_scan_preserves_existing_file_metadata(tmp_path):
    config = WorkspaceSettings.for_testing(tmp_path)
    runtime = WorkspaceRuntime.build(config)
    scope = _scope()
    workspace = runtime.manager.new_workspace(scope, "notes")
    folder = tmp_path / "allowed" / "notes"
    (folder / "z.md").write_text("Existing file", encoding="utf-8")
    runtime.watcher.poll_once()
    before = runtime.manager.list_files(workspace.workspace_id, scope)[0]

    config.scan_max_entries = 1
    (folder / "a.md").write_text("New file", encoding="utf-8")
    result = runtime.watcher.poll_once()
    assert result[0]["deleted"] == 0
    assert runtime.manager.get_file_metadata(workspace.workspace_id, before.file_id, scope) == before
