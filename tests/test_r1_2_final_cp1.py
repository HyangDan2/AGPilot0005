"""R1.2 Final Closure — Checkpoint 1 (P0) Fail-First Tests: NG-01 & NG-03."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.document import AGToolsHttpAdapter
from app.config import settings
from app.core.auth import ExecutionScope
from app.main import app
from app.tools.policy import ToolPolicyStore, auth_headers
from app.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# NG-01: AGTools Disabled Consistency & Deployment Contract
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ng_01_agtools_disabled_blocks_all_outbound_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGTOOLS_ENABLED", False)
    monkeypatch.setattr(settings, "AGTOOLS_TOKEN", "valid-rotated-token-123456")

    # 1) Document Adapter refuses outbound headers when AGTOOLS_ENABLED=False
    adapter = AGToolsHttpAdapter(base_url="http://127.0.0.1:8006/api/v1", token="valid-rotated-token-123456")
    with pytest.raises(RuntimeError, match="agtools_disabled"):
        adapter._headers()

    # 2) auth_headers refuses agtools_service_token when AGTOOLS_ENABLED=False
    with pytest.raises(RuntimeError, match="agtools_disabled"):
        auth_headers("agtools_service_token")

    # 3) Provider sync skips HTTP call completely when AGTOOLS_ENABLED=False
    store = ToolPolicyStore(path=tmp_path / "tp.json")
    reg = ToolRegistry()
    prov = await store.sync_provider("agtools", reg)
    assert prov["last_error"] == "agtools_disabled"
    assert prov["tools"] == []

    # 4) is_tool_allowed denies document.* when AGTOOLS_ENABLED=False
    allowed, reason = store.is_tool_allowed("document.search", "hd2ai-admin")
    assert allowed is False
    assert reason == "agtools_disabled"

    # 5) Deployment checklist MUST NOT mention unsupported AGTOOLS_SERVICE_TOKENS
    checklist = Path("/Users/jaehyukshim/AGWS/AGPilot0005/docs/DEPLOYMENT_CHECKLIST.md").read_text(encoding="utf-8")
    assert "AGTOOLS_SERVICE_TOKENS" not in checklist
    assert "DOCUMENT_SERVICE_TOKEN" in checklist


# ---------------------------------------------------------------------------
# NG-03: Credential Scope Binding, BFF Delegation, Unregistered Service 403, Run-Scoped HTTP Tool
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ng_03_credential_scope_binding_and_isolation(tmp_path, monkeypatch):
    from app.core.auth import current_execution_scope

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "AGTOOLS_ENABLED", True)
    monkeypatch.setattr(settings, "AGTOOLS_TOKEN", "valid-rotated-token-123456")
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "fixed-beta": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["service"],
                    "allowed_tenants": ["default_tenant"],
                    "owner_delegation": False,
                    "fixed_owner": "beta-system-bot",
                },
                "trusted-bff": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["user"],
                    "allowed_tenants": ["default_tenant"],
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
                "fixed-beta": ["secret-fixed-beta-v2", "secret-fixed-beta-v1"],
                "trusted-bff": ["secret-trusted-bff-v1"],
            }
        ),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) Service spoofing (fixed-beta tries to claim hd2ai-admin) -> 403
        r_svc = await client.get(
            "/api/v1/agent/runs/run-none12345",
            headers={
                "X-Service-Token": "secret-fixed-beta-v2",
                "X-HD2Lab-Service": "hd2ai-admin",
                "X-HD2Lab-User": "victim",
            },
        )
        assert r_svc.status_code == 403

        # 2) Role escalation (fixed-beta tries to claim admiral) -> 403 on admin config
        r_role = await client.get(
            "/api/v1/config/tools/snapshot",
            headers={
                "X-Service-Token": "secret-fixed-beta-v2",
                "X-HD2Lab-Service": "hd2ai-beta",
                "X-HD2Lab-Role": "admiral",
            },
        )
        assert r_role.status_code == 403

        # 3) Non-delegating credential forces fixed_owner (ignores spoofed X-HD2Lab-User)
        r_ok = await client.post(
            "/api/v1/agent/runs",
            headers={
                "X-Service-Token": "secret-fixed-beta-v1",  # rotated previous secret also accepted
                "X-HD2Lab-Service": "hd2ai-beta",
                "X-HD2Lab-User": "spoofed-victim",
            },
            json={"message": "hi", "stream": False},
        )
        assert r_ok.status_code == 202
        rid = r_ok.json()["run_id"]
        from app.runtime.runner import runner
        sess = runner.get_session(rid)
        assert sess is not None and sess.state.owner_id == "beta-system-bot"

        # 4) Trusted BFF with owner_delegation=True delegates verified user within allowed service
        r_bff = await client.post(
            "/api/v1/agent/runs",
            headers={
                "X-Service-Token": "secret-trusted-bff-v1",
                "X-HD2Lab-Service": "hd2ai-beta",
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Role": "admiral",  # still cannot escalate role!
            },
            json={"message": "hi bff", "stream": False},
        )
        assert r_bff.status_code == 202
        sess_bff = runner.get_session(r_bff.json()["run_id"])
        assert sess_bff is not None and sess_bff.state.owner_id == "alice"

    # 5) Unregistered service MUST NOT implicitly fall back to public-safe!
    tp = ToolPolicyStore(path=tmp_path / "tp_unreg.json")
    allowed, reason = tp.is_tool_allowed("system.get_datetime", "unregistered-mystery-svc")
    assert allowed is False
    assert reason == "unregistered_service"

    # 6) Dynamic HTTP tool auth_headers uses verified Run ExecutionScope (not hardcoded agcore/hd2ai-admin)
    scope = ExecutionScope(
        tenant_id="tenant-alpha",
        owner_id="alice",
        service_id="hd2ai-beta",
        role="user",
        authenticated=True,
        auth_source="credential_scope:trusted-bff",
    )
    tok = current_execution_scope.set(scope)
    try:
        hdrs = auth_headers("agtools_service_token")
        assert hdrs["X-Owner-Id"] == "alice"
        assert hdrs["X-Tenant-Id"] == "tenant-alpha"
    finally:
        current_execution_scope.reset(tok)
