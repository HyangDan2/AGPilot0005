"""Final Release Verification Mandatory Addendum Fail-First Tests: ADD-01 & ADD-02."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.document import AGToolsHttpAdapter
from app.config import settings
from app.core.auth import current_execution_scope
from app.core.clock import MockClock
from app.main import app
from app.runtime.document_analysis import AnalysisStore
from app.runtime.tasks.bootstrap import AnalysisServices
from app.runtime.tasks.orchestrator import TaskOrchestrator
from app.runtime.tasks.ports import JsonTaskRepository
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# ADD-01: Wrong Credential NEVER Falls Back to Trusted Proxy; Admin Scope Strict
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_add_01_wrong_credential_never_falls_back_to_trusted_proxy(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_SECRET", "proxy-sec-add01-9999")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "127.0.0.1,::1,testclient")
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "cred-alpha": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["user"],
                    "allowed_tenants": ["tenant-alpha"],
                    "owner_delegation": False,
                    "fixed_owner": "alice",
                }
            }
        ),
    )
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SECRETS_JSON",
        json.dumps({"cred-alpha": ["valid-cred-secret-0001"]}),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Valid Trusted Proxy headers + INVALID X-Service-Token MUST return 401 (NEVER fall back to Trusted Proxy!)
        r = await client.get(
            "/api/v1/agent/runs/run-nonexistent1234",
            headers={
                "X-Service-Token": "wrong-invalid-token-9999",
                "X-Trusted-Proxy-Secret": "proxy-sec-add01-9999",
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Service": "hd2ai-beta",
            },
        )
        assert r.status_code == 401

        # Without X-Service-Token, valid Trusted Proxy succeeds (404 = authenticated, run not found)
        r_proxy = await client.get(
            "/api/v1/agent/runs/run-nonexistent1234",
            headers={
                "X-Trusted-Proxy-Secret": "proxy-sec-add01-9999",
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Service": "hd2ai-beta",
            },
        )
        assert r_proxy.status_code == 404


@pytest.mark.asyncio
async def test_add_01_admin_scope_no_header_expansion_and_no_implicit_wildcard(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_SECRET", "proxy-sec-add01-9999")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "127.0.0.1,::1,testclient")
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "admin-beta-alpha": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["admin"],
                    "allowed_tenants": ["tenant-alpha"],
                    "owner_delegation": False,
                    "fixed_owner": "ops-beta",
                },
                "admin-global": {
                    "allowed_services": ["hd2ai-admin", "*"],
                    "allowed_roles": ["admiral"],
                    "allowed_tenants": ["*"],
                    "owner_delegation": False,
                    "fixed_owner": "ops-global",
                },
            }
        ),
    )
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SECRETS_JSON",
        json.dumps(
            {
                "admin-beta-alpha": ["tok-admin-beta-alpha-001"],
                "admin-global": ["tok-admin-global-002"],
            }
        ),
    )

    store = TrajectoryEventStore(tmp_path / "add01_admin.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)

    for rid, svc, tenant in [
        ("run-a01-beta-alpha", "hd2ai-beta", "tenant-alpha"),
        ("run-a01-adm-alpha", "hd2ai-admin", "tenant-alpha"),
        ("run-a01-beta-beta", "hd2ai-beta", "tenant-beta"),
        ("run-a01-legacy-nil", None, None),
    ]:
        tlogger.log_decision(
            trajectory_id=f"traj-{rid}",
            run_id=rid,
            decision_id=f"dec-{rid}",
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
            owner_id="alice" if svc else None,
            service_id=svc,
            tenant_id=tenant,
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) Scoped admin trying to expand scope via forged request headers MUST be ignored!
        forged_hdrs = {
            "X-Service-Token": "tok-admin-beta-alpha-001",
            "X-HD2Lab-Service": "hd2ai-beta",
            "X-HD2Lab-Admin-Tenants": "*",
            "X-HD2Lab-Admin-Services": "*",
        }
        # Own tenant + own service -> 200
        assert (await client.get("/api/v1/agent/runs/run-a01-beta-alpha/trajectory", headers=forged_hdrs)).status_code == 200
        # Same tenant, DIFFERENT service -> 403!
        assert (await client.get("/api/v1/agent/runs/run-a01-adm-alpha/trajectory", headers=forged_hdrs)).status_code == 403
        # DIFFERENT tenant -> 403!
        assert (await client.get("/api/v1/agent/runs/run-a01-beta-beta/trajectory", headers=forged_hdrs)).status_code == 403
        # Legacy NULL tenant/service -> 403 (no implicit wildcard)!
        assert (await client.get("/api/v1/agent/runs/run-a01-legacy-nil/trajectory", headers=forged_hdrs)).status_code == 403

        # 2) Regular Trusted Proxy admin (no wildcard headers) has NO implicit wildcard
        proxy_adm_hdrs = {
            "X-Trusted-Proxy-Secret": "proxy-sec-add01-9999",
            "X-HD2Lab-User": "ops",
            "X-HD2Lab-Service": "hd2ai-beta",
            "X-HD2Lab-Tenant": "tenant-alpha",
            "X-HD2Lab-Role": "admin",
        }
        assert (await client.get("/api/v1/agent/runs/run-a01-beta-alpha/trajectory", headers=proxy_adm_hdrs)).status_code == 200
        assert (await client.get("/api/v1/agent/runs/run-a01-adm-alpha/trajectory", headers=proxy_adm_hdrs)).status_code == 403
        assert (await client.get("/api/v1/agent/runs/run-a01-legacy-nil/trajectory", headers=proxy_adm_hdrs)).status_code == 403

        # 3) Explicit server-configured wildcard admin CAN inspect legacy record
        global_hdrs = {
            "X-Service-Token": "tok-admin-global-002",
            "X-HD2Lab-Service": "hd2ai-admin",
        }
        assert (await client.get("/api/v1/agent/runs/run-a01-legacy-nil/trajectory", headers=global_hdrs)).status_code == 200


# ---------------------------------------------------------------------------
# ADD-02: Real AnalysisServices -> TaskOrchestrator -> ScopedDocuments -> AGToolsHttpAdapter
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_add_02_durable_analysis_real_orchestrator_create_resume_and_no_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGTOOLS_ENABLED", True)
    monkeypatch.setattr(settings, "AGTOOLS_TOKEN", "valid-agtools-token-add02")
    monkeypatch.setattr(settings, "AGTOOLS_TENANT", "default_tenant")

    captured_headers: List[Dict[str, str]] = []

    def _mock_transport_handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(
            {
                "path": request.url.path,
                "tenant": request.headers.get("X-Tenant-Id", ""),
                "owner": request.headers.get("X-Owner-Id", ""),
            }
        )
        if request.url.path.endswith("/outline"):
            return httpx.Response(
                200,
                json={"doc_id": "doc_1", "document_name": "Spec", "status": "completed", "sections": []},
            )
        return httpx.Response(
            200,
            json={"snapshot_id": "snap1", "total_chunks": 0, "chunks": [], "next_cursor": None},
        )

    transport = httpx.MockTransport(_mock_transport_handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_async_client(*args, transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"}),
    )

    http_adapter = AGToolsHttpAdapter(base_url="http://127.0.0.1:8006/api/v1", token="valid-agtools-token-add02")
    store = AnalysisStore(tmp_path / "analyses")
    repo = JsonTaskRepository(store, time.time)

    from app.runtime.tasks.domain import Plan

    class _DummyPlanner:
        async def plan(self, task, metadata):
            task.plan = Plan(
                intent="summary",
                scope="all",
                objective="summarize",
                required_outputs=["summary"],
                completion_criteria=["done"],
            )

    class _DummyNoop:
        pass

    orchestrator = TaskOrchestrator(repo, http_adapter, _DummyPlanner(), _DummyNoop(), _DummyNoop(), time.time)
    svc = AnalysisServices(legacy=_DummyNoop(), orchestrator=orchestrator, store=store)

    # 1) Create analysis for alice@tenant-alpha (NO ContextVar pre-set; must come from persisted task!)
    assert current_execution_scope.get() is None
    created = await svc.create("alice", "hd2ai-beta", "summarize", ["doc_1"], tenant_id="tenant-alpha")
    job_id = created["job_id"]
    worker = orchestrator.tasks[job_id]
    try:
        await asyncio.wait_for(worker, timeout=3.0)
    except asyncio.CancelledError:
        pass

    assert len(captured_headers) >= 1
    assert captured_headers[0]["tenant"] == "tenant-alpha"
    assert captured_headers[0]["owner"] == "alice"

    # 2) Same owner_id="alice" in DIFFERENT tenant ("tenant-beta") MUST NOT see or resume the job
    assert svc.get(job_id, "alice", "hd2ai-beta", tenant_id="tenant-beta") is None
    assert svc.resume(job_id, "alice", "hd2ai-beta", tenant_id="tenant-beta") is None

    # 3) Resume after pause MUST preserve verified tenant_id & owner_id on outbound AGTools HTTP calls
    task = repo.load(job_id)
    assert task is not None
    task.status = "paused"
    repo.save(task)
    captured_headers.clear()
    assert current_execution_scope.get() is None

    resumed = svc.resume(job_id, "alice", "hd2ai-beta", tenant_id="tenant-alpha")
    assert resumed is not None
    worker2 = orchestrator.tasks[job_id]
    try:
        await asyncio.wait_for(worker2, timeout=3.0)
    except asyncio.CancelledError:
        pass

    assert len(captured_headers) >= 1
    assert captured_headers[0]["tenant"] == "tenant-alpha"
    assert captured_headers[0]["owner"] == "alice"

    # 4) Existing legacy tenant-less task on disk (owner_id="alice", no tenant prefix) MUST NOT be auto-adopted or fallback-read
    legacy_job_id = "ana_" + "b" * 32
    store.write(
        {
            "schema_version": 2,
            "job_id": legacy_job_id,
            "owner_id": "alice",
            "service_id": "hd2ai-beta",
            "question": "legacy",
            "document_ids": ["doc_1"],
            "status": "completed",
            "created_at": time.time(),
            "updated_at": time.time(),
            "budget": {"deadline": time.time() + 100, "max_model_calls": 5, "max_document_calls": 5},
        }
    )
    assert svc.get(legacy_job_id, "alice", "hd2ai-beta", tenant_id="tenant-alpha") is None
    assert svc.resume(legacy_job_id, "alice", "hd2ai-beta", tenant_id="tenant-alpha") is None
    assert svc.cancel(legacy_job_id, "alice", "hd2ai-beta", tenant_id="tenant-alpha") is None
