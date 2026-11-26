"""R1.2 Stage A (P0) Fail-First Reproduction Tests: FIX-201 .. FIX-204."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.document import AGToolsHttpAdapter
from app.config import settings
from app.core.auth import ExecutionScope, is_agtools_token_valid
from app.core.clock import MockClock
from app.main import app
from app.runtime.policy.constraints import ConstraintSpec
from app.runtime.runner import runner
from app.schemas.agent import AgentRunRequest
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# FIX-201: Complete Auth Integration, Non-Admin Token Escalation Block, Key Rotation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_201_complete_auth_and_role_escalation_block(monkeypatch):
    import json

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "svc-user": {
                    "allowed_services": ["hd2ai-admin"],
                    "allowed_roles": ["service"],
                    "allowed_tenants": ["default_tenant"],
                    "owner_delegation": True,
                },
                "svc-admin": {
                    "allowed_services": ["hd2ai-admin"],
                    "allowed_roles": ["admiral"],
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
                "svc-user": ["curr-user-tok", "prev-user-tok"],
                "svc-admin": ["admin-only-tok"],
            }
        ),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) Unauthenticated access to protected endpoints MUST fail with 401
        r_run = await client.post("/api/v1/agent/runs", json={"message": "hi", "stream": False})
        assert r_run.status_code == 401

        r_ana = await client.post(
            "/api/v1/agent/analyses",
            json={"message": "analyze", "context_refs": ["doc_1"]},
        )
        assert r_ana.status_code == 401

        r_demo = await client.post(
            "/api/v1/demo/run",
            json={"mode": "chat", "model_mode": "direct", "base_url": "mock", "model": "m", "message": "hi"},
        )
        assert r_demo.status_code == 401

        # 2) Non-admin service token CANNOT escalate to admiral via X-HD2Lab-Role header!
        r_esc = await client.get(
            "/api/v1/config/tools/snapshot",
            headers={
                "X-Service-Token": "curr-user-tok",
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Service": "hd2ai-admin",
                "X-HD2Lab-Role": "admiral",  # forged role escalation!
            },
        )
        assert r_esc.status_code == 403

        # 3) Admin service token succeeds
        r_adm = await client.get(
            "/api/v1/config/tools/snapshot",
            headers={
                "X-Service-Token": "admin-only-tok",
                "X-HD2Lab-User": "ops",
                "X-HD2Lab-Service": "hd2ai-admin",
                "X-HD2Lab-Role": "admiral",
            },
        )
        assert r_adm.status_code == 200

        # 4) Previous rotated token is accepted while revoked token is rejected
        r_prev = await client.get(
            "/api/v1/agent/runs/run-nonexistent123",
            headers={
                "X-Service-Token": "prev-user-tok",
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Service": "hd2ai-admin",
            },
        )
        assert r_prev.status_code == 404  # authenticated, run just doesn't exist

        r_revoked = await client.get(
            "/api/v1/agent/runs/run-nonexistent123",
            headers={"X-Service-Token": "revoked-old-tok", "X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin"},
        )
        assert r_revoked.status_code == 401


# ---------------------------------------------------------------------------
# FIX-202: Verified ConstraintSpec & Fail-Closed Unauthenticated Runtime Entry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_202_verified_constraint_spec_and_unauthenticated_runtime_blocked():
    # 1) ConstraintSpec.from_runtime MUST default authenticated=False (fail-closed)
    c = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin",
        owner_id="alice",
        allowed_tools=["system.echo"],
        max_steps=4,
        max_tool_calls=2,
        time_budget_seconds=10.0,
        context_budget_tokens=2000,
    )
    assert c.authenticated is False

    # 2) AgentRunner.create_run with unauthenticated ExecutionScope MUST fail closed
    unauth_scope = ExecutionScope(
        tenant_id="unauthenticated",
        owner_id="",
        service_id="",
        role="guest",
        authenticated=False,
        auth_source="unauthenticated",
    )
    with pytest.raises(PermissionError):
        await runner.create_run(AgentRunRequest(message="hi"), caller_scope=unauth_scope)


# ---------------------------------------------------------------------------
# FIX-203: AGTools Credential Validation & Production Boot Fail-Fast
# ---------------------------------------------------------------------------
def test_fix_203_agtools_token_placeholders_and_boot_fail_fast(monkeypatch):
    from app.config import validate_boot_configuration

    # 1) Empty, whitespace, placeholder, and old default tokens rejected
    for bad in ("", "   ", "changeme", "placeholder", "REPLACE_ME", "your-token-here", "hd2-doc-secret-token-key-2026"):
        assert is_agtools_token_valid(bad) is False

    # 2) AGToolsHttpAdapter refuses outbound call without valid token
    adapter = AGToolsHttpAdapter(base_url="http://127.0.0.1:8006/api/v1", token="   ")
    with pytest.raises(RuntimeError, match="agtools_token"):
        adapter._headers()

    # 3) Production boot fail-fast catches missing env vars without leaking secret values
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "AGTOOLS_TOKEN", "")
    monkeypatch.setattr(settings, "SERVICE_TOKENS", "")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_SECRET", "")
    with pytest.raises(RuntimeError) as exc_info:
        validate_boot_configuration()
    msg = str(exc_info.value)
    assert "AGCORE_AGTOOLS_TOKEN" in msg


# ---------------------------------------------------------------------------
# FIX-204: Unowned Legacy Trajectory Isolation (Admin-only, No First-Submitter Adoption)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fix_204_unowned_trajectory_isolated_from_regular_users(tmp_path, monkeypatch):
    store = TrajectoryEventStore(tmp_path / "legacy_unowned.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())
    # Record legacy event with owner_id=None, service_id=None
    tlogger.log_decision(
        trajectory_id="traj-legacy-unowned",
        run_id="run-unowned001",
        decision_id="dec-unowned-1",
        step_index=1,
        decision_type="step_action",
        state_features={},
        allowed_actions=["CALL_MODEL"],
        constraints={},
        chosen_action="CALL_MODEL",
        proposed_parameters={},
        behavior_policy_version="agpolicy-r1-v1",
        config_digest="0000",
        action_probability=None,
        validator_result={"accepted": True},
        owner_id=None,
        service_id=None,
    )
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Regular user MUST be denied reading unowned trajectory
        r_read = await client.get(
            "/api/v1/agent/runs/run-unowned001/trajectory",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin", "X-HD2Lab-Role": "user"},
        )
        assert r_read.status_code == 403

        # Regular user MUST be denied submitting feedback (never adopt first submitter as owner!)
        r_fb = await client.post(
            "/api/v1/agent/runs/run-unowned001/feedback",
            headers={"X-HD2Lab-User": "alice", "X-HD2Lab-Service": "hd2ai-admin", "X-HD2Lab-Role": "user"},
            json={"feedback_type": "thumbs_up"},
        )
        assert r_fb.status_code == 403

        # Regular admin without explicit wildcard scope MUST ALSO be denied (ADD-01: no implicit wildcard)
        r_reg_adm = await client.get(
            "/api/v1/agent/runs/run-unowned001/trajectory",
            headers={"X-HD2Lab-User": "ops", "X-HD2Lab-Service": "hd2ai-admin", "X-HD2Lab-Role": "admiral"},
        )
        assert r_reg_adm.status_code == 403

        # Explicit server-configured wildcard admin CAN inspect unowned legacy record (ADD-01)
        import json as _json

        monkeypatch.setattr(
            settings,
            "CREDENTIAL_SCOPES_JSON",
            _json.dumps(
                {
                    "global-admin": {
                        "allowed_services": ["hd2ai-admin", "*"],
                        "allowed_roles": ["admiral"],
                        "allowed_tenants": ["*"],
                        "owner_delegation": False,
                        "fixed_owner": "ops",
                    }
                }
            ),
        )
        monkeypatch.setattr(
            settings,
            "CREDENTIAL_SECRETS_JSON",
            _json.dumps({"global-admin": ["global-admin-secret-204"]}),
        )
        r_adm = await client.get(
            "/api/v1/agent/runs/run-unowned001/trajectory",
            headers={"X-Service-Token": "global-admin-secret-204", "X-HD2Lab-Service": "hd2ai-admin"},
        )
        assert r_adm.status_code == 200
