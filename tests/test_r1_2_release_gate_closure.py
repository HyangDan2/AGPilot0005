"""R1.2 Release Gate Closure Fail-First Tests: RB-01 & RB-02."""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.core.auth import ExecutionScope
from app.core.clock import MockClock
from app.main import app
from app.runtime.tasks.bootstrap import AnalysisServices
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# RB-01: Block '::' Delimiter Collision in Tenant & Owner Across Auth and Analysis
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rb_01_delimiter_collision_blocked_in_auth_and_analysis(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_SECRET", "proxy-sec-rb01-9999")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "127.0.0.1,::1,testclient")
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "delegated-svc": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["user"],
                    "allowed_tenants": ["tenant-alpha"],
                    "owner_delegation": True,
                }
            }
        ),
    )
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SECRETS_JSON",
        json.dumps({"delegated-svc": ["tok-delegated-rb01-001"]}),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) Trusted Proxy with '::' in X-HD2Lab-User MUST be rejected (403)
        r_owner = await client.get(
            "/api/v1/agent/runs/run-nonexistent1234",
            headers={
                "X-Trusted-Proxy-Secret": "proxy-sec-rb01-9999",
                "X-HD2Lab-User": "evil::alice",
                "X-HD2Lab-Service": "hd2ai-beta",
                "X-HD2Lab-Tenant": "tenant-alpha",
            },
        )
        assert r_owner.status_code == 403

        # 2) Trusted Proxy with '::' in X-HD2Lab-Tenant MUST be rejected (403)
        r_tenant = await client.get(
            "/api/v1/agent/runs/run-nonexistent1234",
            headers={
                "X-Trusted-Proxy-Secret": "proxy-sec-rb01-9999",
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Service": "hd2ai-beta",
                "X-HD2Lab-Tenant": "tenant::alpha",
            },
        )
        assert r_tenant.status_code == 403

        # 3) Delegated CredentialScope with '::' in X-HD2Lab-User MUST be rejected (403)
        r_delegated = await client.get(
            "/api/v1/agent/runs/run-nonexistent1234",
            headers={
                "X-Service-Token": "tok-delegated-rb01-001",
                "X-HD2Lab-Service": "hd2ai-beta",
                "X-HD2Lab-User": "tenant-beta::alice",
            },
        )
        assert r_delegated.status_code == 403

    # 4) Direct AnalysisServices._effective_owner MUST also reject '::' in owner or tenant_id
    with pytest.raises(ValueError):
        AnalysisServices._effective_owner("a::b", "tenant-alpha")
    with pytest.raises(ValueError):
        AnalysisServices._effective_owner("alice", "tenant::alpha")


# ---------------------------------------------------------------------------
# RB-02: Explicit Fail-Closed When Admin Scope Is Unspecified or Empty
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rb_02_admin_unspecified_or_empty_scope_fails_closed(tmp_path, monkeypatch):
    # 1) Unit check: Admin ExecutionScope with allowed_tenants=None or empty MUST fail closed
    adm_none = ExecutionScope(
        tenant_id="tenant-alpha",
        owner_id="ops",
        service_id="hd2ai-beta",
        role="admin",
        authenticated=True,
        auth_source="credential_scope:adm",
        allowed_tenants=None,
        allowed_services=None,
    )
    assert adm_none.can_access_tenant("tenant-alpha") is False
    assert adm_none.can_access_service("hd2ai-beta") is False

    adm_empty = ExecutionScope(
        tenant_id="tenant-alpha",
        owner_id="ops",
        service_id="hd2ai-beta",
        role="admiral",
        authenticated=True,
        auth_source="credential_scope:adm",
        allowed_tenants=frozenset(),
        allowed_services=frozenset(),
    )
    assert adm_empty.can_access_tenant("tenant-alpha") is False
    assert adm_empty.can_access_service("hd2ai-beta") is False

    # 2) Real API check: Admin CredentialScope with omitted/empty allowed_tenants MUST fail closed (403)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "adm-no-tenants": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["admin"],
                    "allowed_tenants": [],  # Empty admin tenant scope!
                    "owner_delegation": False,
                    "fixed_owner": "ops-empty",
                }
            }
        ),
    )
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SECRETS_JSON",
        json.dumps({"adm-no-tenants": ["tok-adm-empty-rb02-001"]}),
    )

    store = TrajectoryEventStore(tmp_path / "rb02.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())
    tlogger.log_decision(
        trajectory_id="traj-rb02",
        run_id="run-rb02-0001",
        decision_id="dec-rb02-1",
        step_index=1,
        decision_type="step_action",
        state_features={},
        allowed_actions=["CALL_MODEL"],
        constraints={},
        chosen_action="CALL_MODEL",
        proposed_parameters={},
        behavior_policy_version="agpolicy-r1-v1",
        config_digest="1111",
        action_probability=None,
        validator_result={"accepted": True},
        owner_id="alice",
        service_id="hd2ai-beta",
        tenant_id="default_tenant",
    )
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(
            "/api/v1/agent/runs/run-rb02-0001/trajectory",
            headers={
                "X-Service-Token": "tok-adm-empty-rb02-001",
                "X-HD2Lab-Service": "hd2ai-beta",
            },
        )
        assert r.status_code == 403
