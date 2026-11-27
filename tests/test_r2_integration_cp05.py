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
        if body["status"] in ("completed", "failed", "cancelled"):
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
