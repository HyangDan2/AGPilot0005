"""WO-R2-BACKEND (CP-05): Lean Integration QA Suite (10 End-to-End Backend Scenarios).

Covers:
1. Feature Flag OFF default + normal Chat invariance + AM-06 fail-fast
2. Workspace context binding in AgentRunRequest -> LangGraph single runtime
3. File exploration & reading via LangGraph tool calling (workspace_list_files / workspace_read_file)
4. RAG retrieval & citation answer via LangGraph tool calling (workspace_retrieve)
5. File creation & patching via LangGraph tool calling (workspace_create_file / workspace_patch_file)
6. Chat -> Canvas tool calling + unified SSE `artifact.created` event
7. Real Workspace SSoT -> Canvas load -> edit buffer isolation -> explicit save-to-workspace
8. External disk edit -> live SHA-256 Content Hash conflict (409 content_conflict, no silent overwrite)
9. Tenant/Owner rootfs isolation, identifier collision guard, symlink escape block, read-only load vs save block
10. Local Browser `client_read` / `client_write` unsupported server runtime error contract (never false success)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from httpx import ASGITransport, AsyncClient

from app.canvas.config import load_canvas_settings
from app.canvas.service import CanvasService, get_canvas_service, reset_canvas_service
from app.canvas.workspace import RuntimeWorkspaceProvider, register_runtime_provider_factory
from app.config import settings, validate_boot_configuration
from app.core.auth import ExecutionScope
from app.core.clock import MockClock
from app.main import app
from app.schemas.gateway import FunctionCall, ModelReply, ModelUsage, ToolCall
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger
from app.workspace.config import WorkspaceSettings
from app.workspace.contracts import (
    LocalBrowserWorkspaceRequest,
    NewWorkspaceRequest,
    WorkspaceWriteRequest,
)
from app.workspace.manager import WorkspaceFailure
from app.workspace.rootfs import AllowedRoot, AllowedRootPolicy, RootPolicyError, _safe_scope_segment, parse_allowed_roots
from app.workspace.runtime import WorkspaceRuntime, reset_runtime, set_runtime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_sse(raw_text: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for block in raw_text.strip().split("\n\n"):
        if not block.strip():
            continue
        ev_name: Optional[str] = None
        data_lines: List[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if ev_name or data_lines:
            raw = "\n".join(data_lines)
            try:
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {"raw": raw}
            events.append({"event": ev_name, "data": parsed})
    return events


async def _wait_terminal(client: AsyncClient, run_id: str, headers: Dict[str, str], timeout: float = 5.0) -> Dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        r = await client.get(f"/api/v1/agent/runs/{run_id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("completed", "failed", "cancelled", "partial"):
            return body
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Run {run_id} did not finish: {body}")
        await asyncio.sleep(0.02)


class ScriptedGateway:
    def __init__(self, script: List[Dict[str, Any]]):
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    async def execute(
        self,
        messages,
        tools=None,
        tool_choice=None,
        stream=False,
        reasoning_effort=None,
        on_reasoning_delta=None,
        on_content_delta=None,
        _structured=False,
        **kwargs,
    ):
        idx = len(self.calls)
        self.calls.append(
            {
                "messages": list(messages),
                "tools": [t.function.name for t in (tools or [])],
                "tool_choice": tool_choice,
            }
        )
        step = self.script[min(idx, len(self.script) - 1)]
        content = step.get("content")
        tool_calls = step.get("tool_calls") or []
        finish_reason = step.get("finish_reason", "tool_calls" if tool_calls else "stop")
        if stream and on_content_delta and content:
            await on_content_delta(content)
        if _structured:
            return ModelReply(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                model_label="r2-int-mock",
                usage=ModelUsage(prompt_tokens=15, completion_tokens=10, total_tokens=25),
            )
        return content, tool_calls or None, finish_reason


def _proxy_headers(user: str = "alice", tenant: str = "tenant-a", service: str = "hd2ai-admin", role: str = "user") -> Dict[str, str]:
    return {
        "X-Trusted-Proxy-Secret": "r2-int-proxy-secret",
        "X-HD2Lab-User": user,
        "X-HD2Lab-Service": service,
        "X-HD2Lab-Tenant": tenant,
        "X-HD2Lab-Role": role,
    }


def _scope(user: str = "alice", tenant: str = "tenant-a", service: str = "hd2ai-admin", role: str = "user") -> ExecutionScope:
    return ExecutionScope(
        authenticated=True,
        auth_source="trusted_proxy",
        tenant_id=tenant,
        owner_id=user,
        service_id=service,
        role=role,
    )


def _setup_r2_env(
    monkeypatch,
    tmp_path: Path,
    gw: Any,
    *,
    workspace_enabled: bool = True,
    canvas_enabled: bool = True,
    canvas_tools_enabled: bool = True,
    allowed_roots: Optional[tuple] = None,
) -> tuple[WorkspaceRuntime, CanvasService]:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_SECRET", "r2-int-proxy-secret")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "127.0.0.1,::1,testclient")
    monkeypatch.setattr(settings, "CREDENTIAL_SCOPES_JSON", "")
    monkeypatch.setattr(settings, "CREDENTIAL_SECRETS_JSON", "")
    monkeypatch.setattr(settings, "RUNTIME_ENGINE", "langgraph_r1")

    monkeypatch.setenv("AGCORE_WORKSPACE_ENABLED", "true" if workspace_enabled else "false")
    monkeypatch.setenv("AGCORE_CANVAS_ENABLED", "true" if canvas_enabled else "false")
    monkeypatch.setenv("AGCORE_CANVAS_TOOLS_ENABLED", "true" if canvas_tools_enabled else "false")
    monkeypatch.setenv("AGCORE_CANVAS_WORKSPACE_PROVIDER", "workspace")

    tstore = TrajectoryEventStore(tmp_path / "traj.db")
    tlogger = TrajectoryLogger(tstore, clock=MockClock())
    monkeypatch.setattr("app.tracing.store.get_trajectory_logger", lambda: tlogger)
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)
    monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw)
    monkeypatch.setattr("app.runtime.graph.gateway_client", gw)

    async def _no_contract(client, svc):
        return None, False

    monkeypatch.setattr("app.core.runtime_config.get_effective_route_budget", _no_contract)

    ws_root = tmp_path / "allowed"
    ws_root.mkdir(parents=True, exist_ok=True)
    ws_settings = WorkspaceSettings.for_testing(
        tmp_path / "ws",
        enabled=workspace_enabled,
        allowed_roots=allowed_roots
        or (
            AllowedRoot(root_id="rw_root", path=str(ws_root), scope="global", writable=True, is_default=True),
        ),
    )
    ws_rt = WorkspaceRuntime.build(ws_settings)
    set_runtime(ws_rt)
    register_runtime_provider_factory(lambda: ws_rt.manager)

    reset_canvas_service()
    canvas_svc: Optional[CanvasService] = None
    if canvas_enabled:
        canvas_settings = load_canvas_settings(data_dir=tmp_path / "canvas_data")
        canvas_svc = get_canvas_service(canvas_settings, reset=True)
    return ws_rt, canvas_svc


@pytest.fixture(autouse=True)
def _cleanup_r2_singletons():
    yield
    reset_runtime()
    reset_canvas_service()
    register_runtime_provider_factory(None)
    from app.tools.registry import registry

    for name in list(registry._tools.keys()):
        if name.startswith("workspace_") or name.startswith("canvas."):
            registry.unregister(name)


@pytest.mark.asyncio
async def test_hotfix07_managed_binding_persists_and_never_switches_workspace(monkeypatch, tmp_path):
    workspace, _ = _setup_r2_env(monkeypatch, tmp_path, ScriptedGateway([{"content": "OK"}]))
    scope = _scope()
    headers = _proxy_headers()
    folder_a = workspace.manager.new_workspace(scope, "folder-a")
    folder_b = workspace.manager.new_workspace(scope, "folder-b")
    bind_path = "/api/v1/conversations/managed-a/workspace"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.put(bind_path, headers=headers, json={})).status_code == 422
        for _ in range(2):
            result = await client.put(bind_path, headers=headers, json={"workspace_id": folder_a.workspace_id})
            assert result.status_code == 200
            assert result.json() == {"conversation_id": "managed-a", "workspace_id": folder_a.workspace_id}

        set_runtime(WorkspaceRuntime.build(workspace.settings))
        for other in (None, folder_b.workspace_id):
            conflict = await client.put(bind_path, headers=headers, json={"workspace_id": other})
            assert conflict.status_code == 409
            assert conflict.json()["detail"] == "conversation_workspace_conflict"
            rejected_run = await client.post("/api/v1/agent/runs", headers=headers, json={
                "message": "Change folder", "conversation_id": "managed-a", "workspace_id": other,
            })
            assert rejected_run.status_code == 409

        for isolated_headers in (
            _proxy_headers(user="bob"), _proxy_headers(tenant="tenant-b"),
            _proxy_headers(service="hd2ai-beta"),
        ):
            unauthorized = await client.put(bind_path, headers=isolated_headers, json={
                "workspace_id": folder_a.workspace_id,
            })
            assert unauthorized.status_code == 404
            distinct = await client.put(bind_path, headers=isolated_headers, json={"workspace_id": None})
            assert distinct.status_code == 200 and distinct.json()["workspace_id"] is None

        no_assignment = await client.post("/api/v1/agent/runs", headers=headers, json={
            "message": "Legacy message", "conversation_id": "legacy-unbound",
        })
        assert no_assignment.status_code == 202
        assert (await _wait_terminal(client, no_assignment.json()["run_id"], headers))["status"] == "completed"
        assert workspace.store.find_conversation_workspace(
            scope.tenant_id, scope.owner_id, scope.service_id, "legacy-unbound",
        ) is None

        general = await client.put(
            "/api/v1/conversations/managed-general/workspace", headers=headers, json={"workspace_id": None},
        )
        assert general.status_code == 200
        switch = await client.post("/api/v1/agent/runs", headers=headers, json={
            "message": "Attach folder", "conversation_id": "managed-general", "bind_workspace": True,
            "workspace_id": folder_a.workspace_id,
        })
        assert switch.status_code == 409

        competing = await asyncio.gather(*(
            client.put("/api/v1/conversations/managed-race/workspace", headers=headers, json={"workspace_id": value})
            for value in (folder_a.workspace_id, folder_b.workspace_id)
        ))
        assert sorted(result.status_code for result in competing) == [200, 409]

        workspace.manager.close_workspace(folder_a.workspace_id, scope)
        closed = await client.post("/api/v1/agent/runs", headers=headers, json={
            "message": "Resume", "conversation_id": "managed-a",
        })
        assert closed.status_code == 409 and closed.json()["detail"] == "workspace_closed"
        monkeypatch.setattr("app.workspace.binding.get_settings", lambda: workspace.settings)
        monkeypatch.setenv("AGCORE_WORKSPACE_ENABLED", "false")
        disabled = await client.post("/api/v1/agent/runs", headers=headers, json={
            "message": "Resume", "conversation_id": "managed-a",
        })
        assert disabled.status_code == 403 and disabled.json()["detail"] == "workspace_disabled_for_service"


@pytest.mark.asyncio
async def test_hotfix07_omni_tools_get_bound_scope_without_model_ids(monkeypatch, tmp_path):
    from app.tools.policy import tool_policy
    from app.tools.registry import registry

    class CapturedSchemas(ScriptedGateway):
        async def execute(self, messages, tools=None, **kwargs):
            self.schemas = {t.function.name: t.function.parameters for t in tools or []}
            if tools:
                self.initial_schemas = self.schemas
            return await super().execute(messages, tools=tools, **kwargs)

    def call(name, arguments, call_id):
        return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=json.dumps(arguments)))

    gateway = CapturedSchemas([])
    monkeypatch.setenv("AGCORE_CANVAS_WORKSPACE_TOOLS_ENABLED", "true")
    workspace, canvas = _setup_r2_env(monkeypatch, tmp_path, gateway)
    scope = _scope()
    headers = _proxy_headers()
    folder = workspace.manager.new_workspace(scope, "folder-a")
    other = workspace.manager.new_workspace(scope, "folder-b")
    workspace.manager.write_file(folder.workspace_id, scope, WorkspaceWriteRequest(
        path="notes.md", content="distinctive dummy notes for search", create_parents=True,
    ))
    workspace.indexer.run_pending()
    foreign_artifact, *_ = canvas.manager.create(
        scope, title="Other session", artifact_type="markdown", content="private for other session",
        conversation_id="other-session", workspace_id=other.workspace_id,
    )

    async def external_workspace(workspace_id):
        return {"workspace_id": workspace_id}

    parameters = {
        "type": "object", "properties": {"workspace_id": {"type": "string"}}, "required": ["workspace_id"],
    }
    registry.register("test.external", "Independent test context", parameters, external_workspace)
    original_allow = tool_policy.is_tool_allowed
    monkeypatch.setattr(tool_policy, "is_tool_allowed", lambda name, service: (
        (True, None) if name == "test.external" else original_allow(name, service)
    ))
    gateway.script = [
        {"tool_calls": [call("workspace_list_files", {}, "tc-list")]},
        {"tool_calls": [call("workspace_read_file", {"path": "notes.md"}, "tc-read")]},
        {"tool_calls": [call("workspace_retrieve", {"query": "distinctive", "mode": "keyword"}, "tc-rag")]},
        {"tool_calls": [call("workspace_list_files", {"workspace_id": other.workspace_id}, "tc-wrong-folder")]},
        {"tool_calls": [call("canvas.read_artifact", {"artifact_id": foreign_artifact.artifact_id}, "tc-wrong-artifact")]},
        {"tool_calls": [call("canvas.create_artifact", {
            "title": "Scoped artifact", "artifact_type": "markdown", "content": "hello world",
        }, "tc-create")]},
        {"content": "Complete"},
    ]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            conversation_id = "managed-tools"
            assert (await client.put(f"/api/v1/conversations/{conversation_id}/workspace", headers=headers, json={
                "workspace_id": folder.workspace_id,
            })).status_code == 200
            result = await client.post("/api/v1/agent/runs", headers=headers, json={
                "message": "Use my folder", "stream": True, "conversation_id": conversation_id,
            })
            assert result.status_code == 200
            events = _parse_sse(result.text)
            assert any(event["event"] == "run.completed" for event in events), result.text
            traces = await _wait_terminal(client, events[0]["data"]["run_id"], headers)
            by_id = {trace["tool_call_id"]: trace for trace in traces["traces"]}
            for call_id in ("tc-list", "tc-read", "tc-rag", "tc-create"):
                assert by_id[call_id]["status"] == "completed"
                assert by_id[call_id]["arguments"]["workspace_id"] == folder.workspace_id
            assert by_id["tc-wrong-folder"]["error_code"] == "conversation_workspace_conflict"
            assert by_id["tc-wrong-artifact"]["error_code"] == "conversation_workspace_conflict"
            assert "workspace_id" not in gateway.initial_schemas["workspace_read_file"]["properties"]
            assert "workspace_id" not in gateway.initial_schemas["canvas.load_from_workspace"]["required"]
            assert gateway.initial_schemas["test.external"] == parameters
            artifact_event = next(event["data"] for event in events if event["event"] == "artifact.created")
            artifact_id = artifact_event["artifact_id"]
            assert artifact_event["conversation_id"] == conversation_id
            artifact = canvas.manager.get(scope, artifact_id)["artifact"]
            assert artifact["workspace_id"] == folder.workspace_id
            assert artifact["conversation_id"] == conversation_id

            wrong_save = await client.post(f"/api/v1/canvas/artifacts/{artifact_id}/save-to-workspace", headers=headers, json={
                "workspace_id": other.workspace_id, "path": "wrong.md",
            })
            assert wrong_save.status_code == 409
            valid_save = await client.post(f"/api/v1/canvas/artifacts/{artifact_id}/save-to-workspace", headers=headers, json={
                "path": "scoped.md",
            })
            assert valid_save.status_code == 200 and valid_save.json()["workspace_id"] == folder.workspace_id
            assert (await client.get("/api/v1/canvas/artifacts", headers=headers, params={
                "conversation_id": conversation_id, "workspace_id": other.workspace_id,
            })).status_code == 409

            second = "managed-tools-second"
            assert (await client.put(f"/api/v1/conversations/{second}/workspace", headers=headers, json={
                "workspace_id": folder.workspace_id,
            })).status_code == 200
            loaded = []
            for selected in (conversation_id, second, second):
                response = await client.post("/api/v1/canvas/artifacts/from-workspace", headers=headers, json={
                    "workspace_id": folder.workspace_id, "conversation_id": selected, "path": "notes.md",
                })
                assert response.status_code == 201, response.text
                body = response.json()
                assert body["artifact"]["conversation_id"] == selected
                loaded.append(body["artifact"]["artifact_id"])
            assert loaded[0] != loaded[1] == loaded[2]

            general = "managed-general-tools"
            assert (await client.put(f"/api/v1/conversations/{general}/workspace", headers=headers, json={
                "workspace_id": None,
            })).status_code == 200
            gateway.calls.clear()
            gateway.script = [
                {"tool_calls": [call("workspace_list_files", {}, "tc-general-workspace")]},
                {"tool_calls": [call("canvas.create_artifact", {
                    "title": "General artifact", "artifact_type": "markdown", "content": "general hello",
                }, "tc-general-canvas")]},
                {"content": "Complete"},
            ]
            general_run = await client.post("/api/v1/agent/runs", headers=headers, json={
                "message": "General chat", "conversation_id": general, "stream": True,
            })
            assert general_run.status_code == 200
            assert "workspace_list_files" not in gateway.initial_schemas
            assert "canvas.save_to_workspace" not in gateway.initial_schemas
            general_events = _parse_sse(general_run.text)
            general_created = next(event["data"] for event in general_events if event["event"] == "artifact.created")
            saved = canvas.manager.get(scope, general_created["artifact_id"])["artifact"]
            assert saved["conversation_id"] == general and saved["workspace_id"] is None
    finally:
        registry.unregister("test.external")


# ---------------------------------------------------------------------------
# Scenario 1: Feature Flag OFF default + Normal Chat + AM-06 Fail-Fast
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cp05_01_feature_flags_off_normal_chat_and_am06(monkeypatch, tmp_path):
    gw = ScriptedGateway([{"content": "기본 R1.3 일반 대화 응답입니다."}])
    _setup_r2_env(
        monkeypatch,
        tmp_path,
        gw,
        workspace_enabled=False,
        canvas_enabled=False,
        canvas_tools_enabled=False,
    )
    hdr = _proxy_headers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Workspace & Canvas APIs both fail-closed with 503 when flags are OFF
        rw = await client.get("/api/v1/workspaces", headers=hdr)
        assert rw.status_code == 503
        assert rw.json()["detail"]["code"] == "workspace_disabled"

        rc = await client.get("/api/v1/canvas/capabilities", headers=hdr)
        assert rc.status_code == 503
        assert rc.json()["detail"]["code"] == "canvas_disabled"

        # Normal R1.3 Chat works identically with ZERO R2 tools exposed
        r = await client.post("/api/v1/agent/runs", json={"message": "안녕하세요", "stream": False}, headers=hdr)
        assert r.status_code == 202
        final = await _wait_terminal(client, r.json()["run_id"], hdr)
        assert final["status"] == "completed"
        assert final["answer"] == "기본 R1.3 일반 대화 응답입니다."
        exposed = set(gw.calls[0]["tools"])
        assert not any(t.startswith("workspace_") or t.startswith("canvas.") for t in exposed)

    # AM-06: AGCORE_RUNTIME_ENGINE=legacy still fails fast
    monkeypatch.setattr(settings, "RUNTIME_ENGINE", "legacy")
    with pytest.raises(RuntimeError, match="AGCORE_RUNTIME_ENGINE=legacy"):
        validate_boot_configuration()


# ---------------------------------------------------------------------------
# Scenario 2 & 3: Workspace Context Binding + File Explore & Read via LangGraph
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cp05_02_03_workspace_context_explore_and_read(monkeypatch, tmp_path):
    ws_rt, _ = _setup_r2_env(monkeypatch, tmp_path, ScriptedGateway([]))
    scope = _scope()
    ws_dir = tmp_path / "allowed" / "project-alpha"
    ws_dir.mkdir(parents=True, exist_ok=True)
    ws = ws_rt.manager.open_folder(scope, str(ws_dir), name="project-alpha", root_id="rw_root")
    ws_rt.manager.write_file(
        ws.workspace_id,
        scope,
        WorkspaceWriteRequest(path="docs/overview.md", content="# Project Alpha\n핵심 목표: R2 통합 완료."),
    )

    gw = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_ws_list",
                        function=FunctionCall(
                            name="workspace_list_files",
                            arguments=json.dumps({"workspace_id": ws.workspace_id, "recursive": True}),
                        ),
                    )
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_ws_read",
                        function=FunctionCall(
                            name="workspace_read_file",
                            arguments=json.dumps({"workspace_id": ws.workspace_id, "path": "docs/overview.md"}),
                        ),
                    )
                ],
            },
            {"content": "docs/overview.md 확인 결과 핵심 목표는 R2 통합 완료입니다."},
        ]
    )
    monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw)
    hdr = _proxy_headers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/runs",
            json={"message": "워크스페이스 파일 목록과 overview 내용 알려줘", "workspace_id": ws.workspace_id, "stream": False},
            headers=hdr,
        )
        assert r.status_code == 202
        final = await _wait_terminal(client, r.json()["run_id"], hdr)
        assert final["status"] == "completed"
        assert final["usage"]["tool_calls"] == 2
        assert [t["tool_name"] for t in final["traces"]] == ["workspace_list_files", "workspace_read_file"]
        assert "R2 통합 완료" in final["answer"]

    # Verify all 11 workspace tools were exposed to the model and workspace context was injected
    assert "workspace_list_files" in gw.calls[0]["tools"]
    assert "workspace_retrieve" in gw.calls[0]["tools"]
    assert "workspace_create_file" in gw.calls[0]["tools"]
    sys_text = "\n".join(m.content or "" for m in gw.calls[0]["messages"] if m.role == "system")
    assert ws.workspace_id in sys_text


# ---------------------------------------------------------------------------
# Scenario 4: RAG Retrieval & Citation Answer via LangGraph Tool Calling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cp05_04_workspace_rag_retrieve_via_langgraph(monkeypatch, tmp_path):
    ws_rt, _ = _setup_r2_env(monkeypatch, tmp_path, ScriptedGateway([]))
    scope = _scope()
    ws_dir = tmp_path / "allowed" / "rag-ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    ws = ws_rt.manager.open_folder(scope, str(ws_dir), name="rag-ws", root_id="rw_root")
    written = ws_rt.manager.write_file(
        ws.workspace_id,
        scope,
        WorkspaceWriteRequest(
            path="handbook/policy.md",
            content="# 보안 정책\n모든 프로덕션 워크스페이스는 테넌트별 폴더로 격리되어야 하며 심볼릭 링크 탈출을 금지합니다.",
        ),
    )
    assert written.file is not None
    # Index synchronously for deterministic retrieval
    ws_rt.indexer.run_pending(workspace_id=ws.workspace_id)

    gw = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_rag_1",
                        function=FunctionCall(
                            name="workspace_retrieve",
                            arguments=json.dumps({"workspace_id": ws.workspace_id, "query": "테넌트 격리 보안 정책"}),
                        ),
                    )
                ],
            },
            {"content": "보안 정책 문서(handbook/policy.md)에 따르면 테넌트별 폴더 격리와 심볼릭 링크 금지가 필수입니다."},
        ]
    )
    monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw)
    hdr = _proxy_headers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/runs",
            json={"message": "보안 정책 알려줘", "workspace_id": ws.workspace_id, "stream": False},
            headers=hdr,
        )
        assert r.status_code == 202
        final = await _wait_terminal(client, r.json()["run_id"], hdr)
        assert final["status"] == "completed"
        assert final["traces"][0]["tool_name"] == "workspace_retrieve"
        assert final["traces"][0]["status"] == "completed"

    tool_msg = next(m for m in gw.calls[1]["messages"] if m.role == "tool")
    payload = json.loads(tool_msg.content or "{}")
    assert payload.get("status") == "ok"
    assert len(payload["result"].get("citations") or payload["result"].get("chunks") or []) >= 1


# ---------------------------------------------------------------------------
# Scenario 5: Workspace File Creation & Patching via LangGraph
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cp05_05_workspace_create_and_patch_via_langgraph(monkeypatch, tmp_path):
    ws_rt, _ = _setup_r2_env(monkeypatch, tmp_path, ScriptedGateway([]))
    scope = _scope()
    ws_dir = tmp_path / "allowed" / "edit-ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    ws = ws_rt.manager.open_folder(scope, str(ws_dir), name="edit-ws", root_id="rw_root")

    gw = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_create",
                        function=FunctionCall(
                            name="workspace_create_file",
                            arguments=json.dumps(
                                {
                                    "workspace_id": ws.workspace_id,
                                    "path": "notes/plan.md",
                                    "content": "# Plan\nStatus: DRAFT\n",
                                }
                            ),
                        ),
                    )
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_patch",
                        function=FunctionCall(
                            name="workspace_patch_file",
                            arguments=json.dumps(
                                {
                                    "workspace_id": ws.workspace_id,
                                    "path": "notes/plan.md",
                                    "edits": [{"find": "Status: DRAFT", "replace": "Status: APPROVED"}],
                                }
                            ),
                        ),
                    )
                ],
            },
            {"content": "notes/plan.md 파일을 생성하고 APPROVED로 수정했습니다."},
        ]
    )
    monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw)
    hdr = _proxy_headers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/runs",
            json={"message": "plan.md 생성 후 승인 상태로 변경해줘", "workspace_id": ws.workspace_id, "stream": False},
            headers=hdr,
        )
        assert r.status_code == 202
        final = await _wait_terminal(client, r.json()["run_id"], hdr)
        assert final["status"] == "completed"
        assert [t["tool_name"] for t in final["traces"]] == ["workspace_create_file", "workspace_patch_file"]

    disk_file = Path(ws.root.display_path) / "notes" / "plan.md"
    assert disk_file.is_file()
    assert "Status: APPROVED" in disk_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Scenario 6: Chat -> Canvas Auto Tool Calling + Unified SSE `artifact.created`
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cp05_06_chat_to_canvas_sse_artifact_event(monkeypatch, tmp_path):
    gw = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_canvas_1",
                        function=FunctionCall(
                            name="canvas.create_artifact",
                            arguments=json.dumps(
                                {
                                    "title": "R2 Release Notes",
                                    "artifact_type": "markdown",
                                    "content": "# R2 Release\n- Workspace + Canvas unified\n",
                                }
                            ),
                        ),
                    )
                ],
            },
            {"content": "캔버스에 R2 Release Notes 문서를 생성했습니다."},
        ]
    )
    _setup_r2_env(monkeypatch, tmp_path, gw)
    hdr = _proxy_headers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rs = await client.post(
            "/api/v1/agent/runs",
            json={"message": "릴리즈 노트를 캔버스에 작성해줘", "stream": True},
            headers=hdr,
        )
        assert rs.status_code == 200
        events = _parse_sse(rs.text)
        ev_names = [e["event"] for e in events]
        assert "artifact.created" in ev_names
        assert ev_names[-1] == "run.completed"
        art_ev = next(e for e in events if e["event"] == "artifact.created")
        assert art_ev["data"]["artifact_id"]
        assert art_ev["data"]["title"] == "R2 Release Notes"


# ---------------------------------------------------------------------------
# Scenario 7 & 8: Real Workspace SSoT -> Canvas Edit Buffer Isolation ->
#                 Explicit Save + External Disk Edit Hash Conflict (409)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cp05_07_08_workspace_canvas_roundtrip_and_external_hash_conflict(monkeypatch, tmp_path):
    ws_rt, _ = _setup_r2_env(monkeypatch, tmp_path, ScriptedGateway([]))
    scope = _scope()
    ws_dir = tmp_path / "allowed" / "sync-ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    ws = ws_rt.manager.open_folder(scope, str(ws_dir), name="sync-ws", root_id="rw_root")
    ws_rt.manager.write_file(
        ws.workspace_id,
        scope,
        WorkspaceWriteRequest(path="spec.md", content="# Spec v1\nOriginal line.\n"),
    )
    disk_file = Path(ws.root.display_path) / "spec.md"
    hdr = _proxy_headers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 7a. Load real Workspace file into Canvas via SSoT RuntimeWorkspaceProvider
        rl = await client.post(
            "/api/v1/canvas/artifacts/from-workspace",
            json={"workspace_id": ws.workspace_id, "path": "spec.md"},
            headers=hdr,
        )
        assert rl.status_code == 201, rl.text
        artifact_id = rl.json()["artifact"]["artifact_id"]

        # 7b. Edit Canvas revision — disk file MUST remain untouched until explicit save!
        rp = await client.post(
            f"/api/v1/canvas/artifacts/{artifact_id}/patches",
            json={
                "base_revision": 1,
                "operation": "replace_content",
                "replacement_text": "# Spec v2\nEdited in Canvas.\n",
            },
            headers=hdr,
        )
        assert rp.status_code == 200, rp.text
        assert "Original line." in disk_file.read_text(encoding="utf-8")

        # 7c. Explicit Save to Workspace -> disk file updated
        rs = await client.post(
            f"/api/v1/canvas/artifacts/{artifact_id}/save-to-workspace",
            json={"workspace_id": ws.workspace_id, "path": "spec.md"},
            headers=hdr,
        )
        assert rs.status_code == 200, rs.text
        assert rs.json()["save_state"] == "saved"
        assert "Edited in Canvas." in disk_file.read_text(encoding="utf-8")

        # 8. External modification directly on disk (outside AGCore!)
        disk_file.write_text("# Spec v3\nModified externally by IDE!\n", encoding="utf-8")

        # Another Canvas edit + save MUST fail with 409 content_conflict and preserve external disk edit!
        rp2 = await client.post(
            f"/api/v1/canvas/artifacts/{artifact_id}/patches",
            json={
                "base_revision": 2,
                "operation": "replace_content",
                "replacement_text": "# Spec stale overwrite attempt\n",
            },
            headers=hdr,
        )
        assert rp2.status_code == 200
        rs_conflict = await client.post(
            f"/api/v1/canvas/artifacts/{artifact_id}/save-to-workspace",
            json={"workspace_id": ws.workspace_id, "path": "spec.md"},
            headers=hdr,
        )
        assert rs_conflict.status_code == 409, rs_conflict.text
        assert rs_conflict.json()["detail"]["code"] == "content_conflict"
        assert "Modified externally by IDE!" in disk_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Scenario 9: Tenant/Owner Isolation, Collision Guard, Symlink Escape, Read-Only Policy
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cp05_09_security_isolation_symlink_and_readonly_policy(monkeypatch, tmp_path):
    # 9a. Default rootfs includes {tenant}/{owner} and prevents identifier collisions
    default_roots = parse_allowed_roots("", default_root=tmp_path / "defaults")
    assert "{tenant}" in default_roots[0].path and "{owner}" in default_roots[0].path
    assert _safe_scope_segment("alice/bob") != _safe_scope_segment("alice_bob")
    assert _safe_scope_segment("alice:bob") != _safe_scope_segment("alice_bob")
    assert _safe_scope_segment("..") not in {".", ".."}

    # 9b. Symlink escape inside expanded allowed root MUST be rejected
    base = tmp_path / "roots"
    tenant_dir = base / "tenant-a" / "alice"
    tenant_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside_secret"
    outside.mkdir(parents=True, exist_ok=True)
    link = tenant_dir / "escape_link"
    link.symlink_to(outside)

    policy = AllowedRootPolicy(
        (AllowedRoot(root_id="personal", path=str(base / "{tenant}" / "{owner}"), scope="owner", writable=True),),
        deny_symlinks=True,
    )
    with pytest.raises(RootPolicyError) as exc_info:
        policy.resolve("tenant-a", "alice", str(link))
    assert exc_info.value.code == "symlink_escape"

    # 9c. Read-only workspace allows Canvas load (201) while refusing save (403 read_only_workspace)
    ro_dir = tmp_path / "ro_allowed" / "readonly_proj"
    ro_dir.mkdir(parents=True, exist_ok=True)
    (ro_dir / "guide.md").write_text("# Read-Only Guide\n", encoding="utf-8")

    ws_rt, _ = _setup_r2_env(
        monkeypatch,
        tmp_path,
        ScriptedGateway([]),
        allowed_roots=(
            AllowedRoot(root_id="ro_root", path=str(tmp_path / "ro_allowed"), scope="global", writable=False, is_default=True),
        ),
    )
    ws_ro = ws_rt.manager.open_folder(
        _scope(),
        str(ro_dir),
        name="readonly_proj",
        root_id="ro_root",
        read_only=True,
    )
    hdr_alice = _proxy_headers("alice", "tenant-a")
    hdr_bob = _proxy_headers("bob", "tenant-b")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Cross-tenant isolation on Workspace
        r_cross = await client.get(f"/api/v1/workspaces/{ws_ro.workspace_id}", headers=hdr_bob)
        assert r_cross.status_code in (403, 404)

        # Read-only load into Canvas succeeds (201), save back is blocked (403 read_only_workspace)
        r_load = await client.post(
            "/api/v1/canvas/artifacts/from-workspace",
            json={"workspace_id": ws_ro.workspace_id, "path": "guide.md"},
            headers=hdr_alice,
        )
        assert r_load.status_code == 201, r_load.text
        aid = r_load.json()["artifact"]["artifact_id"]

        r_save = await client.post(
            f"/api/v1/canvas/artifacts/{aid}/save-to-workspace",
            json={"workspace_id": ws_ro.workspace_id, "path": "guide.md"},
            headers=hdr_alice,
        )
        assert r_save.status_code == 403, r_save.text
        assert r_save.json()["detail"]["code"] == "read_only_workspace"


# ---------------------------------------------------------------------------
# Scenario 10: Local Browser `client_read` / `client_write` Error Contract
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cp05_10_local_browser_client_read_write_fail_explicitly(monkeypatch, tmp_path):
    ws_rt, _ = _setup_r2_env(monkeypatch, tmp_path, ScriptedGateway([]))
    scope = _scope()
    ws = ws_rt.manager.create_local_browser_workspace(
        scope,
        "browser-ws",
        client_handle_id="handle-1",
        read_only=False,
    )

    # Server-side tool call writing to local_browser MUST return explicit error, never ok/saved
    from app.core.auth import current_execution_scope
    from app.tools.registry import registry

    tok = current_execution_scope.set(scope)
    try:
        create_handler = registry.get_handler("workspace_create_file")
        assert create_handler is not None
        out = await create_handler(workspace_id=ws.workspace_id, path="local.md", content="hello")
        assert out.get("error") is True
        assert out.get("code") == "provider_mismatch"
        assert out.get("client_write_required") is True
    finally:
        current_execution_scope.reset(tok)

    # Canvas load from local_browser MUST fail with 400 provider_mismatch + client_read_required
    hdr = _proxy_headers()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rl = await client.post(
            "/api/v1/canvas/artifacts/from-workspace",
            json={"workspace_id": ws.workspace_id, "path": "local.md"},
            headers=hdr,
        )
        assert rl.status_code == 400
        assert rl.json()["detail"]["code"] == "provider_mismatch"
        assert rl.json()["detail"]["context"].get("client_read_required") is True


# ---------------------------------------------------------------------------
# R2-INT-FIX-02: Admin / Beta / Public Service-Level R2 Feature Isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_r2_int_fix_02_beta_only_service_isolation(monkeypatch, tmp_path):
    ws_rt, _ = _setup_r2_env(
        monkeypatch,
        tmp_path,
        ScriptedGateway([]),
        workspace_enabled=True,
        canvas_enabled=True,
        canvas_tools_enabled=True,
    )
    monkeypatch.setenv("AGCORE_WORKSPACE_ALLOWED_SERVICES", "hd2ai-beta")
    monkeypatch.setenv("AGCORE_CANVAS_ALLOWED_SERVICES", "hd2ai-beta")

    # Seed a beta workspace
    beta_scope = _scope("alice", "tenant-a", "hd2ai-beta")
    ws_dir = tmp_path / "allowed" / "beta-only-ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    ws = ws_rt.manager.open_folder(beta_scope, str(ws_dir), name="beta-only-ws", root_id="rw_root")

    hdr_beta = _proxy_headers("alice", "tenant-a", "hd2ai-beta")
    hdr_admin = _proxy_headers("alice", "tenant-a", "hd2ai-admin")
    hdr_public = _proxy_headers("alice", "tenant-a", "hd2ai-public")

    from app.tools.policy import tool_policy

    # Policy surface: beta sees R2 tools, admin & public see ZERO R2 tools
    beta_tools = set(tool_policy.allowed_tools_for_service("hd2ai-beta"))
    admin_tools = set(tool_policy.allowed_tools_for_service("hd2ai-admin"))
    public_tools = set(tool_policy.allowed_tools_for_service("hd2ai-public"))
    assert "workspace_list_files" in beta_tools and "canvas.create_artifact" in beta_tools
    assert not any(t.startswith("workspace_") or t.startswith("canvas.") for t in admin_tools)
    assert not any(t.startswith("workspace_") or t.startswith("canvas.") for t in public_tools)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) hd2ai-beta: Workspace API, Canvas API, workspace_id Chat + Canvas SSE ALL allowed
        assert (await client.get("/api/v1/workspaces", headers=hdr_beta)).status_code == 200
        assert (await client.get("/api/v1/canvas/capabilities", headers=hdr_beta)).status_code == 200

        gw_beta = ScriptedGateway(
            [
                {
                    "content": None,
                    "tool_calls": [
                        ToolCall(
                            id="tc_beta_art",
                            function=FunctionCall(
                                name="canvas.create_artifact",
                                arguments=json.dumps(
                                    {
                                        "title": "Beta Draft",
                                        "artifact_type": "markdown",
                                        "content": "# Beta\nIsolated R2 artifact.\n",
                                    }
                                ),
                            ),
                        )
                    ],
                },
                {"content": "Beta 워크스페이스+캔버스 실행 완료"},
            ]
        )
        monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw_beta)
        rs_beta = await client.post(
            "/api/v1/agent/runs",
            json={"message": "베타 캔버스 생성", "workspace_id": ws.workspace_id, "stream": True},
            headers=hdr_beta,
        )
        assert rs_beta.status_code == 200
        beta_events = _parse_sse(rs_beta.text)
        assert "artifact.created" in [e["event"] for e in beta_events]
        assert beta_events[-1]["event"] == "run.completed"
        assert "workspace_list_files" in gw_beta.calls[0]["tools"]
        assert "canvas.create_artifact" in gw_beta.calls[0]["tools"]

        # 2) hd2ai-admin & hd2ai-public: Workspace/Canvas API -> 403, workspace_id -> 422, Normal Chat -> 200/completed with ZERO R2 tools
        for svc_name, hdr in (("hd2ai-admin", hdr_admin), ("hd2ai-public", hdr_public)):
            r_ws = await client.get("/api/v1/workspaces", headers=hdr)
            assert r_ws.status_code == 403, f"{svc_name}: {r_ws.text}"
            assert r_ws.json()["detail"]["code"] == "workspace_disabled_for_service"

            r_cv = await client.get("/api/v1/canvas/capabilities", headers=hdr)
            assert r_cv.status_code == 403, f"{svc_name}: {r_cv.text}"
            assert r_cv.json()["detail"]["code"] == "canvas_disabled_for_service"

            r_run_ws = await client.post(
                "/api/v1/agent/runs",
                json={"message": "워크스페이스 접근 시도", "workspace_id": ws.workspace_id, "stream": False},
                headers=hdr,
            )
            assert r_run_ws.status_code == 422, f"{svc_name}: {r_run_ws.text}"
            assert r_run_ws.json()["detail"] == "workspace_disabled_for_service"

            gw_plain = ScriptedGateway([{"content": f"{svc_name} 일반 대화 정상 응답"}])
            monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c, g=gw_plain: g)
            r_plain = await client.post(
                "/api/v1/agent/runs",
                json={"message": "일반 인사", "stream": False},
                headers=hdr,
            )
            assert r_plain.status_code == 202
            fin = await _wait_terminal(client, r_plain.json()["run_id"], hdr)
            assert fin["status"] == "completed"
            assert fin["answer"] == f"{svc_name} 일반 대화 정상 응답"
            exposed = set(gw_plain.calls[0]["tools"])
            assert not any(t.startswith("workspace_") or t.startswith("canvas.") for t in exposed), f"{svc_name} leaked R2 tools: {exposed}"


# ---------------------------------------------------------------------------
# R2-INT-FIX-03-BE: Server-Side User Approval & Production Mock/Preview Block
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_r2_int_fix_03_server_side_approval_blocks_llm_confirm_and_allows_user_approval(monkeypatch, tmp_path):
    ws_rt, _ = _setup_r2_env(monkeypatch, tmp_path, ScriptedGateway([]))
    scope = _scope()
    ws_dir = tmp_path / "allowed" / "approval-ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    ws = ws_rt.manager.open_folder(scope, str(ws_dir), name="approval-ws", root_id="rw_root")
    ws_rt.manager.write_file(
        ws.workspace_id,
        scope,
        WorkspaceWriteRequest(path="notes/protected.md", content="ORIGINAL CONTENT"),
    )
    disk_file = Path(ws.root.display_path) / "notes" / "protected.md"
    hdr = _proxy_headers()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) LLM passes overwrite=True & confirm_overwrite=True WITHOUT server-side AgentRunRequest.approvals -> BLOCKED!
        gw_ow_denied = ScriptedGateway(
            [
                {
                    "content": None,
                    "tool_calls": [
                        ToolCall(
                            id="tc_ow_1",
                            function=FunctionCall(
                                name="workspace_create_file",
                                arguments=json.dumps(
                                    {
                                        "workspace_id": ws.workspace_id,
                                        "path": "notes/protected.md",
                                        "content": "HIJACKED",
                                        "overwrite": True,
                                        "confirm_overwrite": True,
                                    }
                                ),
                            ),
                        )
                    ],
                },
                {"content": "덮어쓰기 승인이 필요합니다."},
            ]
        )
        monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw_ow_denied)
        r1 = await client.post(
            "/api/v1/agent/runs",
            json={"message": "덮어써줘", "workspace_id": ws.workspace_id, "stream": False},
            headers=hdr,
        )
        assert r1.status_code == 202
        await _wait_terminal(client, r1.json()["run_id"], hdr)
        tool_msg1 = next(m for m in gw_ow_denied.calls[1]["messages"] if m.role == "tool")
        assert json.loads(tool_msg1.content)["code"] == "approval_required"
        assert disk_file.read_text(encoding="utf-8") == "ORIGINAL CONTENT"

        # 2) LLM passes confirm=True on delete WITHOUT delete approval (even with write_approved!) -> BLOCKED!
        gw_del_denied = ScriptedGateway(
            [
                {
                    "content": None,
                    "tool_calls": [
                        ToolCall(
                            id="tc_del_1",
                            function=FunctionCall(
                                name="workspace_delete_file",
                                arguments=json.dumps(
                                    {
                                        "workspace_id": ws.workspace_id,
                                        "path": "notes/protected.md",
                                        "confirm": True,
                                    }
                                ),
                            ),
                        )
                    ],
                },
                {"content": "삭제 승인이 필요합니다."},
            ]
        )
        monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw_del_denied)
        r2 = await client.post(
            "/api/v1/agent/runs",
            json={
                "message": "삭제해줘",
                "workspace_id": ws.workspace_id,
                "approvals": ["workspace:write_approved"],
                "stream": False,
            },
            headers=hdr,
        )
        assert r2.status_code == 202
        await _wait_terminal(client, r2.json()["run_id"], hdr)
        tool_msg2 = next(m for m in gw_del_denied.calls[1]["messages"] if m.role == "tool")
        assert json.loads(tool_msg2.content)["code"] == "approval_required"
        assert disk_file.is_file()

        # 3) Explicit server-side AgentRunRequest.approvals grants overwrite -> SUCCEEDS!
        gw_ow_ok = ScriptedGateway(
            [
                {
                    "content": None,
                    "tool_calls": [
                        ToolCall(
                            id="tc_ow_2",
                            function=FunctionCall(
                                name="workspace_create_file",
                                arguments=json.dumps(
                                    {
                                        "workspace_id": ws.workspace_id,
                                        "path": "notes/protected.md",
                                        "content": "APPROVED UPDATE",
                                        "overwrite": True,
                                    }
                                ),
                            ),
                        )
                    ],
                },
                {"content": "사용자 승인 후 덮어쓰기 완료."},
            ]
        )
        monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw_ow_ok)
        r3 = await client.post(
            "/api/v1/agent/runs",
            json={
                "message": "승인됨, 덮어써줘",
                "workspace_id": ws.workspace_id,
                "approvals": ["workspace_create_file:notes/protected.md"],
                "stream": False,
            },
            headers=hdr,
        )
        assert r3.status_code == 202
        await _wait_terminal(client, r3.json()["run_id"], hdr)
        assert disk_file.read_text(encoding="utf-8") == "APPROVED UPDATE"

        # 4) Explicit server-side AgentRunRequest.approvals grants delete -> SUCCEEDS!
        gw_del_ok = ScriptedGateway(
            [
                {
                    "content": None,
                    "tool_calls": [
                        ToolCall(
                            id="tc_del_2",
                            function=FunctionCall(
                                name="workspace_delete_file",
                                arguments=json.dumps(
                                    {
                                        "workspace_id": ws.workspace_id,
                                        "path": "notes/protected.md",
                                    }
                                ),
                            ),
                        )
                    ],
                },
                {"content": "사용자 승인 후 삭제 완료."},
            ]
        )
        monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw_del_ok)
        r4 = await client.post(
            "/api/v1/agent/runs",
            json={
                "message": "승인됨, 삭제해줘",
                "workspace_id": ws.workspace_id,
                "approvals": ["workspace_delete_file:notes/protected.md"],
                "stream": False,
            },
            headers=hdr,
        )
        assert r4.status_code == 202
        await _wait_terminal(client, r4.json()["run_id"], hdr)
        tool_msg4 = next(m for m in gw_del_ok.calls[1]["messages"] if m.role == "tool")
        assert json.loads(tool_msg4.content)["status"] == "ok"


@pytest.mark.asyncio
async def test_r2_int_fix_03_mock_and_preview_blocked_in_production(monkeypatch):
    from app.workspace import preview_app
    from app.workspace.mock import MockWorkspaceProvider

    monkeypatch.setenv("AGCORE_ENV", "production")

    with pytest.raises(RuntimeError, match="MockWorkspaceProvider"):
        MockWorkspaceProvider()

    with pytest.raises(RuntimeError, match="Workspace preview app"):
        async with preview_app.lifespan(preview_app.app):
            pass

    async with AsyncClient(transport=ASGITransport(app=preview_app.app), base_url="http://test") as client:
        r_health = await client.get("/health")
        assert r_health.status_code == 403
        assert r_health.json()["detail"]["code"] == "preview_disabled_in_production"

        r_mock = await client.get("/_mock/workspaces")
        assert r_mock.status_code == 403
        assert r_mock.json()["detail"]["code"] == "preview_disabled_in_production"


# ---------------------------------------------------------------------------
# R2-BETA-HOTFIX-02-BE: AgentRunRequest.conversation_id -> Canvas Tool Auto-Bind
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_r2_beta_hotfix_02_conversation_id_propagates_to_canvas_artifact(monkeypatch, tmp_path):
    gw = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_conv_art",
                        function=FunctionCall(
                            name="canvas.create_artifact",
                            arguments=json.dumps(
                                {
                                    "title": "Conversation Bound Artifact",
                                    "artifact_type": "markdown",
                                    "content": "# Auto-bound\nLinked to conversation_id.\n",
                                }
                            ),
                        ),
                    )
                ],
            },
            {"content": "대화에 연결된 아티팩트를 생성했습니다."},
        ]
    )
    _setup_r2_env(monkeypatch, tmp_path, gw)
    hdr = _proxy_headers()
    conv_id = "conv-beta-hf02-001"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rs = await client.post(
            "/api/v1/agent/runs",
            json={
                "message": "아티팩트 만들어줘",
                "conversation_id": conv_id,
                "stream": True,
            },
            headers=hdr,
        )
        assert rs.status_code == 200
        events = _parse_sse(rs.text)
        art_ev = next(e for e in events if e["event"] == "artifact.created")
        created_id = art_ev["data"]["artifact_id"]

        # Verify GET /api/v1/canvas/artifacts?conversation_id=<conv_id> returns the created artifact
        rl = await client.get(f"/api/v1/canvas/artifacts?conversation_id={conv_id}", headers=hdr)
        assert rl.status_code == 200, rl.text
        items = rl.json()["items"]
        assert len(items) == 1
        assert items[0]["artifact_id"] == created_id
        assert items[0]["conversation_id"] == conv_id


# ---------------------------------------------------------------------------
# R2-BETA-HOTFIX-03: Canvas RuntimeEditAdapter -> AI Gateway Propose / Diff / Apply
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_r2_beta_hotfix_03_canvas_runtime_edit_adapter_gateway_propose_diff_apply(monkeypatch, tmp_path):
    monkeypatch.setenv("AGCORE_CANVAS_AI_ADAPTER", "runtime")
    _setup_r2_env(monkeypatch, tmp_path, ScriptedGateway([]))
    hdr = _proxy_headers("alice", "tenant-a", "hd2ai-beta")

    edit_gw = ScriptedGateway([{"content": "# 다듬어진 제목\n명확하고 간결하게 개선된 본문입니다.\n"}])
    monkeypatch.setattr("app.gateway.client.gateway_for_service", lambda svc, client: edit_gw)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) Create artifact
        rc = await client.post(
            "/api/v1/canvas/artifacts",
            json={
                "title": "AI Edit Test",
                "artifact_type": "markdown",
                "content": "# 초안 제목\n좀 어색한 초안 본문입니다.\n",
            },
            headers=hdr,
        )
        assert rc.status_code == 201, rc.text
        aid = rc.json()["artifact"]["artifact_id"]

        # 2) Propose AI edit via RuntimeEditAdapter -> AI Gateway
        rp = await client.post(
            f"/api/v1/canvas/artifacts/{aid}/edit-proposals",
            json={"instruction": "문체를 자연스럽게 다듬어줘", "instruction_kind": "change_style"},
            headers=hdr,
        )
        assert rp.status_code == 201, rp.text
        prop = rp.json()["proposal"]
        assert prop["adapter"] == "runtime"
        assert prop["ai_integration_verified"] is True
        assert prop["model_label"] == "r2-int-mock"
        assert "개선된 본문" in prop["replacement_text"]
        assert prop["preview"]["stats"]["added_lines"] >= 1

        # 3) Apply proposal with explicit user approval
        ra = await client.post(
            f"/api/v1/canvas/edit-proposals/{prop['proposal_id']}/apply",
            json={"user_approved": True},
            headers=hdr,
        )
        assert ra.status_code == 200, ra.text
        assert ra.json()["artifact"]["current_revision"] == 2
        rg = await client.get(f"/api/v1/canvas/artifacts/{aid}", headers=hdr)
        assert rg.status_code == 200
        assert "개선된 본문" in rg.json()["content"]

        # 4) Gateway failure MUST return 503 ai_adapter_unavailable (no mock fallback!)
        class FailingGW:
            async def execute(self, **kwargs):
                raise RuntimeError("upstream_gateway_down")

        monkeypatch.setattr("app.gateway.client.gateway_for_service", lambda svc, client: FailingGW())
        rf = await client.post(
            f"/api/v1/canvas/artifacts/{aid}/edit-proposals",
            json={"instruction": "짧게 요약해줘", "instruction_kind": "adjust_length"},
            headers=hdr,
        )
        assert rf.status_code == 503, rf.text
        assert rf.json()["detail"]["code"] == "ai_adapter_unavailable"


# ---------------------------------------------------------------------------
# R2-BETA-HOTFIX-04-BE: Canvas AI Edit Output Sanitization & Capabilities Verified
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_r2_beta_hotfix_04_edit_output_sanitization_and_capabilities_verified(monkeypatch, tmp_path):
    monkeypatch.setenv("AGCORE_CANVAS_AI_ADAPTER", "runtime")
    _setup_r2_env(monkeypatch, tmp_path, ScriptedGateway([]))
    hdr = _proxy_headers("alice", "tenant-a", "hd2ai-beta")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # (c) /capabilities reports ai_integration_verified=True & edit_adapter_mode="runtime"
        caps = await client.get("/api/v1/canvas/capabilities", headers=hdr)
        assert caps.status_code == 200, caps.text
        ai_edit = caps.json()["ai_edit"]
        assert ai_edit["adapter"] == "runtime"
        assert ai_edit["edit_adapter_mode"] == "runtime"
        assert ai_edit["ai_integration_verified"] is True
        assert ai_edit["dev_preview"] is False

        rc = await client.post(
            "/api/v1/canvas/artifacts",
            json={
                "title": "안내 편지",
                "artifact_type": "markdown",
                "content": "# 안내\n초안 내용입니다.\n",
            },
            headers=hdr,
        )
        assert rc.status_code == 201
        aid = rc.json()["artifact"]["artifact_id"]

        # (a) Strict JSON response -> separates content and summary cleanly
        gw_json = ScriptedGateway(
            [
                {
                    "content": json.dumps(
                        {
                            "content": "# 정중한 안내\n정중하게 다듬어진 본문입니다.",
                            "summary": "문체를 정중하고 간결하게 개선했습니다.",
                        },
                        ensure_ascii=False,
                    )
                }
            ]
        )
        monkeypatch.setattr("app.gateway.client.gateway_for_service", lambda svc, c: gw_json)
        rp1 = await client.post(
            f"/api/v1/canvas/artifacts/{aid}/edit-proposals",
            json={"instruction": "문체 다듬기", "instruction_kind": "change_style"},
            headers=hdr,
        )
        assert rp1.status_code == 201, rp1.text
        p1 = rp1.json()["proposal"]
        assert p1["replacement_text"] == "# 정중한 안내\n정중하게 다듬어진 본문입니다."
        assert p1["change_reason"] == "문체를 정중하고 간결하게 개선했습니다."

        # (b) Preamble + ```markdown ... ``` + trailing '수정 내역:' commentary -> extracts pure body only!
        noisy_reply = (
            "전통적인 서신 형식을 살려 문체를 다듬었습니다.\n\n"
            "```markdown\n"
            "# 따뜻한 안내\n"
            "봄바람과 함께 안부를 전합니다.\n"
            "```\n\n"
            "수정 내역:\n"
            "- 인사말을 부드럽게 변경했습니다.\n"
        )
        gw_noisy = ScriptedGateway([{"content": noisy_reply}])
        monkeypatch.setattr("app.gateway.client.gateway_for_service", lambda svc, c: gw_noisy)
        rp2 = await client.post(
            f"/api/v1/canvas/artifacts/{aid}/edit-proposals",
            json={"instruction": "따뜻하게 다듬어줘", "instruction_kind": "change_style"},
            headers=hdr,
        )
        assert rp2.status_code == 201, rp2.text
        p2 = rp2.json()["proposal"]
        assert p2["replacement_text"] == "# 따뜻한 안내\n봄바람과 함께 안부를 전합니다."
        assert "```" not in p2["replacement_text"]
        assert "수정 내역" not in p2["replacement_text"]
        assert "전통적인 서신" not in p2["replacement_text"]


@pytest.mark.asyncio
async def test_session_attachment_workspace_and_parser_scope(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import httpx
    from app.adapters import document
    from app.tools.policy import tool_policy

    gateway = ScriptedGateway([])
    workspace, _ = _setup_r2_env(monkeypatch, tmp_path, gateway)
    monkeypatch.setenv("AGCORE_SESSION_ATTACHMENT_SERVICES", "hd2ai-beta")
    monkeypatch.setattr(settings, "AGTOOLS_TOKEN", "sandbox-parser-token")
    monkeypatch.setattr(settings, "ALLOW_MOCK_FALLBACK", False)
    headers = _proxy_headers(service="hd2ai-beta")
    scope = _scope(service="hd2ai-beta")
    folder = workspace.manager.new_workspace(scope, "mixed-folder")
    workspace.manager.write_file(folder.workspace_id, scope, WorkspaceWriteRequest(
        path="workspace.md", content="workspace-only-fixture", create_parents=True,
    ))

    uploads = set()
    calls = []

    def agtools(request):
        tenant = request.headers["X-Tenant-Id"]
        assert request.headers.get("X-Access-Scope") == "private"
        key = (tenant, request.headers["X-Owner-Id"])
        calls.append((request.method, request.url.path, key))
        if request.method == "POST" and request.url.path.endswith("/documents"):
            assert b"%PDF" in request.content
            uploads.add(key)
            return httpx.Response(202, json={"document_id": "doc-scoped", "status": "queued"})
        if key not in uploads:
            return httpx.Response(404, json={"detail": "not found"})
        if request.url.path.endswith("/outline"):
            return httpx.Response(200, json={"status": "completed", "filename": "fixture.pdf", "sections": []})
        if request.url.path.endswith("/text"):
            return httpx.Response(200, json={"text": "attachment-only-fixture", "internal_path": "private"})
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"total_matches": 1, "results": [{"snippet": "attachment-only-fixture"}]})
        return httpx.Response(200, json={"status": "completed", "page_count": 1})

    monkeypatch.setattr(document, "httpx", SimpleNamespace(
        AsyncClient=lambda **kwargs: AsyncClient(transport=httpx.MockTransport(agtools), **kwargs),
    ))
    monkeypatch.setattr(document.document_adapter, "http", document.AGToolsHttpAdapter())
    original_allow = tool_policy.is_tool_allowed
    monkeypatch.setattr(tool_policy, "is_tool_allowed", lambda name, service: (
        (True, None) if name == "document.search" else original_allow(name, service)
    ))
    gateway.script = [
        {"tool_calls": [
            ToolCall(id="workspace-1", function=FunctionCall(
                name="workspace_read_file", arguments='{"path":"workspace.md"}')),
            ToolCall(id="attachment-1", function=FunctionCall(
                name="document.search", arguments='{"doc_id":"att-doc-scoped","query":"fixture"}')),
        ]},
        {"content": "both sources"},
    ]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        prefix = "/api/v1/conversations"
        for name, workspace_id in (("one", folder.workspace_id), ("two", folder.workspace_id), ("general", None)):
            response = await client.put(f"{prefix}/{name}/workspace", headers=headers, json={"workspace_id": workspace_id})
            assert response.status_code == 200, response.text
        pdf = {"file": ("fixture.pdf", b"%PDF-synthetic", "application/pdf")}
        response = await client.post(f"{prefix}/one/attachments", headers=headers, files=pdf)
        assert response.status_code == 202, response.text
        attachment = response.json()["document_id"]
        assert attachment == "att-doc-scoped"
        assert (await client.post(f"{prefix}/missing/attachments", headers=headers, files=pdf)).status_code == 404
        for name in ("two", "general"):
            assert (await client.get(f"{prefix}/{name}/attachments/{attachment}/status", headers=headers)).status_code == 404
        other_user = _proxy_headers(user="bob", service="hd2ai-beta")
        assert (await client.get(f"{prefix}/one/attachments/{attachment}/status", headers=other_user)).status_code == 404
        assert (await client.post(f"{prefix}/one/attachments", headers=_proxy_headers(), files=pdf)).status_code == 403
        response = await client.get(f"{prefix}/one/attachments/{attachment}/text", headers=headers)
        assert response.json() == {"document_id": attachment, "text": "attachment-only-fixture"}
        response = await client.post("/api/v1/agent/runs", headers=headers, json={
            "message": "check", "conversation_id": "one", "session_id": "two", "context_refs": [attachment],
        })
        assert response.status_code == 409
        response = await client.post("/api/v1/agent/runs", headers=headers, json={
            "message": "check", "conversation_id": "two", "context_refs": [attachment],
        })
        assert response.status_code == 422
        response = await client.post("/api/v1/agent/runs", headers=headers, json={
            "message": "Use both sources", "conversation_id": "one", "context_refs": [attachment], "stream": True,
        })
        assert response.status_code == 200
        events = _parse_sse(response.text)
        assert any(event["event"] == "run.completed" for event in events), response.text
        status_url = "/api/v1/agent/runs/" + events[0]["data"]["run_id"]
        assert (await client.get(status_url, headers=headers)).status_code == 404
        assert (await client.get(status_url, headers=headers, params={"conversation_id": "two"})).status_code == 404
        assert (await client.get(status_url + "/trajectory", headers=headers)).status_code == 404
        for session in ("two", None):
            response = await client.post(status_url + "/feedback", headers=headers,
                params={"conversation_id": session} if session else {}, json={"feedback_type": "thumbs_up"})
            assert response.status_code == 404
        positive_feedback = await client.post(status_url + "/feedback", headers=headers,
            params={"conversation_id": "one"}, json={"feedback_type": "thumbs_up"})
        assert positive_feedback.status_code == 201, positive_feedback.text
        result = await client.get(status_url, headers=headers, params={"conversation_id": "one"})
        traces = {trace["tool_call_id"]: trace for trace in result.json()["traces"]}
        assert traces["workspace-1"]["status"] == "completed"
        assert traces["attachment-1"]["status"] == "completed"
        assert any(method == "POST" and path.endswith("/search") and key in uploads for method, path, key in calls)

        monkeypatch.setenv("AGCORE_DURABLE_ANALYSIS_SERVICES", "hd2ai-beta")
        from app.runtime.tasks.bootstrap import AnalysisServices

        created = []

        async def create(owner, service, message, refs, history, prior, session, effort):
            created.append((owner, service, refs, session))
            return {"job_id": "ana_" + "a" * 32, "status": "queued"}

        previous = {"owner_id": "tenant-a::alice", "service_id": "hd2ai-beta",
                    "session_id": "general", "document_ids": [attachment], "schema_version": 2}
        management = AnalysisServices(None, SimpleNamespace(create=create), SimpleNamespace(read=lambda _: previous))
        monkeypatch.setattr(app.state, "analysis_services", management, raising=False)
        payload = {"message": "Read PDF", "conversation_id": "general",
                   "execution_mode": "document_analysis", "context_refs": [attachment]}
        assert (await client.post("/api/v1/agent/runs", headers=headers, json=payload)).status_code == 422
        assert not created
        assert (await client.post(f"{prefix}/general/attachments", headers=headers, files=pdf)).status_code == 202
        assert (await client.post("/api/v1/agent/runs", headers=headers, json=payload)).status_code == 202
        assert created == [("tenant-a::alice", "hd2ai-beta", [attachment], "general")]
        assert (await client.get("/api/v1/agent/analyses/ana_" + "a" * 32, headers=headers,
                                 params={"conversation_id": "two"})).status_code == 404
        with pytest.raises(ValueError, match="analysis_reference_unavailable"):
            await management.create(
                "alice", "hd2ai-beta", "Continue", [], analysis_refs=["ana_" + "a" * 32],
                session_id="two", tenant_id="tenant-a",
            )
        previous["document_ids"] = ["legacy-doc"]
        legacy_resume = await client.post("/api/v1/agent/runs", headers=headers, json={
            "message": "Use my prior legacy analysis", "analysis_refs": ["ana_" + "a" * 32],
            "conversation_id": "legacy-unbound",
        })
        assert legacy_resume.status_code == 202, legacy_resume.text
        assert created[-1] == ("tenant-a::alice", "hd2ai-beta", ["legacy-doc"], None)
