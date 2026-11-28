"""WO-R3-PREDEV-FIX-02 — Final safety closure verification.

One class per corrected defect (P0 first, then P1).  Each test asserts the observable
contract (HTTP status/error code, run status, ledger record, process environment, DNS
decision, system message) rather than internal plumbing:

    P0-1  R2 immutable session binding (a plain chat never gains a workspace)
    P0-2  workspace/canvas tools are fail-closed when the feature is off or unverifiable
    P0-3  an expired execution lease is never a licence to run a side effect again
    P0-4  unknown-outcome failures always become ``recovery_required``
    P1-5  MCP HTTP egress: DNS resolution of the real target + redirects refused
    P1-6  MCP stdio: real path / dangerous args / sanitized env / process-group isolation
    P1-7  Human Approval needs a human principal (machine credentials are refused)
    P1-8  the ordinary Omni path reads the shared Skills / Rules / Hooks / MCP / Registry

Run:  /workspace/.venv-r2canvas/bin/python -m pytest tests/test_r3_fix02_closure.py -v
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.agent import router as agent_router
from app.api.workflow import WorkflowService, router as workflow_router, set_workflow_service
from app.config import settings
from app.core.auth import ConversationWorkspace, ExecutionScope
from app.runtime.runner import AgentRunner
from app.schemas.agent import AgentRunRequest
from app.workflow import (
    ApprovalAuthority,
    ApprovalBinding,
    ApprovalError,
    ExecutionLedger,
    ExecutionScopeAuthority,
    HookDefinition,
    HookEvent,
    HookRegistry,
    PlanStep,
    R2WorkspaceResolver,
    ScopeViolation,
    StreamableHTTPMCPClient,
    UnifiedToolRegistry,
    UnifiedToolSpec,
    WorkflowRunRequest,
    WorkspaceToolPolicy,
)
from app.workflow.isolation import (
    MCPSecurityError,
    assert_egress_target,
    resolve_host_addresses,
    resolve_executable,
    sanitize_child_env,
    validate_command,
    validate_endpoint,
)
from app.workflow.omni import OmniRuntimeBridge
from app.workflow.safety import classify_principal, ensure_human_approver
from app.workflow.scope import workspace_tool_kind
from app.workflow.transport import MCPTransportError, StdioMCPClient

from tests.test_r3_fix01_hardening import (  # noqa: E402 - shared R3 fixtures
    REPO_ROOT,
    STDIO_COMMAND,
    GatedAdapter,
    ctx,
    make_service,
    office,
    runtime_for,
    write_step,
    _scope,
)

ENV_SERVER = f"{sys.executable} {REPO_ROOT / 'tests/fixtures/mcp_env_server.py'}"


# ======================================================================================
# P0-1 — R2 immutable session binding
# ======================================================================================
class TestFix02SessionBinding:
    def _authority(self, bindings: Dict[str, Optional[str]]) -> ExecutionScopeAuthority:
        resolver = R2WorkspaceResolver(
            binding_lookup=lambda scope, conversation_id: (
                ConversationWorkspace(conversation_id, bindings[conversation_id])
                if conversation_id in bindings else None
            ),
            workspace_opener=lambda workspace_id, scope: type("W", (), {"service_id": scope.service_id})(),
            enabled=lambda: True,
        )
        return ExecutionScopeAuthority(resolver)

    def test_plain_chat_cannot_gain_a_workspace(self):
        authority = self._authority({"conv-chat": None})
        scope = _scope(role="user", auth_source="trusted_proxy")
        with pytest.raises(ScopeViolation) as excinfo:
            authority.resolve(scope, {"conversation_id": "conv-chat", "workspace_id": "ws-1"})
        assert excinfo.value.code == "conversation_workspace_unbound"

        resolved, evidence = authority.resolve(scope, {"conversation_id": "conv-chat"})
        assert resolved.workspace_id is None
        assert evidence.conversation_bound is True and evidence.workspace_verified is False

    def test_a_bound_conversation_keeps_its_workspace(self):
        authority = self._authority({"conv-ws": "ws-2"})
        scope = _scope(role="user", auth_source="trusted_proxy")
        resolved, evidence = authority.resolve(scope, {"conversation_id": "conv-ws"})
        assert resolved.workspace_id == "ws-2" and evidence.workspace_verified is True

        with pytest.raises(ScopeViolation) as excinfo:
            authority.resolve(scope, {"conversation_id": "conv-ws", "workspace_id": "ws-1"})
        assert excinfo.value.code == "conversation_workspace_mismatch"

    def test_rest_workflow_run_cannot_inject_a_workspace_into_a_chat_session(self):
        bindings = {"conv-chat": None}
        runtime = runtime_for()
        service = WorkflowService(
            runtime=runtime,
            office=runtime.office,
            scope_authority=ExecutionScopeAuthority(
                R2WorkspaceResolver(
                    binding_lookup=lambda scope, conversation_id: (
                        ConversationWorkspace(conversation_id, bindings[conversation_id])
                        if conversation_id in bindings else None
                    ),
                )
            ),
        )
        set_workflow_service(service)
        app = FastAPI()
        app.include_router(workflow_router, prefix="/api/v1")
        client = TestClient(app)
        try:
            response = client.post(
                "/api/v1/agent/workflows/runs",
                json={
                    "query": "저장",
                    "execution_mode": "workflow",
                    "capabilities": ["workspace.file.write"],
                    "context": {
                        "conversation_id": "conv-chat",
                        "workspace_id": "ws-1",
                        "params": {"workspace.file.write": {
                            "path": "x.md", "content": "x", "overwrite": True,
                        }},
                    },
                },
            )
        finally:
            set_workflow_service(None)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "conversation_workspace_unbound"


# ======================================================================================
# P0-2 — workspace / canvas tools are fail-closed
# ======================================================================================
def workspace_registry(capability: str, *, name: str = "write_file") -> tuple:
    registry = UnifiedToolRegistry(bridge_legacy=False)
    calls: List[Dict[str, Any]] = []

    async def handler(**kwargs: Any) -> Dict[str, Any]:
        calls.append(dict(kwargs))
        return {"written": kwargs.get("path")}

    registry.register(
        UnifiedToolSpec(
            name=name, namespace=capability.split(".")[0], capability=capability,
            permission="write", side_effect="write",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        ),
        handler=handler,
    )
    return registry, calls


def workspace_step(capability: str, step_id: str = "w1") -> PlanStep:
    return PlanStep(
        step_id=step_id, name=capability, capability=capability,
        input_bindings={"workspace_id": "$scope.workspace_id", "path": "reports/x.md"},
        output_key=step_id,
    )


class TestFix02WorkspaceToolGate:
    def test_capability_classification(self):
        assert workspace_tool_kind("workspace.file.write") == "workspace"
        assert workspace_tool_kind("workspace_read_file") == "workspace"
        assert workspace_tool_kind("canvas.apply_edit") == "canvas"
        assert workspace_tool_kind("office.xlsx.analyze") is None

    async def test_disabled_feature_blocks_a_registry_workspace_tool(self):
        registry, calls = workspace_registry("workspace.delete_file", name="delete_file")
        runtime = runtime_for(registry=registry)
        runtime.workspace_policy = WorkspaceToolPolicy(workspace_enabled=lambda _s: False)
        result = await runtime.start(
            WorkflowRunRequest(
                query="저장", capabilities=["workspace.delete_file"], execution_mode="workflow",
                plan_steps=[workspace_step("workspace.delete_file")],
                context={"workspace_id": "ws-1"},
            ),
            ctx(),
        )
        assert result.status == "failed"
        assert result.error["code"] == "workspace_tool_disabled"
        assert calls == []                      # the handler was never reached

    async def test_unverifiable_binding_blocks_the_tool(self):
        registry, calls = workspace_registry("workspace.delete_file", name="delete_file")
        runtime = runtime_for(registry=registry)

        def refuse(_context: Any) -> None:
            raise ScopeViolation("workspace_binding_unverified", detail="ws-1: revoked")

        runtime.workspace_policy = WorkspaceToolPolicy(
            workspace_enabled=lambda _s: True, verifier=refuse,
        )
        result = await runtime.start(
            WorkflowRunRequest(
                query="저장", capabilities=["workspace.delete_file"], execution_mode="workflow",
                plan_steps=[workspace_step("workspace.delete_file")],
                context={"workspace_id": "ws-1"},
            ),
            ctx(),
        )
        assert result.status == "failed"
        assert result.error["code"] == "workspace_binding_unverified"
        assert calls == []

    async def test_verified_binding_lets_the_tool_run(self):
        registry, calls = workspace_registry("workspace.delete_file", name="delete_file")
        runtime = runtime_for(registry=registry)
        runtime.workspace_policy = WorkspaceToolPolicy(
            workspace_enabled=lambda _s: True, verifier=lambda _context: None,
        )
        result = await runtime.start(
            WorkflowRunRequest(
                query="저장", capabilities=["workspace.delete_file"], execution_mode="workflow",
                plan_steps=[workspace_step("workspace.delete_file")],
                context={"workspace_id": "ws-1"},
            ),
            ctx(),
        )
        assert result.status == "completed", result.error
        assert calls and calls[0]["path"] == "reports/x.md"

    async def test_adapter_write_is_blocked_when_the_feature_is_disabled(self):
        """A production adapter-delegated write cannot bypass a disabled Workspace."""

        class RuntimeStyleAdapter(GatedAdapter):
            is_mock = False
            name = "runtime"

        adapter = RuntimeStyleAdapter()
        adapter.gate.set()
        runtime = runtime_for(adapter)
        runtime.workspace_policy = WorkspaceToolPolicy(workspace_enabled=lambda _s: False)
        result = await runtime.start(
            WorkflowRunRequest(
                query="저장", capabilities=["workspace.file.write"], execution_mode="workflow",
                plan_steps=[write_step("w1", "reports/disabled.md")],
                context={"workspace_id": "ws-1"},
            ),
            ctx(),
        )
        assert result.status == "failed"
        assert result.error["code"] == "workspace_tool_disabled"
        assert adapter.side_effects == []

    async def test_canvas_tool_requires_the_canvas_flag(self):
        registry, calls = workspace_registry("canvas.apply_edit", name="apply_edit")
        runtime = runtime_for(registry=registry)
        runtime.workspace_policy = WorkspaceToolPolicy(
            workspace_enabled=lambda _s: True,
            canvas_enabled=lambda _s: False,
            verifier=lambda _context: None,
        )
        result = await runtime.start(
            WorkflowRunRequest(
                query="편집", capabilities=["canvas.apply_edit"], execution_mode="workflow",
                plan_steps=[workspace_step("canvas.apply_edit")],
                context={"workspace_id": "ws-1"},
            ),
            ctx(),
        )
        assert result.status == "failed"
        assert result.error["code"] == "canvas_tool_disabled"
        assert calls == []

    async def test_workspace_tool_without_attribution_is_refused(self):
        registry, calls = workspace_registry("workspace.delete_file", name="delete_file")
        runtime = runtime_for(registry=registry)
        runtime.workspace_policy = WorkspaceToolPolicy(
            workspace_enabled=lambda _s: True, verifier=lambda _context: None,
        )
        result = await runtime.start(
            WorkflowRunRequest(
                query="저장", capabilities=["workspace.delete_file"], execution_mode="workflow",
                plan_steps=[PlanStep(
                    step_id="w1", name="write", capability="workspace.delete_file",
                    input_bindings={"path": "reports/x.md"}, output_key="w1",
                )],
                context={},
            ),
            ctx(workspace_id=None),
        )
        assert result.status == "failed"
        assert result.error["code"] == "workspace_binding_required"
        assert calls == []


# ======================================================================================
# P0-3 / P0-4 — ledger lease expiry and unknown outcomes
# ======================================================================================
class TestFix02LeaseAndRecovery:
    def test_expired_lease_yields_recovery_not_a_second_execution(self, tmp_path):
        ledger = ExecutionLedger(path=str(tmp_path / "lease.db"), backend="sqlite", lease_seconds=30)
        past = datetime.now(timezone.utc) - timedelta(seconds=600)
        first = ledger.claim("run-x", "s1", scope_key="sc", side_effect="write", now=past)
        assert first.granted is True

        # A different worker arrives after the lease expired: the outcome is unknown.
        outcome = ledger.claim("run-x", "s1", scope_key="sc", side_effect="write")
        assert outcome.granted is False
        assert outcome.status == "recovery_required"
        assert outcome.detail == "lease_expired"
        record = ledger.lookup("run-x", "s1", scope_key="sc")
        assert record.status == "uncertain" and record.error == "lease_expired_outcome_unknown"

        # Only an explicit operator decision (recorded as a failure) re-arms the claim.
        ledger.fail("run-x", "s1", "retry_confirmed_by_operator", scope_key="sc")
        retry = ledger.claim("run-x", "s1", scope_key="sc", side_effect="write")
        assert retry.granted is True and retry.attempt == 2
        ledger.close()

    async def test_run_holds_for_recovery_when_its_claim_expired(self):
        adapter = GatedAdapter()
        adapter.gate.set()
        runtime = runtime_for(adapter)
        run_id = "wfrun_fix02_lease"
        scope_key = ctx().scope_hash()
        runtime.execution_ledger.lease_seconds = 30
        runtime.execution_ledger.claim(
            run_id, "w1", idempotency_key=f"{run_id}:w1", scope_key=scope_key,
            side_effect="write", now=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        result = await runtime.start(
            WorkflowRunRequest(
                query="저장", capabilities=["workspace.file.write"], execution_mode="workflow",
                run_id=run_id, plan_steps=[write_step("w1", "reports/lease.md")],
                context={"workspace_id": "ws-1"},
            ),
            ctx(),
        )
        assert result.status == "recovery_required", result.error
        assert result.recovery["reason"] == "lease_expired"
        assert adapter.side_effects == []                 # nothing ran again
        with pytest.raises(Exception) as excinfo:
            await runtime.resume(run_id, ctx())
        assert getattr(excinfo.value, "code", "") == "recovery_required"

    async def test_broken_connection_is_treated_as_an_unknown_outcome(self):
        class ResettingAdapter(GatedAdapter):
            async def workspace_file_write(self, **kwargs: Any):
                raise ConnectionResetError("peer reset the connection")

        adapter = ResettingAdapter()
        adapter.gate.set()
        runtime = runtime_for(adapter)
        result = await runtime.start(
            WorkflowRunRequest(
                query="저장", capabilities=["workspace.file.write"], execution_mode="workflow",
                plan_steps=[write_step("w1", "reports/reset.md", retries=2)],
                context={"workspace_id": "ws-1"},
            ),
            ctx(),
        )
        assert result.status == "recovery_required", result.error
        record = runtime.execution_ledger.lookup(
            result.run_id, "w1", scope_key=ctx().scope_hash(),
        )
        assert record is not None and record.status == "uncertain"


# ======================================================================================
# P1-5 — MCP HTTP egress: real destination + redirects
# ======================================================================================
class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib handler API
        self.send_response(302)
        self.send_header("location", "http://169.254.169.254/latest/meta-data")
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:  # noqa: D401 - silence the test server
        return


class TestFix02Egress:
    def test_dns_resolution_of_a_private_target_is_refused(self):
        with pytest.raises(MCPSecurityError) as excinfo:
            validate_endpoint("https://rebind.example.com/mcp", resolver=lambda host: ["127.0.0.1"])
        assert excinfo.value.code == "mcp_private_endpoint_blocked"

        with pytest.raises(MCPSecurityError) as excinfo:
            assert_egress_target("https://rebind.example.com/mcp", resolver=lambda host: ["169.254.169.254"])
        assert excinfo.value.code == "mcp_private_endpoint_blocked"

        with pytest.raises(MCPSecurityError) as excinfo:
            validate_endpoint("https://rebind.example.com/mcp", resolver=lambda host: ["10.1.2.3"])
        assert excinfo.value.code == "mcp_private_endpoint_blocked"

    def test_public_resolution_is_allowed(self):
        assert validate_endpoint(
            "https://mcp.example.com/mcp", resolver=lambda host: ["93.184.216.34"],
        )
        assert resolve_host_addresses("127.0.0.1") == ["127.0.0.1"]

    def test_unresolvable_host_is_refused_at_connect_time(self):
        with pytest.raises(MCPSecurityError) as excinfo:
            assert_egress_target("https://does-not-resolve.invalid/mcp", resolver=lambda host: [])
        assert excinfo.value.code == "mcp_host_unresolvable"

    async def test_redirect_is_never_followed(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
            client = StreamableHTTPMCPClient(url, timeout_seconds=5, allow_private_network=True)
            with pytest.raises(MCPTransportError) as excinfo:
                await client.call_tool("lookup", {"q": "x"})
            assert excinfo.value.code == "mcp_redirect_blocked"
            await client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    async def test_private_endpoint_is_refused_at_request_time(self):
        client = StreamableHTTPMCPClient("http://127.0.0.1:9/mcp", timeout_seconds=2)
        with pytest.raises(MCPTransportError) as excinfo:
            await client.call_tool("lookup", {})
        assert excinfo.value.code == "mcp_egress_blocked"


# ======================================================================================
# P1-6 — MCP stdio: real path, dangerous args, env, process group
# ======================================================================================
class TestFix02StdioIsolation:
    def test_inline_code_and_unknown_launchers_are_refused(self):
        with pytest.raises(MCPSecurityError) as excinfo:
            validate_command("python -c 'print(1)'")
        assert excinfo.value.code in ("mcp_command_inline_code_forbidden", "mcp_command_shell_forbidden")

        with pytest.raises(MCPSecurityError) as excinfo:
            validate_command("node --eval 'require(\"fs\")'")
        assert excinfo.value.code in ("mcp_command_inline_code_forbidden", "mcp_command_shell_forbidden")

        with pytest.raises(MCPSecurityError) as excinfo:
            validate_command("bash run.sh")
        assert excinfo.value.code == "mcp_command_not_allowlisted"

        # A short-flag cluster carrying inline execution is refused too.
        with pytest.raises(MCPSecurityError) as excinfo:
            validate_command("python -ic 'print(1)'")
        assert excinfo.value.code == "mcp_command_inline_code_forbidden"

    def test_a_symlink_cannot_escape_the_allowlist(self, tmp_path):
        shadow = tmp_path / "python"
        shadow.symlink_to("/bin/echo")
        with pytest.raises(MCPSecurityError) as excinfo:
            validate_command(f"{shadow} server.py")
        assert excinfo.value.code == "mcp_command_path_escape"

    def test_real_interpreter_path_is_accepted(self):
        assert resolve_executable(sys.executable)
        assert validate_command(STDIO_COMMAND)

    def test_sensitive_environment_is_stripped(self):
        env = sanitize_child_env(
            {
                "OPENAI_API_KEY": "sk-live-secret",
                "GITHUB_TOKEN": "ghp_secret",
                "AWS_ACCESS_KEY_ID": "AKIA-secret",
                "DATABASE_URL": "postgres://user:pw@db/app",
                "AGCORE_APPROVAL_SECRET": "approval-secret",
                "MCP_LOG_LEVEL": "debug",
            },
            base={"PATH": "/usr/bin:/bin", "HOME": "/home/tester", "LANG": "C.UTF-8"},
        )
        assert env["PATH"] == "/usr/bin:/bin" and env["HOME"] == "/home/tester"
        for secret in ("OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "DATABASE_URL",
                       "AGCORE_APPROVAL_SECRET"):
            assert secret not in env
        # A variable outside the whitelist is not forwarded either.
        assert "MCP_LOG_LEVEL" not in env

    async def test_child_process_gets_a_clean_env_and_its_own_session(self):
        client = StdioMCPClient(
            ENV_SERVER, timeout_seconds=10,
            env={"OPENAI_API_KEY": "sk-live-secret", "GITHUB_TOKEN": "ghp_secret"},
        )
        await client.initialize()
        try:
            env_result = await client.call_tool("env", {})
            assert "OPENAI_API_KEY" not in env_result
            assert "GITHUB_TOKEN" not in env_result
            assert env_result.get("PATH")

            session = await client.call_tool("session", {})
            parent_sid = os.getsid(0) if hasattr(os, "getsid") else os.getpgrp()
            assert session["sid"] == session["pgid"]          # own process group
            assert session["sid"] != parent_sid                # own session
            pid = int(session["pid"])
            child_pids = [int(item) for item in session["children"]]
            assert child_pids
            assert client.pid == pid
            # It runs from the resolved real path, not from whatever PATH said.
            assert client.resolved_command and os.path.realpath(client.resolved_command) == client.resolved_command
        finally:
            await client.close()
        assert client.returncode is not None
        # Closing killed the whole process group, including the server's own child.
        deadline = time.time() + 5
        while time.time() < deadline:
            alive = [p for p in child_pids if _process_alive(p)]
            if not alive:
                break
            time.sleep(0.1)
        assert [p for p in child_pids if _process_alive(p)] == []


def _process_alive(pid: int) -> bool:
    """True only while the process is actually running (a zombie counts as dead)."""
    stat_path = f"/proc/{pid}/stat"
    if os.path.exists("/proc"):
        try:
            state = open(stat_path, encoding="ascii", errors="replace").read().split()[2]
        except (FileNotFoundError, IndexError, ProcessLookupError, PermissionError):
            state = ""
        if state:
            return state not in ("Z", "X", "x")
        if not os.path.exists(stat_path):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ======================================================================================
# P1-7 — Human Approval is for humans only
# ======================================================================================
class TestFix02HumanApproval:
    def test_principal_classification(self):
        machine_admin = _scope(role="admin", auth_source="credential_scope:bot")
        assert classify_principal(machine_admin) == "machine"
        assert classify_principal(_scope(role="service", auth_source="credential_scope:svc")) == "machine"
        assert classify_principal(_scope(role="user", auth_source="trusted_proxy")) == "user"
        assert classify_principal(_scope(role="admin", auth_source="trusted_proxy")) == "user"
        assert classify_principal(_scope(role="user", auth_source="dev_local")) == "user"
        anonymous = ExecutionScope("t", "", "", "guest", False, "unauthenticated")
        assert classify_principal(anonymous) == "anonymous"

    def test_a_machine_principal_cannot_mint_a_user_approval(self):
        authority = ApprovalAuthority("fix02-secret")
        with pytest.raises(ApprovalError) as excinfo:
            authority.issue_user_approval(ctx(), approved_by="bot", principal_kind="machine")
        assert excinfo.value.code == "approval_requires_user_principal"
        with pytest.raises(ApprovalError):
            ensure_human_approver(_scope(role="admin", auth_source="credential_scope:bot"))
        ensure_human_approver(_scope(role="user", auth_source="trusted_proxy"))

    def test_the_approver_kind_is_covered_by_the_signature(self):
        authority = ApprovalAuthority("fix02-secret")
        token = authority.issue_user_approval(ctx(), approved_by="user-1", run_id="wfrun_1", step_id="s1")
        tampered = token.model_copy(update={"approver_kind": "machine"})
        with pytest.raises(ApprovalError) as excinfo:
            authority.verify_user_approval(
                ctx(), tampered, expected=ApprovalBinding(run_id="wfrun_1", step_id="s1"),
                consume=False,
            )
        assert excinfo.value.code == "invalid_signature"

    def test_admin_api_key_is_refused_at_the_rest_boundary(self, monkeypatch):
        runtime = runtime_for()
        service = make_service(runtime)
        set_workflow_service(service)
        app = FastAPI()
        app.include_router(workflow_router, prefix="/api/v1")
        client = TestClient(app)
        try:
            created = client.post(
                "/api/v1/agent/workflows/runs",
                json={
                    "query": "요약본 저장", "execution_mode": "workflow",
                    "capabilities": ["workspace.file.write"],
                    "context": {"workspace_id": "ws-1", "params": {"workspace.file.write": {
                        "path": "reports/bot.md", "content": "x", "overwrite": True,
                        "approval_required": True,
                    }}},
                },
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
            stored_scope = runtime.store.load_raw(run_id).scope or {}
            tenant = stored_scope.get("tenant_id")

            # A second run is parked on a recovery hold, to show /recovery is human-only too.
            recovering = client.post(
                "/api/v1/agent/workflows/runs",
                json={
                    "query": "저장", "execution_mode": "workflow",
                    "capabilities": ["workspace.file.write"],
                    "context": {"workspace_id": "ws-1", "params": {"workspace.file.write": {
                        "path": "reports/bot2.md", "content": "x", "overwrite": True,
                    }}},
                },
            )
            recovery_run = recovering.json()["run_id"]
            runtime.store.compare_and_set_status(
                recovery_run, expected=("completed", "failed", "running", "awaiting_approval"),
                status="recovery_required", context=None,
            )

            monkeypatch.setattr(settings, "CREDENTIAL_SCOPES_JSON", json.dumps({
                "bot": {
                    "allowed_services": ["hd2ai-admin"],
                    "allowed_roles": ["admin"],
                    "allowed_tenants": ["*"],
                    "owner_delegation": True,
                },
                "desktop": {
                    "allowed_services": ["hd2ai-admin"],
                    "allowed_roles": ["admin"],
                    "allowed_tenants": ["*"],
                    "owner_delegation": True,
                    "principal_kind": "user",
                },
            }))
            monkeypatch.setattr(settings, "CREDENTIAL_SECRETS_JSON", json.dumps({
                "bot": ["bot-secret-value-1"], "desktop": ["desktop-secret-1"],
            }))
            headers = {
                "X-Service-Token": "bot-secret-value-1",
                "X-HD2Lab-Role": "admin",
                "X-HD2Lab-Service": "hd2ai-admin",
                "X-HD2Lab-Tenant": str(tenant),
                "X-HD2Lab-User": "default_user",
            }
            refused = client.post(f"/api/v1/agent/workflows/runs/{run_id}/approve", json={}, headers=headers)
            assert refused.status_code == 403
            assert refused.json()["detail"]["code"] == "approval_requires_user_principal"

            # The same credential declared as a human delegation may approve.
            human_headers = dict(headers, **{"X-Service-Token": "desktop-secret-1"})
            allowed = client.post(
                f"/api/v1/agent/workflows/runs/{run_id}/approve", json={}, headers=human_headers,
            )
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["approval"]["approver_kind"] == "user"

            # The recovery decision ("the side effect did not happen") is human-only too.
            refused_recovery = client.post(
                f"/api/v1/agent/workflows/runs/{recovery_run}/recovery",
                json={"action": "retry"}, headers=headers,
            )
            assert refused_recovery.status_code == 403
            assert refused_recovery.json()["detail"]["code"] == "approval_requires_user_principal"
        finally:
            set_workflow_service(None)


# ======================================================================================
# P1-8 — the ordinary Omni path shares Skills / Rules / Hooks / MCP / Registry
# ======================================================================================
class TestFix02OmniSharedStack:
    def test_shared_catalogue_is_exposed_to_a_chat_run(self):
        runtime = runtime_for()
        service = make_service(runtime)
        bridge = OmniRuntimeBridge(service)
        shared = bridge.shared_services(AgentRunRequest(message="report 요약해줘"), _scope())
        assert shared is not None
        catalog = shared.as_dict()
        assert "workspace.rag.search" in catalog["capabilities"]
        assert catalog["skills"], "the shared skill engine must be discoverable"
        assert "registry" in catalog and "mcp" in catalog

        guidance = "\n".join(shared.guidance_lines())
        assert "공통 스킬" in guidance and "공통 도구 카탈로그" in guidance

    async def test_chat_run_state_carries_the_shared_context(self):
        runtime = runtime_for()
        service = make_service(runtime)
        bridge = OmniRuntimeBridge(service)
        shared = bridge.shared_services(AgentRunRequest(message="report 요약해줘"), _scope())
        runner = AgentRunner()
        session = await runner.create_run(
            AgentRunRequest(message="report 요약해줘"), caller_scope=_scope(), shared_services=shared,
        )
        assert session.state.shared_context["capabilities"]
        texts = [message.content for message in session.state.messages]
        assert any("공통 스킬" in text or "공통 도구 카탈로그" in text for text in texts)
        assert session.shared_services is shared

    async def test_plain_chat_has_no_shared_context(self):
        runner = AgentRunner()
        session = await runner.create_run(AgentRunRequest(message="안녕"), caller_scope=_scope())
        assert session.state.shared_context == {}
        assert session.shared_services is None

    async def test_session_end_hook_fires_on_the_omni_path(self):
        from app.workflow.omni import OmniSharedServices

        hooks = HookRegistry()
        seen: List[Dict[str, Any]] = []
        hooks.register(HookDefinition(
            hook_id="end", event=HookEvent.SESSION_END, handler=lambda payload: seen.append(dict(payload)),
        ))
        services = OmniSharedServices(context={"capabilities": ["workflow.run"]}, hooks=hooks)
        runner = AgentRunner()
        session = await runner.create_run(
            AgentRunRequest(message="안녕"), caller_scope=_scope(), shared_services=services,
        )
        await runner._dispatch_shared_hook(
            session, HookEvent.SESSION_END, {"run_id": session.state.run_id, "executor": "omni"},
        )
        assert seen and seen[0]["run_id"] == session.state.run_id

    def test_common_hooks_are_registered_for_the_chat_path(self):
        hooks = HookRegistry()
        hooks.register(HookDefinition(
            hook_id="audit", event=HookEvent.SESSION_END, handler=lambda payload: {"seen": True},
        ))
        runtime = runtime_for(hooks=hooks)
        service = make_service(runtime)
        shared = OmniRuntimeBridge(service).shared_services(
            AgentRunRequest(message="안녕"), _scope(),
        )
        assert shared is not None and shared._hooks is hooks
