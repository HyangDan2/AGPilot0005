"""WO-R3-PREDEV-FIX-01 — Runtime integration & safety hardening verification.

One test class per fix item, each asserting the *observable* contract (HTTP status/error
code, run status, trajectory, adapter calls, process/transport state) rather than internal
plumbing:

    FIX-01  execution scope (body cannot hold authority, binding verified, resume re-checked)
    FIX-02  idempotency & concurrency (durable claim, duplicate side effect, recovery hold,
            cancel/completion race)
    FIX-03  real MCP transport (local stdio server, local streamable-HTTP server)
    FIX-04  Omni runtime integration (workflow entered from /agent/runs, shared hooks)
    FIX-05  LLM planner (structured DAG, fallback, zero calls on the direct path)
    FIX-06  approval security (bound token, authenticated approver, one-time use)
    FIX-07  MCP/tool isolation (admin-only, tenant scope, egress policy, policy authority)
    FIX-08  R2.1 adapter contract (scope propagation, explicit ``unavailable``)

Run:  /workspace/.venv-r2canvas/bin/python -m pytest tests/test_r3_fix01_hardening.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.agent import router as agent_router
from app.api.workflow import WorkflowService, router as workflow_router, set_workflow_service
from app.core.auth import ExecutionScope
from app.workflow import (
    ApprovalBinding,
    ApprovalError,
    ApprovalToken,
    ExecutionContext,
    ExecutionLedger,
    ExecutionScopeAuthority,
    HookDefinition,
    HookEvent,
    HookRegistry,
    IdempotencyLedger,
    LLMPlannerAdapter,
    LLMPlannerError,
    MCPApprovalRequired,
    MCPClientManager,
    MCPConfigError,
    MCPServerConfig,
    MCPToolDenied,
    MCPUnavailable,
    MockOfficeAdapter,
    PlanStep,
    Planner,
    PlannerRequest,
    R2WorkspaceResolver,
    RunBusy,
    RunExecutionGuard,
    RuntimeOfficeAdapter,
    ScopeEvidence,
    ScopeViolation,
    SkillsEngine,
    StdioMCPClient,
    StreamableHTTPMCPClient,
    ToolAccessPolicy,
    UnifiedToolRegistry,
    UnifiedToolSpec,
    WorkflowCheckpointStore,
    WorkflowRunRequest,
    WorkflowRuntime,
    WorkflowRuntimeError,
    build_workflow_stack,
    get_office_adapter,
    unavailable_client_factory,
)
from app.workflow.checkpoint import default_checkpoint_path
from app.workflow.isolation import (
    MCPSecurityError,
    mask_endpoint,
    require_mcp_admin,
    validate_command,
    validate_endpoint,
)
from app.workflow.ledger import SIDE_EFFECT_STEPS, default_ledger_path
from app.workflow.mcp import TOOL_POLICY_VALUES
from app.workflow.office_adapter import (
    AGTOOLS_PROVIDED_CAPABILITIES,
    OFFICE_CAPABILITIES,
    AdapterCallScope,
    OfficeAdapterError,
    OfficeAdapterUnavailable,
)
from app.workflow.omni import OmniRuntimeBridge
from app.workflow.planner import DOCUMENT_PIPELINE

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


# ======================================================================================
# Helpers
# ======================================================================================
def ctx(**overrides: Any) -> ExecutionContext:
    values = {
        "tenant_id": "tenant-a",
        "owner_id": "user-1",
        "service_id": "agcore",
        "session_id": "sess-1",
        "conversation_id": "conv-1",
        "workspace_id": "ws-1",
    }
    values.update(overrides)
    return ExecutionContext(**values)


DOCUMENTS: Dict[str, str] = {
    "policies/leave.md": "휴가 정책: 연차는 15일이며 이월은 5일까지 가능하다.",
    "tables/q4.json": json.dumps([{"team": "alpha", "reviews": 12}]),
    "docs/base.md": "alpha policy\nbeta process",
    "docs/target.md": "alpha policy\ngamma process",
}


def office(**overrides: Any) -> MockOfficeAdapter:
    files = dict(DOCUMENTS)
    files.update(overrides.pop("documents", {}))
    return MockOfficeAdapter(documents=files, **overrides)


def runtime_for(office_adapter: Optional[MockOfficeAdapter] = None, **kwargs: Any) -> WorkflowRuntime:
    adapter = office_adapter or office()
    stack = build_workflow_stack(office_adapter=adapter)
    options: Dict[str, Any] = {
        "registry": stack.registry,
        "skills": SkillsEngine(),
        "hooks": HookRegistry(),
        "mcp": stack.mcp,
        "office": adapter,
        "approvals": stack.safety["approvals"],
        "ledger": stack.safety["idempotency"],
        "execution_ledger": stack.safety["execution_ledger"],
        "run_guard": stack.safety["run_guard"],
        "store": WorkflowCheckpointStore(backend="memory"),
    }
    options.update(kwargs)
    return WorkflowRuntime(**options)


def document_request(**overrides: Any) -> WorkflowRunRequest:
    params = {
        "workspace.rag.search": {"query": "휴가 정책", "limit": 3},
        "office.xlsx.analyze": {"file_ref": "tables/q4.json"},
        "office.docx.compare": {"base_ref": "docs/base.md", "target_ref": "docs/target.md"},
    }
    payload: Dict[str, Any] = {
        "query": "휴가 정책 보고서 작성",
        "capabilities": list(DOCUMENT_PIPELINE),
        "execution_mode": "workflow",
        "context": {"workspace_id": "ws-1", "params": params},
    }
    payload.update(overrides)
    context = dict(payload.get("context") or {})
    context["params"] = {**params, **(context.get("params") or {})}
    payload["context"] = context
    return WorkflowRunRequest(**payload)


class StubResolver:
    """Server-side attribution double: bindings, allowed workspaces and revocation."""

    def __init__(self) -> None:
        self.bindings: Dict[str, Optional[str]] = {}
        self.allowed_workspaces = {"ws-1", "ws-2"}
        self.revoked: set = set()
        self.denied_tenants: set = set()
        self.calls: List[Dict[str, Any]] = []

    def resolve(
        self,
        scope: ExecutionScope,
        *,
        conversation_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> ScopeEvidence:
        self.calls.append({
            "tenant": scope.tenant_id, "conversation": conversation_id, "workspace": workspace_id,
        })
        if scope.tenant_id in self.denied_tenants:
            raise ScopeViolation("workspace_not_accessible", detail="tenant revoked")
        bound = self.bindings.get(conversation_id or "")
        if workspace_id and bound and workspace_id != bound:
            raise ScopeViolation(
                "conversation_workspace_mismatch", detail=f"bound={bound} requested={workspace_id}",
            )
        effective = workspace_id or bound
        if effective:
            if effective in self.revoked or effective not in self.allowed_workspaces:
                raise ScopeViolation("workspace_not_accessible", detail=str(effective))
            return ScopeEvidence(
                identity_source="stub", conversation_bound=bound is not None, workspace_verified=True,
                conversation_id=conversation_id, workspace_id=effective,
            )
        return ScopeEvidence(
            identity_source="stub", conversation_bound=bound is not None, conversation_id=conversation_id,
        )


def make_service(runtime: Optional[WorkflowRuntime] = None, **kwargs: Any) -> WorkflowService:
    runtime = runtime or runtime_for()
    resolver = kwargs.pop("resolver", None) or StubResolver()
    return WorkflowService(
        runtime=runtime,
        office=runtime.office,
        scope_authority=ExecutionScopeAuthority(resolver),
        **kwargs,
    )


@pytest.fixture()
def api():
    runtime = runtime_for()
    service = make_service(runtime)
    set_workflow_service(service)
    app = FastAPI()
    app.include_router(workflow_router, prefix="/api/v1")
    app.include_router(agent_router, prefix="/api/v1")
    client = TestClient(app)
    yield client, runtime, service
    set_workflow_service(None)


def _write_body(path: str = "reports/gated.md", **extra: Any) -> Dict[str, Any]:
    params = {"path": path, "content": "approved", "overwrite": True}
    params.update(extra.pop("params", {}))
    body: Dict[str, Any] = {
        "query": "요약본 저장",
        "execution_mode": "workflow",
        "capabilities": ["workspace.file.write"],
        "context": {"workspace_id": "ws-1", "params": {"workspace.file.write": params}},
    }
    body.update(extra)
    return body


class GatedAdapter(MockOfficeAdapter):
    """Blocks a write until the test releases it (race / concurrency tests)."""

    def __init__(self, documents: Optional[Mapping[str, str]] = None) -> None:
        super().__init__(documents=dict(documents or DOCUMENTS))
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.fail_codes: Dict[str, List[Optional[str]]] = {}
        self.available = True

    def fail_next(self, capability: str, code: Optional[str]) -> None:
        self.fail_codes.setdefault(capability, []).append(code)

    async def _maybe_fail(self, capability: str) -> None:
        queue = self.fail_codes.get(capability)
        if queue:
            code = queue.pop(0)
            if code:
                raise OfficeAdapterUnavailable(code, "injected")

    async def workspace_file_write(self, **kwargs: Any):
        self.entered.set()
        await self._maybe_fail("workspace.file.write")
        await self.gate.wait()
        return await super().workspace_file_write(**kwargs)

    async def xlsx_analyze(self, **kwargs: Any):
        await self._maybe_fail("office.xlsx.analyze")
        return await super().xlsx_analyze(**kwargs)


# ======================================================================================
# FIX-01 — execution scope
# ======================================================================================
class TestFix01ExecutionScope:
    def test_body_cannot_carry_authority_fields(self):
        resolver = StubResolver()
        authority = ExecutionScopeAuthority(resolver)
        scope = ExecutionScope(
            tenant_id="tenant-a", owner_id="user-1", service_id="agcore", role="user",
            authenticated=True, auth_source="credential_scope:c1",
        )
        for key, value in (
            ("tenant_id", "tenant-b"), ("owner_id", "mallory"), ("role", "admin"),
            ("scope", {"tenant_id": "tenant-b"}), ("is_admin", True),
        ):
            with pytest.raises(ScopeViolation) as excinfo:
                authority.resolve(scope, {"workspace_id": "ws-1", key: value})
            assert excinfo.value.code == "body_scope_overrides_forbidden"
        # Nothing was even resolved once the body tried to describe itself.
        assert resolver.calls == []

        resolved, evidence = authority.resolve(scope, {"workspace_id": "ws-1", "session_id": "sess-9"})
        assert resolved.tenant_id == "tenant-a" and resolved.owner_id == "user-1"
        assert resolved.workspace_id == "ws-1" and evidence.workspace_verified is True

    def test_conversation_bound_to_another_workspace_is_refused(self):
        resolver = StubResolver()
        resolver.bindings["conv-1"] = "ws-2"
        authority = ExecutionScopeAuthority(resolver)
        scope = ExecutionScope(
            tenant_id="tenant-a", owner_id="user-1", service_id="agcore", role="user",
            authenticated=True, auth_source="credential_scope:c1",
        )
        with pytest.raises(ScopeViolation) as excinfo:
            authority.resolve(scope, {"conversation_id": "conv-1", "workspace_id": "ws-1"})
        assert excinfo.value.code == "conversation_workspace_mismatch"

        # Without an explicit workspace the server-side binding wins over "nothing".
        resolved, _ = authority.resolve(scope, {"conversation_id": "conv-1"})
        assert resolved.workspace_id == "ws-2"

    def test_workspace_the_caller_cannot_open_is_refused(self, api):
        client, _runtime, service = api
        service.scope_authority.resolver.allowed_workspaces = {"ws-2"}
        response = client.post(
            "/api/v1/agent/workflows/runs",
            json={
                "query": "저장",
                "execution_mode": "workflow",
                "capabilities": ["workspace.file.write"],
                "context": {"workspace_id": "ws-other", "params": {"workspace.file.write": {
                    "path": "x.md", "content": "x", "overwrite": True,
                }}},
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "workspace_not_accessible"

    def test_body_scope_override_is_a_403_over_rest(self, api):
        client, _runtime, _service = api
        response = client.post(
            "/api/v1/agent/workflows/runs",
            json={
                "query": "저장", "execution_mode": "workflow",
                "capabilities": ["workspace.file.write"],
                "context": {"workspace_id": "ws-1", "tenant_id": "tenant-b"},
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "body_scope_overrides_forbidden"

    def test_resume_reverifies_the_binding_and_blocks_a_revoked_workspace(self, api):
        client, runtime, service = api
        adapter = GatedAdapter()
        runtime.office = adapter
        adapter.fail_next("workspace.file.write", "transient_backend")
        created = client.post(
            "/api/v1/agent/workflows/runs",
            json={
                "query": "저장", "execution_mode": "workflow",
                "capabilities": ["workspace.file.write"],
                "replan_on_failure": False,
                "context": {"workspace_id": "ws-1", "params": {"workspace.file.write": {
                    "path": "reports/revoked.md", "content": "x", "overwrite": True, "max_retries": 0,
                }}},
            },
        )
        assert created.status_code == 200
        run_id = created.json()["run_id"]
        assert created.json()["status"] == "failed"

        service.scope_authority.resolver.revoked.add("ws-1")
        resumed = client.post(f"/api/v1/agent/workflows/runs/{run_id}/resume", json={})
        assert resumed.status_code == 404
        assert resumed.json()["detail"]["code"] == "workflow_run_not_found"
        assert "reports/revoked.md" not in adapter.files


# ======================================================================================
# FIX-02 — idempotency, durable ledger and concurrency
# ======================================================================================
def write_step(step_id: str, path: str, *, depends_on: Sequence[str] = (), retries: int = 0) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        name=f"write {path}",
        capability="workspace.file.write",
        depends_on=tuple(depends_on),
        input_bindings={
            "workspace_id": "$scope.workspace_id",
            "path": path,
            "content": f"body of {path}",
            "overwrite": True,
        },
        output_key=step_id,
        max_retries=retries,
    )


class TestFix02IdempotencyAndConcurrency:
    def test_ledger_claim_is_exclusive_and_replays(self, tmp_path):
        ledger = ExecutionLedger(path=str(tmp_path / "ledger.db"), backend="sqlite")
        first = ledger.claim("run-1", "s1", scope_key="scope", side_effect="write")
        assert first.granted is True and first.status == "claimed"
        second = ledger.claim("run-1", "s1", scope_key="scope", side_effect="write")
        assert second.granted is False and second.status == "in_flight"

        ledger.succeed("run-1", "s1", {"written": True}, scope_key="scope")
        replay = ledger.claim("run-1", "s1", scope_key="scope", side_effect="write")
        assert replay.granted is False and replay.status == "replay"
        assert replay.replay_result == {"written": True}

        # The claim is durable: a second process/instance sees it too.
        other = ExecutionLedger(path=str(tmp_path / "ledger.db"), backend="sqlite")
        assert other.claim("run-1", "s1", scope_key="scope").status == "replay"
        other.close()
        ledger.close()

    def test_uncertain_outcome_is_never_replayed_automatically(self, tmp_path):
        ledger = ExecutionLedger(path=str(tmp_path / "ledger.db"), backend="sqlite")
        ledger.claim("run-2", "s1", scope_key="scope", side_effect="external")
        ledger.mark_uncertain("run-2", "s1", "timeout", scope_key="scope")
        outcome = ledger.claim("run-2", "s1", scope_key="scope", side_effect="external")
        assert outcome.granted is False and outcome.status == "recovery_required"

        # An explicit operator decision turns it into a retryable failure.
        ledger.fail("run-2", "s1", "retry_confirmed", scope_key="scope")
        retry = ledger.claim("run-2", "s1", scope_key="scope", side_effect="external")
        assert retry.granted is True and retry.attempt == 2
        ledger.close()

    def test_a_scope_is_part_of_the_ledger_key(self):
        ledger = ExecutionLedger()
        assert ledger.claim("run-3", "s1", scope_key="tenant-a::user-1").granted is True
        assert ledger.claim("run-3", "s1", scope_key="tenant-b::user-9").granted is True

    def test_run_guard_allows_one_executor_at_a_time(self):
        guard = RunExecutionGuard()
        token = guard.acquire("wfrun_x")
        with pytest.raises(RunBusy):
            guard.acquire("wfrun_x")
        guard.release("wfrun_x", token)
        guard.acquire("wfrun_x")

    async def test_resume_does_not_repeat_a_completed_side_effect(self):
        adapter = GatedAdapter()
        adapter.gate.set()
        adapter.fail_codes["workspace.file.write"] = [None, "invalid_table_payload"]
        runtime = runtime_for(adapter)
        request = WorkflowRunRequest(
            query="두개 저장",
            capabilities=["workspace.file.write"],
            execution_mode="workflow",
            replan_on_failure=False,
            plan_steps=[write_step("w1", "reports/one.md"), write_step("w2", "reports/two.md", depends_on=("w1",))],
            context={"workspace_id": "ws-1", "replan_on_failure": False},
        )
        first = await runtime.start(request, ctx())
        assert first.status == "failed", first.error
        assert adapter.side_effects.count("write:ws-1:reports/one.md") == 1

        resumed = await runtime.resume(first.run_id, ctx())
        assert resumed.status == "completed", resumed.error
        assert adapter.side_effects.count("write:ws-1:reports/one.md") == 1
        assert adapter.side_effects.count("write:ws-1:reports/two.md") == 1

    async def test_ambiguous_timeout_holds_the_run_for_recovery(self):
        adapter = GatedAdapter()
        adapter.gate.set()
        adapter.fail_codes["workspace.file.write"] = ["timeout"]
        runtime = runtime_for(adapter)
        request = WorkflowRunRequest(
            query="저장",
            capabilities=["workspace.file.write"],
            execution_mode="workflow",
            replan_on_failure=False,
            plan_steps=[write_step("w1", "reports/uncertain.md", retries=2)],
            context={"workspace_id": "ws-1", "replan_on_failure": False},
        )
        result = await runtime.start(request, ctx())
        assert result.status == "recovery_required", result.error
        assert result.recovery["step_id"] == "w1" and result.recovery["reason"] == "timeout"
        assert runtime.store.load_raw(result.run_id).status == "recovery_required"
        record = runtime.execution_ledger.lookup(result.run_id, "w1", scope_key=ctx().scope_hash())
        assert record is not None and record.status == "uncertain"
        # Only one attempt reached the adapter before the hold.
        assert adapter.side_effects == []

        with pytest.raises(WorkflowRuntimeError) as excinfo:
            await runtime.resume(result.run_id, ctx())
        assert excinfo.value.code == "recovery_required"

        resolved = await runtime.resolve_recovery(result.run_id, ctx(), action="retry", resolved_by="user-1")
        assert resolved.status == "running"

        resumed = await runtime.resume(result.run_id, ctx())
        assert resumed.status == "completed", resumed.error
        assert adapter.side_effects == ["write:ws-1:reports/uncertain.md"]

    async def test_cancellation_wins_the_race_against_a_finishing_step(self):
        adapter = GatedAdapter()
        runtime = runtime_for(adapter)
        events: List[str] = []
        request = WorkflowRunRequest(
            query="저장",
            capabilities=["workspace.file.write"],
            execution_mode="workflow",
            run_id="wfrun_cancel_race",
            plan_steps=[write_step("w1", "reports/race.md")],
            context={"workspace_id": "ws-1"},
        )
        task = asyncio.create_task(runtime.start(request, ctx(), emit=lambda e: events.append(e["event"])))
        await asyncio.wait_for(adapter.entered.wait(), timeout=5)
        cancelled = await runtime.cancel("wfrun_cancel_race", ctx())
        assert cancelled.status == "cancelled"
        adapter.gate.set()
        result = await task

        assert result.status == "cancelled"
        assert runtime.store.load_raw("wfrun_cancel_race").status == "cancelled"
        assert "workflow.completed" not in events
        assert "workflow.cancelled" in events
        # The side effect happened once; resume can never replay it again.
        assert adapter.side_effects == ["write:ws-1:reports/race.md"]
        with pytest.raises(WorkflowRuntimeError):
            await runtime.resume("wfrun_cancel_race", ctx())

    async def test_concurrent_resume_executes_only_once(self):
        adapter = GatedAdapter()
        adapter.gate.set()
        adapter.fail_codes["workspace.file.write"] = ["transient_backend"]
        runtime = runtime_for(adapter)
        request = WorkflowRunRequest(
            query="저장",
            capabilities=["workspace.file.write"],
            execution_mode="workflow",
            replan_on_failure=False,
            plan_steps=[write_step("w1", "reports/concurrent.md")],
            context={"workspace_id": "ws-1", "replan_on_failure": False},
        )
        failed = await runtime.start(request, ctx())
        assert failed.status == "failed"

        adapter.gate = asyncio.Event()
        adapter.entered = asyncio.Event()
        one = asyncio.create_task(runtime.resume(failed.run_id, ctx()))
        await asyncio.wait_for(adapter.entered.wait(), timeout=5)
        outcomes = await asyncio.gather(runtime.resume(failed.run_id, ctx()), return_exceptions=True)
        assert isinstance(outcomes[0], WorkflowRuntimeError)
        assert outcomes[0].code == "run_in_progress"
        adapter.gate.set()
        result = await one
        assert result.status == "completed", result.error
        assert adapter.side_effects.count("write:ws-1:reports/concurrent.md") == 1

    def test_recovery_endpoint_requires_an_explicit_decision(self, api):
        client, runtime, _service = api
        adapter = GatedAdapter()
        adapter.gate.set()
        adapter.fail_codes["workspace.file.write"] = ["timeout"]
        runtime.office = adapter
        created = client.post("/api/v1/agent/workflows/runs", json=_write_body(
            "reports/recover.md", params={"max_retries": 0},
        ))
        body = created.json()
        assert created.status_code == 202
        assert body["status"] == "recovery_required"
        assert body["recovery"]["reason"] == "timeout"
        run_id = body["run_id"]

        blocked = client.post(f"/api/v1/agent/workflows/runs/{run_id}/resume", json={})
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "recovery_required"

        resolved = client.post(f"/api/v1/agent/workflows/runs/{run_id}/recovery", json={"action": "retry"})
        assert resolved.status_code == 200
        resumed = client.post(f"/api/v1/agent/workflows/runs/{run_id}/resume", json={})
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "completed"
        assert adapter.side_effects == ["write:ws-1:reports/recover.md"]

        again = client.post(f"/api/v1/agent/workflows/runs/{run_id}/recovery", json={"action": "retry"})
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "workflow_not_in_recovery"


# ======================================================================================
# FIX-03 — real MCP transports (local stdio server + local streamable-HTTP server)
# ======================================================================================
STDIO_COMMAND = f"{sys.executable} {REPO_ROOT / 'tests/fixtures/mcp_stdio_server.py'}"


@pytest.fixture()
def http_mcp():
    from tests.fixtures.mcp_http_server import make_server

    server, state, port = make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/mcp", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestFix03RealMcpTransport:
    def test_default_factory_builds_real_transports(self):
        from app.workflow.mcp import default_client_factory

        stdio_config = MCPServerConfig(server_id="s", transport='stdio' if False else "stdio", command_or_url=STDIO_COMMAND)
        assert isinstance(default_client_factory(stdio_config, None), StdioMCPClient)
        http_config = MCPServerConfig(server_id="h", command_or_url="https://mcp.example.com/mcp")
        client = default_client_factory(http_config, "tok-123")
        assert isinstance(client, StreamableHTTPMCPClient)
        assert client._headers["authorization"] == "Bearer tok-123"

        # The stub is now an explicit test/opt-out helper, never the default.
        assert not isinstance(default_client_factory(stdio_config, None), type(unavailable_client_factory(stdio_config, None)))

    async def test_stdio_server_lifecycle_discovery_and_calls(self):
        registry = UnifiedToolRegistry(bridge_legacy=False)
        manager = MCPClientManager(registry)
        config = MCPServerConfig(
            server_id="stdio", transport="stdio", command_or_url=STDIO_COMMAND,
            allowed_tools=("echo", "purge"), timeout_seconds=10, max_retries=0,
            tool_policies={"echo": "read_only", "purge": "destructive"}, policy_mode="strict",
        )
        manager.register_server(config)
        assert await manager.connect("stdio") == manager.state("stdio")
        report = await manager.discover_tools("stdio")
        assert report.registered == ("mcp.stdio.echo", "mcp.stdio.purge")

        echo = registry.require("mcp.stdio.echo")
        assert echo.side_effect == "read" and echo.approval_required is False

        result = await manager.call_tool("stdio", "echo", {"text": "hello"}, context=ctx())
        assert result.content == {"echo": "hello"}
        with pytest.raises(MCPApprovalRequired):
            await manager.call_tool("stdio", "purge", {"id": "1"}, context=ctx())

        resources = await manager.discover_resources("stdio")
        assert resources[0].uri == "memo://1"
        prompt = await manager.bind_prompt("stdio", "summarise", {"uri": "memo://1"})
        assert "summarise it" in prompt.tagged

        client = manager._clients["stdio"]
        assert client.running is True
        await manager.close("stdio")
        assert client.running is False
        assert client.returncode is not None

    async def test_stdio_tool_runs_inside_a_workflow(self):
        registry = UnifiedToolRegistry(bridge_legacy=False)
        manager = MCPClientManager(registry)
        manager.register_server(MCPServerConfig(
            server_id="stdio", transport="stdio", command_or_url=STDIO_COMMAND,
            allowed_tools=("echo",), timeout_seconds=10, max_retries=0,
            tool_policies={"echo": "read_only"}, policy_mode="strict",
        ))
        await manager.discover_tools("stdio")
        runtime = runtime_for(mcp=manager, registry=registry)
        result = await runtime.start(
            WorkflowRunRequest(
                query="echo", capabilities=["mcp.stdio.echo"], execution_mode="workflow",
                plan_steps=[PlanStep(step_id="e1", capability="mcp.stdio.echo",
                                     input_bindings={"text": "$request.query"}, output_key="echo")],
            ),
            ctx(),
        )
        assert result.status == "completed", result.error
        assert result.trajectory["mcp_executions"][0]["server_id"] == "stdio"
        assert result.trajectory["tool_calls"][0]["result_summary"] == {"echo": "echo"}
        await manager.close_all()

    async def test_streamable_http_session_tracking_and_sse_response(self, http_mcp):
        url, state = http_mcp
        registry = UnifiedToolRegistry(bridge_legacy=False)
        manager = MCPClientManager(registry)
        manager.register_server(MCPServerConfig(
            server_id="http", command_or_url=url, allowed_tools=("lookup",),
            timeout_seconds=5, max_retries=0, allow_private_network=True,
            tool_policies={"lookup": "read_only"}, policy_mode="strict",
        ))
        assert await manager.connect("http") == manager.state("http")
        report = await manager.discover_tools("http")
        assert report.registered == ("mcp.http.lookup",)

        result = await manager.call_tool("http", "lookup", {"q": "C-1"}, context=ctx())
        assert result.content == {"ok": True, "q": "C-1"}
        resource = await manager.read_resource("http", "memo://http")
        assert "sse body" in json.dumps(resource.content)

        assert state.missing_session_attempts == 0
        assert [item["method"] for item in state.requests][:2] == ["initialize", "notifications/initialized"]
        await manager.close("http")
        assert state.deleted is True

    async def test_transport_timeout_is_typed_as_ambiguous(self):
        from app.workflow.transport import MCPTransportTimeout

        client = StdioMCPClient(STDIO_COMMAND, timeout_seconds=0.3)
        await client.initialize()
        with pytest.raises(MCPTransportTimeout):
            await client.call_tool("slow", {"seconds": 2})
        await client.close()


# ======================================================================================
# FIX-04 — Omni runtime integration
# ======================================================================================
class RecordingHooks(HookRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.dispatched: List[str] = []

    async def dispatch(self, event, payload=None, **kwargs):  # noqa: ANN001 - mirrors the engine
        self.dispatched.append(getattr(event, "value", str(event)))
        return await super().dispatch(event, payload, **kwargs)


def agent_body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "message": "휴가 정책 정리",
        "execution_mode": "workflow",
        "capabilities": ["workspace.rag.search", "workspace.file.write"],
        "workflow": {
            "workspace_id": "ws-1",
            "params": {
                "workspace.rag.search": {"query": "휴가 정책", "limit": 3},
                "workspace.file.write": {"path": "reports/omni.md", "content": "omni", "overwrite": True},
            },
        },
    }
    body.update(overrides)
    return body


class TestFix04OmniIntegration:
    def test_workflow_is_entered_from_the_agent_endpoint(self, api):
        client, runtime, _service = api
        created = client.post("/api/v1/agent/runs", json=agent_body())
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["executor"] == "workflow"
        assert body["status"] == "completed"
        assert body["workflow_status"] == "completed"
        run_id = body["run_id"]
        assert run_id.startswith("wfrun_")

        fetch = client.get(f"/api/v1/agent/runs/{run_id}")
        assert fetch.status_code == 200
        payload = fetch.json()
        assert payload["status"] == "completed"
        assert payload["agent_id"] == "workflow"
        assert "reports/omni.md" in runtime.office.files

    def test_workflow_mode_requires_explicit_capabilities(self, api):
        client, _runtime, _service = api
        response = client.post("/api/v1/agent/runs", json=agent_body(capabilities=[]))
        assert response.status_code == 422
        assert response.json()["detail"] == "workflow_capabilities_required"

    def test_shared_runtime_registry_is_reachable_from_the_omni_side(self, api):
        _client, runtime, service = api
        bridge = OmniRuntimeBridge(service)
        described = bridge.describe()
        assert described["workflow_capability"]["id"] == "workflow.run"
        assert described["shared"]["skills"] >= 1
        assert set(described["shared"]["office_availability"]) == set(OFFICE_CAPABILITIES)
        capabilities = bridge.available_capabilities()
        assert "workspace.rag.search" in capabilities

    async def test_common_hooks_fire_on_the_omni_path(self, api):
        _client, runtime, service = api
        hooks = RecordingHooks()
        hooks.register(HookDefinition(
            hook_id="audit-session", event=HookEvent.SESSION_START, handler=lambda payload: {"seen": True},
        ))
        runtime.hooks = hooks
        bridge = OmniRuntimeBridge(service)
        from app.schemas.agent import AgentRunRequest

        await bridge.dispatch_session_start(AgentRunRequest(message="안녕", conversation_id="conv-1"), _scope())
        assert hooks.dispatched == ["session_start"]

    def test_plain_chat_does_not_construct_the_workflow_stack(self, monkeypatch):
        from app.api.workflow import peek_workflow_service, reset_workflow_service
        from app.workflow.omni import get_omni_bridge

        reset_workflow_service()
        assert peek_workflow_service() is None
        assert get_omni_bridge() is None


def _office_side_effects(client, run_id: str) -> List[str]:
    """Reach the mock adapter behind the API to count real side effects."""
    service = __import__("app.api.workflow", fromlist=["peek_workflow_service"]).peek_workflow_service()
    return list(service.runtime.office.side_effects)


def _scope(**overrides: Any) -> ExecutionScope:
    values = {
        "tenant_id": "tenant-a", "owner_id": "user-1", "service_id": "agcore",
        "role": "user", "authenticated": True, "auth_source": "credential_scope:c1",
    }
    values.update(overrides)
    return ExecutionScope(**values)


# ======================================================================================
# FIX-05 — LLM planner
# ======================================================================================
class FakeGateway:
    """Gateway double that returns one canned completion and records the calls."""

    def __init__(self, content: Any = None, *, error: Optional[BaseException] = None) -> None:
        self.content = content
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    async def complete(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return type("Reply", (), {"content": self.content})()


LLM_PLAN = {
    "goal": "휴가 정책 정리",
    "steps": [
        {
            "step_id": "s1_search", "name": "search", "capability": "workspace.rag.search",
            "input_bindings": {"workspace_id": "$scope.workspace_id", "query": "$request.query", "limit": 3},
            "output_key": "research",
        },
        {
            "step_id": "s2_write", "name": "write", "capability": "workspace.file.write",
            "depends_on": ["s1_search"],
            "input_bindings": {
                "workspace_id": "$scope.workspace_id", "path": "reports/llm.md",
                "content": {"$template": "hits: {hits}", "vars": {"hits": "$outputs.research.hit_count"}},
                "overwrite": True,
            },
            "output_key": "report", "approval_required": False,
        },
    ],
}


class TestFix05LlmPlanner:
    async def test_model_builds_a_validated_structured_dag(self):
        gateway = FakeGateway(json.dumps(LLM_PLAN))
        registry = UnifiedToolRegistry(bridge_legacy=False)
        registry.register(UnifiedToolSpec(
            name="search", namespace="workspace", capability="workspace.rag.search",
            description="hybrid retrieval inside a workspace",
            input_schema={"type": "object", "properties": {
                "workspace_id": {"type": "string"}, "query": {"type": "string"},
            }},
        ))
        planner = Planner(llm=LLMPlannerAdapter(gateway, registry=registry), registry=registry)
        decision = await planner.aplan(PlannerRequest(
            query="휴가 정책 정리", execution_mode="workflow",
            available_capabilities=("workspace.rag.search", "workspace.file.write"),
            context={"workspace_id": "ws-1"},
        ))
        assert decision.route == "workflow"
        assert [step.step_id for step in decision.plan.steps] == ["s1_search", "s2_write"]
        assert decision.plan.steps[1].depends_on == ("s1_search",)
        assert planner.model_calls == 1 and planner.last_plan_source == "llm"
        assert gateway.calls[0]["response_format"] == {"type": "json_object"}
        prompt = gateway.calls[0]["messages"][-1].content
        assert "workspace.rag.search" in prompt and "input_schema" in prompt
        # The plan is executable: same DAG validation as the deterministic planner.
        decision.plan.topological_order()

    async def test_direct_chat_never_calls_the_model(self):
        gateway = FakeGateway(json.dumps(LLM_PLAN))
        planner = Planner(llm=LLMPlannerAdapter(gateway))
        for request in (
            PlannerRequest(query="안녕", execution_mode="direct"),
            PlannerRequest(query="안녕", execution_mode="auto"),
            PlannerRequest(query="안녕", execution_mode="auto",
                           available_capabilities=("workspace.rag.search",)),
        ):
            decision = await planner.aplan(request)
            assert decision.route == "direct_chat"
        assert gateway.calls == [] and planner.model_calls == 0

    async def test_malformed_model_output_falls_back_to_the_deterministic_planner(self):
        gateway = FakeGateway("sorry, I cannot plan that")
        planner = Planner(llm=LLMPlannerAdapter(gateway))
        decision = await planner.aplan(PlannerRequest(
            query="휴가 정책 보고서 작성", execution_mode="workflow",
            available_capabilities=tuple(DOCUMENT_PIPELINE),
        ))
        assert decision.route == "workflow"
        assert decision.plan is not None and len(decision.plan.steps) == 5
        assert planner.last_plan_source == "deterministic_fallback"
        assert planner.last_llm_error["code"] == "llm_plan_not_json"

    async def test_model_cannot_widen_its_capabilities_or_create_a_cycle(self):
        escalated = {"goal": "x", "steps": [{"step_id": "s1", "capability": "workspace.file.write"}]}
        planner = Planner(llm=LLMPlannerAdapter(FakeGateway(json.dumps(escalated))))
        decision = await planner.aplan(PlannerRequest(
            query="조사", execution_mode="workflow", available_capabilities=("workspace.rag.search",),
        ))
        assert planner.last_plan_source == "deterministic_fallback"
        assert planner.last_llm_error["code"] == "llm_plan_capability_denied"
        assert all(step.capability == "workspace.rag.search" for step in decision.plan.steps)

        cycle = {"goal": "x", "steps": [
            {"step_id": "a", "capability": "workspace.rag.search", "depends_on": ["b"]},
            {"step_id": "b", "capability": "workspace.rag.search", "depends_on": ["a"]},
        ]}
        planner = Planner(llm=LLMPlannerAdapter(FakeGateway(json.dumps(cycle))))
        await planner.aplan(PlannerRequest(
            query="조사", execution_mode="workflow", available_capabilities=("workspace.rag.search",),
        ))
        assert planner.last_llm_error["code"] == "llm_plan_invalid_dag"

    async def test_gateway_failure_degrades_to_the_rules_planner(self):
        planner = Planner(llm=LLMPlannerAdapter(FakeGateway(error=RuntimeError("gateway_down"))))
        decision = await planner.aplan(PlannerRequest(
            query="휴가 정책 보고서 작성", execution_mode="workflow",
            available_capabilities=tuple(DOCUMENT_PIPELINE),
        ))
        assert decision.route == "workflow" and len(decision.plan.steps) == 5
        assert planner.last_plan_source == "deterministic_fallback"
        assert planner.llm_failures == 1

    def test_planner_prompt_never_carries_a_credential(self):
        adapter = LLMPlannerAdapter(FakeGateway("{}"))
        messages = adapter.build_messages(
            PlannerRequest(query="q", context={"workspace_id": "ws-1", "access_token": "SECRET-XYZ",
                                               "nested": {"api_key": "SECRET-2", "keep": "ok"}}),
            available_capabilities=(), max_steps=3,
        )
        body = "\n".join(message.content for message in messages)
        assert "SECRET-XYZ" not in body and "SECRET-2" not in body
        assert "ok" in body

    async def test_runtime_executes_a_model_planned_workflow(self):
        adapter = office()
        gateway = FakeGateway(json.dumps(LLM_PLAN))
        stack = build_workflow_stack(office_adapter=adapter)
        planner = Planner(
            llm=LLMPlannerAdapter(gateway, registry=stack.registry),
            skills=stack.skills, registry=stack.registry, rules=stack.rules,
        )
        runtime = WorkflowRuntime(
            registry=stack.registry, skills=stack.skills, rules=stack.rules,
            mcp=stack.mcp, office=adapter, approvals=stack.safety["approvals"],
            ledger=stack.safety["idempotency"], execution_ledger=stack.safety["execution_ledger"],
            store=WorkflowCheckpointStore(backend="memory"), planner=planner,
        )
        result = await runtime.start(
            WorkflowRunRequest(
                query="휴가 정책 정리", execution_mode="workflow",
                capabilities=["workspace.rag.search", "workspace.file.write"],
                context={"workspace_id": "ws-1"},
            ),
            ctx(),
        )
        assert result.status == "completed", result.error
        assert planner.model_calls == 1
        assert result.trajectory["metadata"]["planner_source"] == "llm"
        assert adapter.files["reports/llm.md"].startswith("hits: ")


# ======================================================================================
# FIX-06 — approval security
# ======================================================================================
class TestFix06ApprovalSecurity:
    def _authority(self):
        from app.workflow.safety import ApprovalAuthority

        return ApprovalAuthority("test-secret-key")

    def test_token_is_bound_to_the_exact_action(self):
        authority = self._authority()
        context = ctx()
        binding = {
            "run_id": "wfrun_1", "step_id": "s5_report", "capability": "workspace.file.write",
            "target": "path:reports/a.md", "args_hash": "sha256:aaa",
        }
        token = authority.issue_user_approval(context, approved_by="user-1", **binding)
        assert token.run_id == "wfrun_1" and token.args_hash == "sha256:aaa"

        ok = authority.verify_user_approval(context, token, expected=ApprovalBinding(**binding))
        assert ok.approved_by == "user-1" and ok.args_hash == "sha256:aaa"

        for field, value in (
            ("run_id", "wfrun_other"), ("step_id", "s1_search"),
            ("capability", "workspace.delete"), ("target", "path:reports/b.md"),
            ("args_hash", "sha256:bbb"),
        ):
            bad = dict(binding, **{field: value})
            token_again = authority.issue_user_approval(context, approved_by="user-1", **binding)
            with pytest.raises(ApprovalError) as excinfo:
                authority.verify_user_approval(
                    context, token_again, expected=ApprovalBinding(**bad), consume=False,
                )
            assert excinfo.value.code == "approval_binding_mismatch"
            assert field in str(excinfo.value)

    def test_a_bound_token_cannot_be_verified_without_its_binding(self):
        authority = self._authority()
        context = ctx()
        token = authority.issue_user_approval(
            context, approved_by="user-1", run_id="wfrun_1", step_id="s1",
        )
        with pytest.raises(ApprovalError) as excinfo:
            authority.verify_user_approval(context, token)
        assert excinfo.value.code == "approval_binding_required"

    def test_a_token_is_single_use(self):
        authority = self._authority()
        context = ctx()
        token = authority.issue_user_approval(context, approved_by="user-1", run_id="wfrun_2", step_id="s1")
        binding = ApprovalBinding(run_id="wfrun_2", step_id="s1")
        authority.verify_user_approval(context, token, expected=binding)
        with pytest.raises(ApprovalError) as excinfo:
            authority.verify_user_approval(context, token, expected=binding)
        assert excinfo.value.code == "approval_replayed"
        assert authority.is_consumed(token.nonce) is True

    def test_signature_covers_the_binding(self):
        authority = self._authority()
        context = ctx()
        token = authority.issue_user_approval(context, approved_by="user-1", run_id="wfrun_3", step_id="s1")
        tampered = token.model_copy(update={"step_id": "s9_other"})
        with pytest.raises(ApprovalError) as excinfo:
            authority.verify_user_approval(context, tampered, expected=ApprovalBinding(step_id="s9_other"))
        assert excinfo.value.code == "invalid_signature"

    def test_expiry_is_enforced(self):
        from datetime import datetime, timedelta, timezone

        authority = self._authority()
        context = ctx()
        token = authority.issue_user_approval(context, approved_by="user-1", ttl_seconds=1)
        with pytest.raises(ApprovalError) as excinfo:
            authority.verify_user_approval(
                context, token, now=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
        assert excinfo.value.code == "expired_approval"

    def test_rest_approver_comes_from_the_credential(self, api):
        client, _runtime, _service = api
        created = client.post("/api/v1/agent/workflows/runs", json=_write_body(params={"approval_required": True}))
        body = created.json()
        assert body["status"] == "awaiting_approval"
        run_id = body["run_id"]
        pending = body["pending_approval"]
        assert pending["capability"] == "workspace.file.write"

        approved = client.post(
            f"/api/v1/agent/workflows/runs/{run_id}/approve",
            json={"approved_by": "somebody-else"},
        )
        assert approved.status_code == 200
        token = approved.json()["approval"]
        assert approved.json()["approved_by_source"] == "authenticated_scope"
        assert token["approved_by"] == "default_user"          # the dev_local credential
        assert token["run_id"] == run_id
        assert token["step_id"] == pending["step_id"]
        assert token["args_hash"].startswith("sha256:")

        resumed = client.post(f"/api/v1/agent/workflows/runs/{run_id}/resume", json={"approval": token})
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "completed"

        # The approval is one-time: replaying it cannot execute the side effect again.
        replay = client.post(f"/api/v1/agent/workflows/runs/{run_id}/resume", json={"approval": token})
        assert replay.status_code == 200
        assert replay.json()["status"] == "completed"
        assert _office_side_effects(client, run_id).count("write:ws-1:reports/gated.md") == 1

    def test_machine_principal_cannot_approve(self, api):
        client, _runtime, _service = api
        created = client.post("/api/v1/agent/workflows/runs", json=_write_body(params={"approval_required": True}))
        run_id = created.json()["run_id"]
        refused = client.post(
            f"/api/v1/agent/workflows/runs/{run_id}/approve",
            json={"ttl_seconds": 60},
            headers={"X-HD2Lab-Role": "service"},
        )
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "approval_requires_user_principal"

    def test_resume_refuses_a_token_bound_to_another_step(self, api):
        client, runtime, _service = api
        created = client.post("/api/v1/agent/workflows/runs", json=_write_body(params={"approval_required": True}))
        run_id = created.json()["run_id"]
        stored = runtime.store.load_raw(run_id).scope or {}
        context = ExecutionContext(**{key: value for key, value in stored.items() if value is not None})
        forged = runtime.approvals.issue_user_approval(
            context, approved_by="default_user",
            approval_id=f"apreq_{run_id}_s1_write", run_id=run_id, step_id="s9_elsewhere",
            capability="workspace.file.write", args_hash="sha256:bogus",
        )
        refused = client.post(
            f"/api/v1/agent/workflows/runs/{run_id}/resume", json={"approval": forged.model_dump(mode="json")},
        )
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "approval_binding_mismatch"


# ======================================================================================
# FIX-07 — MCP & tool isolation
# ======================================================================================
class FakeToolClient:
    """In-process MCP double for isolation tests (no subprocess)."""

    def __init__(self, tools: Sequence[Mapping[str, Any]]) -> None:
        self.tools = list(tools)
        self.calls: List[Dict[str, Any]] = []

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> Sequence[Mapping[str, Any]]:
        return self.tools

    async def list_resources(self):
        return []

    async def list_prompts(self):
        return []

    async def read_resource(self, uri: str):
        return {"uri": uri}

    async def call_tool(self, name: str, arguments: Mapping[str, Any]):
        self.calls.append({"name": name, "arguments": dict(arguments)})
        return {"ok": True}

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def mcp_manager(tools: Sequence[Mapping[str, Any]]) -> tuple:
    registry = UnifiedToolRegistry(bridge_legacy=False)
    client = FakeToolClient(tools)
    manager = MCPClientManager(registry, client_factory=lambda _config, _secret: client)
    return registry, manager, client


class TestFix07McpToolIsolation:
    def test_server_management_requires_an_administrator(self):
        require_mcp_admin(_scope(role="admin"))
        require_mcp_admin(_scope(role="admiral"))
        require_mcp_admin(_scope(role="user", auth_source="dev_local"))     # local operator
        for scope in (
            _scope(role="user", auth_source="credential_scope:c1"),
            _scope(role="service", auth_source="credential_scope:c1"),
            _scope(role="user", auth_source="trusted_proxy"),
        ):
            with pytest.raises(ScopeViolation) as excinfo:
                require_mcp_admin(scope)
            assert excinfo.value.code == "mcp_admin_required"

    def test_endpoint_policy_blocks_ssrf_and_untrusted_schemes(self):
        for url, code in (
            ("http://127.0.0.1:8080/mcp", "mcp_private_endpoint_blocked"),
            ("http://localhost/mcp", "mcp_private_endpoint_blocked"),
            ("http://169.254.169.254/latest/meta-data", "mcp_private_endpoint_blocked"),
            ("http://10.0.0.5/mcp", "mcp_private_endpoint_blocked"),
            ("ftp://mcp.example.com/rpc", "mcp_scheme_not_allowed"),
            ("https://user:pw@mcp.example.com/rpc", "mcp_url_credentials_forbidden"),
        ):
            with pytest.raises(MCPSecurityError) as excinfo:
                validate_endpoint(url)
            assert excinfo.value.code == code, url

        assert validate_endpoint("https://mcp.example.com/rpc") == "https://mcp.example.com/rpc"
        with pytest.raises(MCPSecurityError) as excinfo:
            validate_endpoint("https://evil.example.net/rpc", allowed_hosts=("mcp.example.com",))
        assert excinfo.value.code == "mcp_host_not_allowlisted"
        with pytest.raises(ValueError):
            MCPServerConfig(server_id="ssrf", command_or_url="http://127.0.0.1:9000/mcp")
        allowed = MCPServerConfig(
            server_id="local", command_or_url="http://127.0.0.1:9000/mcp", allow_private_network=True,
        )
        assert allowed.allow_private_network is True

    def test_stdio_command_allowlist(self):
        with pytest.raises(MCPSecurityError) as excinfo:
            validate_command("bash -c 'rm -rf /'")
        assert excinfo.value.code == "mcp_command_not_allowlisted"
        with pytest.raises(MCPSecurityError) as excinfo:
            validate_command("python -c 'import os; os.system(\"id\")'")
        assert excinfo.value.code == "mcp_command_shell_forbidden"
        assert validate_command(STDIO_COMMAND)
        with pytest.raises(ValueError):
            MCPServerConfig(server_id="sh", transport="stdio", command_or_url="bash script.sh")

    async def test_servers_and_calls_are_isolated_by_scope(self):
        registry, manager, client = mcp_manager([{"name": "lookup", "inputSchema": {"type": "object"}}])
        manager.register_server(MCPServerConfig(
            server_id="crm", command_or_url="https://mcp.example.com/rpc", allowed_tools=("lookup",),
            tenant_id="tenant-a", owner_id="user-1", service_id="agcore",
            tool_policies={"lookup": "read_only"}, policy_mode="strict",
        ))
        manager.register_server(MCPServerConfig(
            server_id="shared", command_or_url="https://mcp.example.com/shared", allowed_tools=("lookup",),
            shared=True, tool_policies={"lookup": "read_only"}, policy_mode="strict",
        ))
        await manager.discover_tools("crm")
        await manager.discover_tools("shared")

        owner = ctx()
        intruder = ctx(tenant_id="tenant-b", owner_id="user-9")
        assert manager.servers(owner) == ["crm", "shared"]
        assert manager.servers(intruder) == ["shared"]
        descriptions = manager.describe(intruder)
        assert [item["server_id"] for item in descriptions["servers"]] == ["shared"]

        with pytest.raises(ScopeViolation):
            await manager.call_tool("crm", "lookup", {"q": "x"}, context=intruder)
        assert client.calls == [] or all(call["name"] for call in client.calls)
        allowed = await manager.call_tool("shared", "lookup", {"q": "x"}, context=intruder)
        assert allowed.content == {"ok": True}

    async def test_server_policy_overrides_server_annotations(self):
        tools = [
            {"name": "search", "annotations": {"readOnlyHint": True},
             "inputSchema": {"type": "object"}},
            {"name": "wipe", "annotations": {"readOnlyHint": True},
             "inputSchema": {"type": "object"}},
            {"name": "undeclared", "annotations": {"readOnlyHint": True},
             "inputSchema": {"type": "object"}},
        ]
        registry, manager, _client = mcp_manager(tools)
        manager.register_server(MCPServerConfig(
            server_id="docs", command_or_url="https://mcp.example.com/rpc",
            allowed_tools=("search", "wipe", "undeclared"),
            tool_policies={"search": "read_only", "wipe": "destructive"}, policy_mode="strict",
        ))
        await manager.discover_tools("docs")

        search = registry.require("mcp.docs.search")
        assert search.side_effect == "read" and search.approval_required is False
        assert "policy_source:server" in search.tags

        wipe = registry.require("mcp.docs.wipe")
        assert wipe.side_effect == "destructive" and wipe.approval_required is True

        undeclared = registry.require("mcp.docs.undeclared")
        assert undeclared.side_effect == "external" and undeclared.approval_required is True
        assert "policy_source:strict_default" in undeclared.tags
        # A readOnlyHint alone never grants the tool a free pass in strict mode.
        with pytest.raises(MCPApprovalRequired):
            await manager.call_tool("docs", "undeclared", {}, context=ctx())

        # Advisory mode keeps the pre-existing behaviour for direct (engine-level) wiring.
        registry2, manager2, _ = mcp_manager(tools[:1])
        manager2.register_server(MCPServerConfig(
            server_id="docs", command_or_url="https://mcp.example.com/rpc", allowed_tools=("search",),
        ))
        await manager2.discover_tools("docs")
        advisory = registry2.require("mcp.docs.search")
        assert advisory.side_effect == "read" and "policy_source:annotation" in advisory.tags

    def test_diagnostics_never_leak_endpoints_or_credentials(self):
        registry, manager, _client = mcp_manager([{"name": "lookup", "inputSchema": {"type": "object"}}])
        manager.credentials.put("crm-token", "super-secret-value-123")
        manager.register_server(MCPServerConfig(
            server_id="crm", command_or_url="https://mcp.example.com/rpc?token=abc123secret",
            allowed_tools=("lookup",), credential_ref="crm-token", tenant_id="tenant-a",
        ))
        described = json.dumps(manager.describe(ctx()))
        assert "abc123secret" not in described
        assert "super-secret-value-123" not in described
        assert "[REDACTED]" in described
        assert mask_endpoint("https://u:p@host/mcp?key=1") == "https://host/mcp?key=[REDACTED]"

    def test_tool_policy_vocabulary_is_fixed(self):
        assert set(TOOL_POLICY_VALUES) == {
            "read_only", "read", "write", "external", "destructive", "approval_required",
        }
        with pytest.raises(ValueError):
            MCPServerConfig(
                server_id="x", command_or_url="https://mcp.example.com/rpc",
                tool_policies={"lookup": "superuser"},
            )

    def test_delete_server_endpoint_removes_registration(self, api):
        client, runtime, _service = api
        runtime.mcp = MCPClientManager(runtime.registry, client_factory=lambda _c, _s: FakeToolClient(
            [{"name": "lookup", "inputSchema": {"type": "object"}}]
        ))
        created = client.post("/api/v1/agent/workflows/mcp/servers", json={
            "server_id": "crm", "command_or_url": "https://mcp.example.com/rpc",
            "allowed_tools": ["lookup"], "tool_policies": {"lookup": "read_only"}, "policy_mode": "strict",
        })
        assert created.status_code == 201, created.text
        assert created.json()["access"]["tenant_id"] == "default_tenant"

        removed = client.delete("/api/v1/agent/workflows/mcp/servers/crm")
        assert removed.status_code == 200
        assert removed.json()["removed_tools"] == ["mcp.crm.lookup"]
        assert runtime.mcp.servers() == []
        assert client.delete("/api/v1/agent/workflows/mcp/servers/crm").status_code == 404


# ======================================================================================
# FIX-08 — R2.1 adapter contract
# ======================================================================================
class TestFix08R21AdapterContract:
    def test_runtime_adapter_reports_not_provided_capabilities_as_unavailable(self):
        adapter = RuntimeOfficeAdapter("https://agtools.internal", transport=_unused_transport)
        assert adapter.availability("workspace.rag.search").status == "available"
        for capability in ("office.xlsx.analyze", "office.docx.compare", "office.pptx.generate",
                           "workspace.file.write"):
            availability = adapter.availability(capability)
            assert availability.status == "unavailable"
            assert availability.detail == "capability_not_provided_by_runtime"
        with pytest.raises(OfficeAdapterUnavailable) as excinfo:
            asyncio.run(adapter.invoke("office.xlsx.analyze", {"file_ref": "a.json"}))
        assert excinfo.value.code == "capability_unavailable"
        assert AGTOOLS_PROVIDED_CAPABILITIES == frozenset({"workspace.rag.search"})
        assert set(OFFICE_CAPABILITIES) >= set(AGTOOLS_PROVIDED_CAPABILITIES) | {
            "office.xlsx.analyze", "office.docx.compare", "office.pptx.generate", "workspace.file.write",
        }

    async def test_scope_and_credential_are_propagated_to_the_runtime(self):
        seen: List[Dict[str, Any]] = []

        async def transport(method, url, headers, payload, timeout):
            seen.append({"method": method, "url": url, "headers": headers, "payload": payload})
            return 200, {"ok": True, "capability": "workspace.rag.search", "data": {"hits": []}}

        adapter = RuntimeOfficeAdapter(
            "https://agtools.internal", transport=transport, token_provider=lambda: "svc-token-123",
        )
        scope = AdapterCallScope(tenant_id="tenant-a", owner_id="user-1", service_id="agcore")
        result = await adapter.invoke(
            "workspace.rag.search", {"workspace_id": "ws-1", "query": "pricing"}, call_scope=scope,
        )
        assert result.data == {"hits": []}
        headers = seen[0]["headers"]
        assert headers["X-HD2Lab-Tenant"] == "tenant-a"
        assert headers["X-HD2Lab-User"] == "user-1"
        assert headers["X-HD2Lab-Service"] == "agcore"
        assert headers["Authorization"] == "Bearer svc-token-123"
        assert "svc-token-123" not in repr(scope)

    async def test_runtime_step_fails_explicitly_instead_of_returning_mock_data(self):
        calls: List[str] = []

        async def transport(method, url, headers, payload, timeout):
            calls.append(payload.get("capability"))
            return 200, {"ok": True, "capability": payload.get("capability"), "data": {"hits": [{"path": "x"}]}}

        adapter = RuntimeOfficeAdapter("https://agtools.internal", transport=transport)
        runtime = runtime_for(adapter)
        result = await runtime.start(
            WorkflowRunRequest(
                query="스프레드시트 분석",
                capabilities=["workspace.rag.search", "office.xlsx.analyze"],
                execution_mode="workflow",
                replan_on_failure=False,
                plan_steps=[
                    PlanStep(step_id="s1", capability="workspace.rag.search",
                             input_bindings={"workspace_id": "$scope.workspace_id", "query": "$request.query"},
                             output_key="research"),
                    PlanStep(step_id="s2", capability="office.xlsx.analyze",
                             input_bindings={"file_ref": "tables/q4.json"}, depends_on=("s1",),
                             output_key="table"),
                ],
                context={"workspace_id": "ws-1", "replan_on_failure": False},
            ),
            ctx(),
        )
        assert result.status == "failed"
        assert result.error["code"] == "capability_unavailable"
        assert "office.xlsx.analyze" not in calls       # nothing was sent for a not-provided capability
        assert "workspace.rag.search" in calls

    def test_mock_adapter_is_unavailable_in_production(self):
        production = MockOfficeAdapter(environment="production")
        assert production.availability("workspace.file.write").status == "unavailable"
        with pytest.raises(OfficeAdapterUnavailable):
            asyncio.run(production.invoke("workspace.file.write", {
                "workspace_id": "ws-1", "path": "x.md", "content": "x",
            }))
        assert get_office_adapter("mock").availability("workspace.rag.search").status == "available"

    def test_tools_endpoint_reports_availability(self, api):
        client, _runtime, _service = api
        response = client.get("/api/v1/agent/workflows/tools")
        assert response.status_code == 200
        availability = response.json()["office_availability"]
        assert set(availability) == set(OFFICE_CAPABILITIES)
        assert availability["workspace.rag.search"]["status"] == "available"
        assert availability["workspace.rag.search"]["adapter"] == "mock"


async def _unused_transport(method, url, headers, payload, timeout):  # pragma: no cover
    raise AssertionError("the transport must not be reached")


# ======================================================================================
# R2 preservation smoke
# ======================================================================================
class TestR2Preservation:
    def test_agent_contract_keeps_its_existing_modes(self):
        from app.schemas.agent import AgentRunRequest

        # The pre-existing modes still validate exactly as before ...
        for mode in (None, "chat", "workspace", "document_analysis"):
            assert AgentRunRequest(message="hi", execution_mode=mode).execution_mode == mode
        # ... and the R3 entry point is additive.
        request = AgentRunRequest(message="hi", execution_mode="workflow", capabilities=["demo.x"])
        assert request.capabilities == ["demo.x"]

    def test_unknown_run_is_still_not_found_on_the_agent_contract(self, api):
        client, _runtime, _service = api
        response = client.get("/api/v1/agent/runs/run-unknown")
        assert response.status_code == 404
        assert response.json()["detail"] == "SESSION_NOT_FOUND"

    def test_workflow_router_prefix_is_unchanged(self, api):
        client, _runtime, _service = api
        # The pre-existing endpoint paths still answer.
        assert client.post("/api/v1/agent/workflows/plan", json={"query": "안녕"}).status_code == 200
        assert client.get("/api/v1/agent/workflows/skills").status_code == 200


class TestFix01ToolPolicyIntegration:
    class _DenyAll:
        def is_tool_allowed(self, tool_name: str, service_id: Optional[str]):
            return False, f"disallowed_by_profile:{service_id}"

    async def test_registry_capability_is_blocked_by_the_tool_policy(self):
        registry = UnifiedToolRegistry(bridge_legacy=False)
        registry.register(
            UnifiedToolSpec(name="echo", namespace="demo", capability="demo.echo",
                            input_schema={"type": "object", "properties": {"text": {"type": "string"}}}),
            handler=lambda text="": {"echo": text},
        )
        runtime = runtime_for(registry=registry, tool_policy=ToolAccessPolicy(self._DenyAll()))
        result = await runtime.start(
            WorkflowRunRequest(
                query="echo", capabilities=["demo.echo"], execution_mode="workflow",
                replan_on_failure=False,
                plan_steps=[PlanStep(step_id="e1", capability="demo.echo",
                                     input_bindings={"text": "$request.query"}, output_key="echo")],
                context={"replan_on_failure": False},
            ),
            ctx(),
        )
        assert result.status == "failed"
        assert result.error["code"] == "tool_not_permitted_for_service"
        # The denial happens before the handler runs: the step is recorded as an error, not
        # as a successful call, and no tool output exists.
        assert result.trajectory["tool_calls"][0]["status"] == "error"
        assert result.trajectory["tool_calls"][0]["error_code"] == "tool_not_permitted_for_service"
        assert result.trajectory["tool_calls"][0].get("result_summary") in (None, "")

    async def test_allowed_registry_capability_still_runs(self):
        registry = UnifiedToolRegistry(bridge_legacy=False)
        registry.register(
            UnifiedToolSpec(name="echo", namespace="demo", capability="demo.echo",
                            input_schema={"type": "object", "properties": {"text": {"type": "string"}}}),
            handler=lambda text="": {"echo": text},
        )

        class AllowAll:
            def is_tool_allowed(self, tool_name: str, service_id: Optional[str]):
                return True, None

        runtime = runtime_for(registry=registry, tool_policy=ToolAccessPolicy(AllowAll()))
        result = await runtime.start(
            WorkflowRunRequest(
                query="hello", capabilities=["demo.echo"], execution_mode="workflow",
                plan_steps=[PlanStep(step_id="e1", capability="demo.echo",
                                     input_bindings={"text": "$request.query"}, output_key="echo")],
            ),
            ctx(),
        )
        assert result.status == "completed", result.error
        assert result.trajectory["tool_calls"][0]["result_summary"] == {"echo": "hello"}
