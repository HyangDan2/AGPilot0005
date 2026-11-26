"""RC Final Defect Closure Fail-First Tests: HF-01 .. HF-07."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any, Dict, List

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.document import AGToolsHttpAdapter, document_adapter, tool_token_budget
from app.config import settings
from app.core.auth import ExecutionScope, current_execution_scope
from app.core.clock import MockClock
from app.main import app
from app.runtime.event_bus import EventReplayExpiredError, RunEventBus
from app.runtime.graph import tool_node
from app.runtime.runner import AgentRunner, DocumentReferenceError, runner
from app.runtime.state import AgentState
from app.schemas.agent import AgentRunRequest
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.tools.policy import tool_policy
from app.tools.registry import registry
from app.tracing.async_writer import AsyncTrajectoryWriter
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


# ---------------------------------------------------------------------------
# HF-01: Legacy Token Bypass Removed, Trusted Proxy Preserved, Unregistered Service Blocked
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hf_01_legacy_token_bypass_removed_trusted_proxy_works_and_unregistered_service_blocked(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SERVICE_TOKENS", "legacy-svc-secret-token-1234")
    monkeypatch.setattr(settings, "ADMIN_SERVICE_TOKENS", "legacy-adm-secret-token-1234")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_SECRET", "proxy-secret-hf01-9999")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "127.0.0.1,::1,testclient")
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "scoped-beta": {
                    "allowed_services": ["hd2ai-beta", "unregistered-rogue-svc"],
                    "allowed_roles": ["service"],
                    "allowed_tenants": ["tenant-alpha"],
                    "owner_delegation": False,
                    "fixed_owner": "beta-bot",
                }
            }
        ),
    )
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SECRETS_JSON",
        json.dumps({"scoped-beta": ["scoped-beta-secret-0001"]}),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1) Legacy SERVICE_TOKENS MUST be rejected with 401
        r_legacy_svc = await client.get(
            "/api/v1/agent/runs/run-nonexistent1234",
            headers={
                "X-Service-Token": "legacy-svc-secret-token-1234",
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Service": "hd2ai-beta",
            },
        )
        assert r_legacy_svc.status_code == 401

        # 2) Legacy ADMIN_SERVICE_TOKENS MUST be rejected with 401
        r_legacy_adm = await client.get(
            "/api/v1/config/tools/snapshot",
            headers={
                "X-Service-Token": "legacy-adm-secret-token-1234",
                "X-HD2Lab-User": "ops",
                "X-HD2Lab-Service": "hd2ai-admin",
                "X-HD2Lab-Role": "admiral",
            },
        )
        assert r_legacy_adm.status_code == 401

        # 3) Verified Trusted Proxy MUST pass authentication (404 = run not found, NOT 401!)
        r_proxy = await client.get(
            "/api/v1/agent/runs/run-nonexistent1234",
            headers={
                "X-Trusted-Proxy-Secret": "proxy-secret-hf01-9999",
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Service": "hd2ai-beta",
            },
        )
        assert r_proxy.status_code == 404

        # 4) Unregistered service MUST be rejected at API entry with 403
        r_unreg_proxy = await client.get(
            "/api/v1/agent/runs/run-nonexistent1234",
            headers={
                "X-Trusted-Proxy-Secret": "proxy-secret-hf01-9999",
                "X-HD2Lab-User": "alice",
                "X-HD2Lab-Service": "unregistered-rogue-svc",
            },
        )
        assert r_unreg_proxy.status_code == 403

        r_unreg_cred = await client.get(
            "/api/v1/agent/runs/run-nonexistent1234",
            headers={
                "X-Service-Token": "scoped-beta-secret-0001",
                "X-HD2Lab-Service": "unregistered-rogue-svc",
            },
        )
        assert r_unreg_cred.status_code == 403

    # 5) Unregistered service MUST NOT fall back to public-safe tool list
    assert tool_policy.allowed_tools_for_service("unregistered-rogue-svc") == []


# ---------------------------------------------------------------------------
# HF-02: ExecutionScope Full Path (Document Adapter, Pre-validation, Analysis, Trajectory, Feedback, Admin Scope)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hf_02_tenant_scope_propagation_and_same_owner_cross_tenant_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "AGTOOLS_ENABLED", True)
    monkeypatch.setattr(settings, "AGTOOLS_TOKEN", "valid-agtools-secret-token-hf02")
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
                },
                "cred-beta": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["user"],
                    "allowed_tenants": ["tenant-beta"],
                    "owner_delegation": False,
                    "fixed_owner": "alice",  # SAME owner_id, DIFFERENT tenant!
                },
                "admin-alpha": {
                    "allowed_services": ["hd2ai-admin", "hd2ai-beta"],
                    "allowed_roles": ["admin"],
                    "allowed_tenants": ["tenant-alpha"],  # Explicit admin tenant scope!
                    "owner_delegation": False,
                    "fixed_owner": "ops-alpha",
                },
            }
        ),
    )
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SECRETS_JSON",
        json.dumps(
            {
                "cred-alpha": ["tok-alpha-secret-0001"],
                "cred-beta": ["tok-beta-secret-0002"],
                "admin-alpha": ["tok-admin-alpha-0003"],
            }
        ),
    )

    # 1) Document Adapter _headers() MUST propagate verified ExecutionScope tenant_id & owner_id
    adapter = AGToolsHttpAdapter(base_url="http://127.0.0.1:8006/api/v1", token="valid-agtools-secret-token-hf02")
    scope_alpha = ExecutionScope(
        tenant_id="tenant-alpha",
        owner_id="alice",
        service_id="hd2ai-beta",
        role="user",
        authenticated=True,
        auth_source="credential_scope:cred-alpha",
    )
    tok = current_execution_scope.set(scope_alpha)
    try:
        hdrs = adapter._headers()
        assert hdrs["X-Tenant-Id"] == "tenant-alpha"
        assert hdrs["X-Owner-Id"] == "alice"
    finally:
        current_execution_scope.reset(tok)

    # 2) Document Reference pre-validation in runner.create_run MUST propagate ExecutionScope
    observed_preval_tenants: List[str] = []

    async def _fake_outline(doc_id: str) -> Dict[str, Any]:
        sc = current_execution_scope.get()
        observed_preval_tenants.append(sc.tenant_id if sc else "NONE")
        return {"doc_id": doc_id, "document_name": "Doc1", "status": "completed", "outline": []}

    monkeypatch.setattr(document_adapter, "get_outline", _fake_outline)
    local_runner = AgentRunner()
    await local_runner.create_run(
        AgentRunRequest(message="check doc", context_refs=["doc_1"], stream=False),
        caller_scope=scope_alpha,
    )
    assert observed_preval_tenants == ["tenant-alpha"]

    # 3) Trajectory & Feedback isolation for SAME owner_id across different tenants + Admin scope
    store = TrajectoryEventStore(tmp_path / "hf02_traj.db")
    tlogger = TrajectoryLogger(store, clock=MockClock())
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)

    for rid, tid, tenant in [
        ("run-alpha0001", "traj-run-alpha0001", "tenant-alpha"),
        ("run-beta00001", "traj-run-beta00001", "tenant-beta"),
        ("run-legacy001", "traj-run-legacy001", None),  # Legacy row without tenant_id
    ]:
        tlogger.log_decision(
            trajectory_id=tid,
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
            owner_id="alice",
            service_id="hd2ai-beta",
            tenant_id=tenant,
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # alice@tenant-alpha CAN read own trajectory
        r_own = await client.get(
            "/api/v1/agent/runs/run-alpha0001/trajectory",
            headers={"X-Service-Token": "tok-alpha-secret-0001", "X-HD2Lab-Service": "hd2ai-beta"},
        )
        assert r_own.status_code == 200

        # alice@tenant-beta (SAME owner_id="alice") CANNOT read alice@tenant-alpha trajectory or submit feedback
        r_cross_read = await client.get(
            "/api/v1/agent/runs/run-alpha0001/trajectory",
            headers={"X-Service-Token": "tok-beta-secret-0002", "X-HD2Lab-Service": "hd2ai-beta"},
        )
        assert r_cross_read.status_code == 403

        r_cross_fb = await client.post(
            "/api/v1/agent/runs/run-alpha0001/feedback",
            headers={"X-Service-Token": "tok-beta-secret-0002", "X-HD2Lab-Service": "hd2ai-beta"},
            json={"feedback_type": "thumbs_up"},
        )
        assert r_cross_fb.status_code == 403

        # Existing legacy data with tenant_id=None MUST NOT be guessed as tenant-alpha
        r_legacy_user = await client.get(
            "/api/v1/agent/runs/run-legacy001/trajectory",
            headers={"X-Service-Token": "tok-alpha-secret-0001", "X-HD2Lab-Service": "hd2ai-beta"},
        )
        assert r_legacy_user.status_code == 403

        # Admin scoped to tenant-alpha CAN read tenant-alpha, CANNOT read tenant-beta or tenant=None
        r_adm_ok = await client.get(
            "/api/v1/agent/runs/run-alpha0001/trajectory",
            headers={"X-Service-Token": "tok-admin-alpha-0003", "X-HD2Lab-Service": "hd2ai-beta"},
        )
        assert r_adm_ok.status_code == 200

        r_adm_cross = await client.get(
            "/api/v1/agent/runs/run-beta00001/trajectory",
            headers={"X-Service-Token": "tok-admin-alpha-0003", "X-HD2Lab-Service": "hd2ai-beta"},
        )
        assert r_adm_cross.status_code == 403

        r_adm_legacy = await client.get(
            "/api/v1/agent/runs/run-legacy001/trajectory",
            headers={"X-Service-Token": "tok-admin-alpha-0003", "X-HD2Lab-Service": "hd2ai-beta"},
        )
        assert r_adm_legacy.status_code == 403

        # 4) Document Analysis cross-tenant isolation for SAME owner_id="alice"
        class _FakeAnalysisSvc:
            def __init__(self):
                self.jobs: Dict[str, Dict[str, Any]] = {}

            async def create(self, owner, service, message, refs, tenant_id=None, **kwargs):
                sc = current_execution_scope.get()
                assert sc is not None and sc.tenant_id == "tenant-alpha"
                jid = "ana_" + "a" * 32
                self.jobs[jid] = {"job_id": jid, "owner": owner, "service": service, "tenant_id": tenant_id, "status": "queued"}
                return self.jobs[jid]

            def get(self, job_id, owner, service, tenant_id=None):
                j = self.jobs.get(job_id)
                if not j or j["owner"] != owner or j["service"] != service or j["tenant_id"] != tenant_id:
                    return None
                return j

        fake_svc = _FakeAnalysisSvc()
        monkeypatch.setattr("app.api.document_analysis.services", lambda _app: fake_svc)

        r_create = await client.post(
            "/api/v1/agent/analyses",
            headers={"X-Service-Token": "tok-alpha-secret-0001", "X-HD2Lab-Service": "hd2ai-beta"},
            json={"message": "analyze", "context_refs": ["doc_1"]},
        )
        assert r_create.status_code == 202
        jid = r_create.json()["job_id"]

        r_same = await client.get(
            f"/api/v1/agent/analyses/{jid}",
            headers={"X-Service-Token": "tok-alpha-secret-0001", "X-HD2Lab-Service": "hd2ai-beta"},
        )
        assert r_same.status_code == 200

        r_other_tenant = await client.get(
            f"/api/v1/agent/analyses/{jid}",
            headers={"X-Service-Token": "tok-beta-secret-0002", "X-HD2Lab-Service": "hd2ai-beta"},
        )
        assert r_other_tenant.status_code == 404


# ---------------------------------------------------------------------------
# HF-03: Document Tool Own Budget Respects Approved Context Budget
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hf_03_document_tool_budget_respects_approved_context_budget(monkeypatch):
    monkeypatch.setattr(settings, "AGTOOLS_ENABLED", True)
    observed_budgets: List[int] = []

    async def _fake_read_section(doc_id: str, section_id: str) -> Dict[str, Any]:
        observed_budgets.append(tool_token_budget.get() or -1)
        return {"doc_id": doc_id, "section_id": section_id, "content": "ok"}

    monkeypatch.setattr(document_adapter, "read_section", _fake_read_section)

    call = ToolCall(
        id="tc-hf03",
        function=FunctionCall(
            name="document.read_section",
            arguments=json.dumps({"doc_id": "doc_1", "section_id": "sec_1"}),
        ),
    )
    state = AgentState(
        run_id="run-hf03",
        service_id="hd2ai-beta",
        owner_id="alice",
        tenant_id="tenant-alpha",
        authenticated=True,
        context_refs=["doc_1"],
        messages=[GatewayMessage(role="user", content="read section 1")],
        pending_tool_calls=[call],
    )
    await tool_node(state, approved_context_budget=400)
    assert len(observed_budgets) == 1
    assert 0 < observed_budgets[0] <= 400


# ---------------------------------------------------------------------------
# HF-04: SQLite Partial Index Real WHERE Predicate & Runtime Dedup Verification
# ---------------------------------------------------------------------------
def test_hf_04_sqlite_partial_index_where_clause_and_runtime_dedup_verification(tmp_path):
    db_path = tmp_path / "hf04_tampered_where.db"
    TrajectoryEventStore(db_path)

    # Replace index with a tampered WHERE clause that still contains both substrings!
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP INDEX uq_trajectory_feedback_dedup;")
    conn.execute(
        """
        CREATE UNIQUE INDEX uq_trajectory_feedback_dedup
            ON trajectory_events(feedback_dedup_key)
            WHERE event_type = 'FeedbackEvent' AND feedback_dedup_key IS NOT NULL AND 1 = 0;
        """
    )
    conn.commit()
    conn.close()

    reopened = TrajectoryEventStore(db_path)
    assert reopened.feedback_dedup_integrity_ok is False


# ---------------------------------------------------------------------------
# HF-05: Atomic SSE Cursor Validation & Subscriber Registration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hf_05_sse_cursor_validation_and_subscribe_atomic():
    bus = RunEventBus(run_id="run-hf05", max_buffer_size=3, subscriber_queue_size=16)
    e1 = bus.publish("run.started", "running", {})
    bus.publish("answer.delta", "running", {"delta": "a"})

    # open_stream MUST validate cursor and register subscriber synchronously BEFORE returning generator
    gen = bus.open_stream(last_event_id=e1.event_id)
    assert len(bus._subscribers) == 1

    # Now publish enough events to evict e1 from _buffer BEFORE consumer starts iterating!
    bus.publish("answer.delta", "running", {"delta": "b"})
    bus.publish("answer.delta", "running", {"delta": "c"})
    bus.publish_terminal("run.completed", "completed", {"answer": "abc"})

    frames = [f async for f in gen]
    assert any("answer.delta" in f and '"a"' in f for f in frames)
    assert any("run.completed" in f for f in frames)
    assert len(bus._subscribers) == 0

    # Expired cursor MUST raise synchronously before StreamingResponse starts and leak 0 subscribers
    with pytest.raises(EventReplayExpiredError):
        bus.open_stream(last_event_id=e1.event_id)
    assert len(bus._subscribers) == 0


# ---------------------------------------------------------------------------
# HF-06: Atomic Active Run Admission Control (No await inside Lock, Rollback on Failure)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hf_06_atomic_active_run_admission_control_and_rollback(monkeypatch):
    local_runner = AgentRunner(max_active_runs=2)
    scope = ExecutionScope(
        tenant_id="tenant-alpha",
        owner_id="alice",
        service_id="hd2ai-beta",
        role="user",
        authenticated=True,
        auth_source="credential_scope:cred-alpha",
    )

    in_flight = 0
    max_concurrent_outline = 0

    async def _slow_outline(doc_id: str) -> Dict[str, Any]:
        nonlocal in_flight, max_concurrent_outline
        in_flight += 1
        max_concurrent_outline = max(max_concurrent_outline, in_flight)
        try:
            await asyncio.sleep(0.05)
            if doc_id == "bad_doc":
                return {"error": True, "status": "failed"}
            return {"doc_id": doc_id, "document_name": "Doc", "status": "completed", "outline": []}
        finally:
            in_flight -= 1

    monkeypatch.setattr(document_adapter, "get_outline", _slow_outline)

    # Fire 6 concurrent create_run calls against max_active_runs=2
    results = await asyncio.gather(
        *[
            local_runner.create_run(
                AgentRunRequest(message=f"m{i}", context_refs=["good_doc"], stream=False),
                caller_scope=scope,
            )
            for i in range(6)
        ],
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, RuntimeError) and str(r) == "active_run_limit_exceeded"]
    assert len(ok) == 2
    assert len(errs) == 4
    # Because await get_outline runs OUTSIDE the lock, the 2 admitted runs execute get_outline concurrently!
    assert max_concurrent_outline == 2

    # Verify rollback on pre-validation failure frees the reserved slot immediately
    runner2 = AgentRunner(max_active_runs=1)
    with pytest.raises(DocumentReferenceError):
        await runner2.create_run(
            AgentRunRequest(message="fail", context_refs=["bad_doc"], stream=False),
            caller_scope=scope,
        )
    sess = await runner2.create_run(
        AgentRunRequest(message="recover", context_refs=["good_doc"], stream=False),
        caller_scope=scope,
    )
    assert sess.state.status == "queued"


# ---------------------------------------------------------------------------
# HF-07: Single Monotonic Deadline Across Flush and Shutdown
# ---------------------------------------------------------------------------
def test_hf_07_flush_and_shutdown_single_monotonic_deadline(tmp_path):
    store = TrajectoryEventStore(tmp_path / "hf07.db")

    orig_append = store.append

    def _slow_append(*args, **kwargs):
        time.sleep(0.4)
        return orig_append(*args, **kwargs)

    store.append = _slow_append  # type: ignore[method-assign]
    writer = AsyncTrajectoryWriter(store, queue_capacity=1)
    tlogger = TrajectoryLogger(store, clock=MockClock(), async_writer=writer)

    # Enqueue 2 events so worker is busy sleeping 0.4s and queue is full (capacity=1)
    for i in range(2):
        tlogger.log_decision(
            trajectory_id="traj-hf07",
            run_id="run-hf07",
            decision_id=f"dec-{i}",
            step_index=i + 1,
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
        )

    t0 = time.monotonic()
    res = writer.flush(timeout_seconds=0.12)
    flush_elapsed = time.monotonic() - t0
    assert res.completed is False
    # Must NOT double-wait (2 * 0.12 = 0.24s)
    assert flush_elapsed < 0.20

    t1 = time.monotonic()
    summary = writer.shutdown(timeout_seconds=0.12)
    shutdown_elapsed = time.monotonic() - t1
    assert summary["clean"] is False
    # Single monotonic deadline across flush + signal + join
    assert shutdown_elapsed < 0.20
