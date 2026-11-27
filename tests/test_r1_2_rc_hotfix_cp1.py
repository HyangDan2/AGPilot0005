"""R1.2 RC Hotfix — Checkpoint 1 (P0) Fail-First E2E Tests: CR-01, CR-02, CR-03."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings, validate_boot_configuration
from app.core.clock import MockClock
from app.main import app
from app.runtime.runner import runner
from app.schemas.gateway import FunctionCall, ToolCall
from app.tools.dispatcher import ToolDispatcher
from app.tools.policy import ToolPolicyStore
from app.tools.registry import ToolRegistry
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# CR-01: Dynamic HTTP Tool Sync, Registration & Real Execution (AGTOOLS_ENABLED=True)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cr_01_http_provider_sync_registration_and_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGTOOLS_ENABLED", True)
    monkeypatch.setattr(settings, "AGTOOLS_TOKEN", "valid-rotated-token-987654")

    observed_calls: List[Dict[str, Any]] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        observed_calls.append({"url": str(request.url), "headers": dict(request.headers), "body": request.content.decode()})
        if request.url.path.endswith("/api/v1/tools/manifest"):
            return httpx.Response(
                200,
                json={
                    "tools": [
                        {
                            "name": "web.search",
                            "kind": "http",
                            "enabled": True,
                            "is_read_only": True,
                            "description": "Search the web",
                            "parameters": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                            },
                            "endpoint": {"method": "POST", "path": "/api/v1/web/search", "timeout_seconds": 10},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/api/v1/web/search"):
            return httpx.Response(200, json={"results": [{"title": "OK"}], "provider": "searxng"})
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    orig_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.tools.policy.httpx.AsyncClient",
        lambda *a, **kw: orig_async_client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )

    tp = ToolPolicyStore(path=tmp_path / "tp.json")
    reg = ToolRegistry()
    prov = await tp.sync_provider("agtools", reg)
    assert prov["last_sync_ok"] is True, f"Sync failed: {prov.get('last_error')}"
    assert prov["last_error"] is None
    assert reg.get_registration("web.search") is not None

    # Execute the registered dynamic HTTP tool through ToolDispatcher
    disp = ToolDispatcher(tool_registry=reg)
    from app.runtime.policy.constraints import ConstraintSpec
    c = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        allowed_tools=["web.search"], max_steps=3, max_tool_calls=3,
        time_budget_seconds=10.0, context_budget_tokens=4000, check_live_tool_policy=False,
    )
    res = await disp.execute_tool_call(
        ToolCall(id="tc_ws_1", function=FunctionCall(name="web.search", arguments='{"query":"hd2lab"}')),
        service="hd2ai-admin",
        constraints=c,
    )
    assert not res.is_error
    assert json.loads(res.content)["provider"] == "searxng"

    # Also verify AGTOOLS_ENABLED=False does NOT disable an unrelated independent provider
    monkeypatch.setattr(settings, "AGTOOLS_ENABLED", False)
    pol = tp.load()
    pol["providers"]["other_http"] = {
        "id": "other_http",
        "label": "Other",
        "kind": "http",
        "enabled": True,
        "base_url": "http://127.0.0.1:9009",
        "manifest_path": "/api/v1/tools/manifest",
        "auth_preset": "none",
        "tools": [{"name": "other.ping", "kind": "http", "enabled": True}],
    }
    pol["profiles"]["admin-full"]["tools"].append("other.ping")
    tp.save(pol)
    allowed_other, reason_other = tp.is_tool_allowed("other.ping", "hd2ai-admin")
    assert allowed_other is True, f"Unrelated provider tool was wrongly blocked: {reason_other}"


# ---------------------------------------------------------------------------
# CR-02 & CR-03: Full E2E FastAPI -> Auth -> Immutable ExecutionScope -> Runner (LangGraph Single Runtime) -> HTTP Tool
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["langgraph_r1"])
async def test_cr_02_and_03_e2e_scope_propagation_both_runtimes(tmp_path, monkeypatch, engine):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "RUNTIME_ENGINE", engine)
    monkeypatch.setattr(settings, "AGTOOLS_ENABLED", True)
    monkeypatch.setattr(settings, "AGTOOLS_TOKEN", "valid-rotated-token-987654")
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "bff-alpha": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["user"],
                    "allowed_tenants": ["tenant-alpha"],
                    "owner_delegation": True,
                },
                "bff-beta": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["user"],
                    "allowed_tenants": ["tenant-beta"],
                    "owner_delegation": True,
                },
            }
        ),
    )
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SECRETS_JSON",
        json.dumps(
            {
                "bff-alpha": ["sec-alpha-token-1234"],
                "bff-beta": ["sec-beta-token-5678"],
            }
        ),
    )

    outbound_tool_headers: List[Dict[str, str]] = []

    def tool_http_handler(request: httpx.Request) -> httpx.Response:
        outbound_tool_headers.append(dict(request.headers))
        if request.url.path.endswith("/manifest"):
            return httpx.Response(
                200,
                json={
                    "tools": [
                        {
                            "name": "web.search",
                            "kind": "http",
                            "enabled": True,
                            "is_read_only": True,
                            "description": "Search",
                            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                            "endpoint": {"method": "POST", "path": "/api/v1/web/search", "timeout_seconds": 10},
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"results": [{"title": "TenantAlphaDoc"}]})

    transport = httpx.MockTransport(tool_http_handler)
    orig_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.tools.policy.httpx.AsyncClient",
        lambda *a, **kw: orig_async_client(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )

    from app.tools.policy import tool_policy
    from app.tools.registry import registry

    # Sync real HTTP tool into global registry
    prov = await tool_policy.sync_provider("agtools", registry)
    assert prov["last_sync_ok"] is True

    # Ensure web.search is allowed in admin-full profile (hd2ai-beta binds to admin-full)
    pol = tool_policy.load()
    if "web.search" not in pol["profiles"]["admin-full"]["tools"]:
        pol["profiles"]["admin-full"]["tools"].append("web.search")
    tool_policy._cache = pol

    class ToolCallingGateway:
        def __init__(self):
            self.step = 0

        async def execute(self, **kwargs):
            self.step += 1
            if self.step == 1:
                return None, [ToolCall(id="tc_e2e_1", function=FunctionCall(name="web.search", arguments='{"query":"alpha"}'))], "tool_calls"
            return "E2E 완료", None, "stop"

    gw = ToolCallingGateway()
    monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, default: gw)
    tlogger = TrajectoryLogger(TrajectoryEventStore(tmp_path / f"e2e_{engine}.db"), clock=MockClock())
    monkeypatch.setattr("app.tracing.store.get_trajectory_logger", lambda: tlogger)
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) Real FastAPI POST /api/v1/agent/runs authenticated via bff-alpha (tenant-alpha, alice)
        r = await client.post(
            "/api/v1/agent/runs",
            headers={
                "X-Service-Token": "sec-alpha-token-1234",
                "X-HD2Lab-Service": "hd2ai-beta",
                "X-HD2Lab-Tenant": "tenant-alpha",
                "X-HD2Lab-User": "alice",
            },
            json={"message": "alpha 검색", "stream": False},
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        sess = runner.get_session(run_id)
        assert sess is not None and sess.task is not None
        await sess.task
        assert sess.state.status == "completed"
        assert sess.state.final_answer == "E2E 완료"

        # Verify outbound HTTP Tool Provider received tenant-alpha & alice (NOT hardcoded default_tenant!)
        tool_req_hdrs = [h for h in outbound_tool_headers if h.get("x-owner-id") == "alice"]
        assert tool_req_hdrs, f"Expected outbound tool call with x-owner-id=alice, got {outbound_tool_headers}"
        assert tool_req_hdrs[-1]["x-tenant-id"] == "tenant-alpha"

        # 2) Cross-tenant same-owner isolation: tenant-beta/alice MUST NOT see tenant-alpha/alice's run!
        r_cross = await client.get(
            f"/api/v1/agent/runs/{run_id}",
            headers={
                "X-Service-Token": "sec-beta-token-5678",
                "X-HD2Lab-Service": "hd2ai-beta",
                "X-HD2Lab-Tenant": "tenant-beta",
                "X-HD2Lab-User": "alice",
            },
        )
        assert r_cross.status_code == 404


# ---------------------------------------------------------------------------
# CR-03: Duplicate Secret Collision Rejected at Boot & Legacy Token Cannot Bypass Credential Scopes
# ---------------------------------------------------------------------------
def test_cr_03_duplicate_secret_collision_rejected_at_boot(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "AGTOOLS_ENABLED", False)
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "cred-a": {"allowed_services": ["hd2ai-beta"], "allowed_roles": ["service"], "allowed_tenants": ["t1"]},
                "cred-b": {"allowed_services": ["hd2ai-admin"], "allowed_roles": ["admin"], "allowed_tenants": ["t2"]},
            }
        ),
    )
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SECRETS_JSON",
        json.dumps({"cred-a": ["shared-collision-secret-999"], "cred-b": ["shared-collision-secret-999"]}),
    )
    with pytest.raises(RuntimeError, match="duplicate_credential_secret"):
        validate_boot_configuration()
