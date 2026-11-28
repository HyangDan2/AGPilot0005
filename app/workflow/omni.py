"""FIX-04 Omni runtime integration.

R3 must not become a second, competing runtime.  The Omni agent path (``/agent/runs`` →
``app/runtime/runner.py`` → ``langgraph_r1``) stays the default for chat and workspace
answers; the R3 graph is *one capability* of that same runtime:

* :attr:`OmniRuntimeBridge.workflow_capability` describes the workflow graph as a capability
  (``workflow.run``) so the planner/callers see it in the same catalogue as tools.
* :meth:`OmniRuntimeBridge.start_from_agent_request` turns an ``AgentRunRequest`` with
  ``execution_mode="workflow"`` into an R3 ``WorkflowRunRequest`` executing on the shared
  stack (one registry, one skill engine, one rules engine, one hook registry, one MCP
  manager) - no new graph, no copied policy.
* :meth:`OmniRuntimeBridge.dispatch_session_start` runs the *common* lifecycle hooks on the
  ordinary Omni path too, so an installation that adds a hook (audit, redaction, kill
  switch) gets it on chat as well as on workflows.

The bridge never creates the workflow stack on its own: ``app.api.workflow.get_workflow_service``
owns that lifecycle, and :func:`get_omni_bridge` returns ``None`` while R3 is not wired, so a
plain chat request behaves exactly as before.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.workflow.hooks import HookEvent
from app.workflow.runtime import WorkflowRunRequest, WorkflowRunResult, WorkflowRuntimeError

__all__ = [
    "WORKFLOW_CAPABILITY_ID",
    "OmniRuntimeBridge",
    "get_omni_bridge",
    "AGENT_STATUS_MAP",
]

WORKFLOW_CAPABILITY_ID = "workflow.run"

#: R3 run status -> the ``AgentRunResponse.status`` vocabulary (R1.3 contract).
AGENT_STATUS_MAP: Dict[str, str] = {
    "pending": "queued",
    "running": "running",
    "awaiting_approval": "paused",
    "recovery_required": "needs_input",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "direct_chat": "completed",
}


class OmniRuntimeBridge:
    """Adapter between the Omni agent contract and the R3 workflow runtime."""

    def __init__(self, service: Any) -> None:
        self.service = service

    # -- catalogue ----------------------------------------------------------------------
    @property
    def runtime(self) -> Any:
        return self.service.runtime

    @property
    def workflow_capability(self) -> Dict[str, Any]:
        return {
            "id": WORKFLOW_CAPABILITY_ID,
            "source": "downstream",
            "description": "Run a multi-step R3 workflow (plan → DAG → hooks → approval)",
            "execution_mode": "workflow",
            "requires": ["capabilities"],
            "entrypoint": "POST /api/v1/agent/workflows/runs",
        }

    def describe(self) -> Dict[str, Any]:
        runtime = self.runtime
        skills = runtime.skills
        hooks = runtime.hooks
        rules = runtime.rules
        mcp = runtime.mcp
        return {
            "workflow_capability": self.workflow_capability,
            "shared": {
                "skills": len(skills.discover_skills()) if skills is not None else 0,
                "hooks": len(hooks.describe().get("events", {})) if hooks is not None else 0,
                "rules": rules.describe() if hasattr(rules, "describe") else {},
                "mcp_servers": len(mcp.servers()) if mcp is not None else 0,
                "registry": runtime.registry.describe() if runtime.registry is not None else {},
                "office_availability": self.service.office_availability(),
            },
        }

    def available_capabilities(self) -> List[str]:
        runtime = self.runtime
        capabilities: List[str] = []
        if runtime.office is not None:
            capabilities.extend(runtime.office.capabilities)
        if runtime.registry is not None:
            for spec in runtime.registry.list_specs():
                if spec.capability not in capabilities:
                    capabilities.append(spec.capability)
        return capabilities

    # -- requests -----------------------------------------------------------------------
    def workflow_request(self, agent_request: Any, *, scope: Any) -> WorkflowRunRequest:
        """Translate an agent run request into an R3 workflow request.

        Only fields the caller actually controls (message, capabilities, context, budget)
        are forwarded; tenant/owner/service never come from the body - the service resolves
        them from the credential when the run context is built (FIX-01).
        """
        explicit = list(getattr(agent_request, "capabilities", None) or ())
        if not explicit:
            raise WorkflowRuntimeError(
                "workflow_capabilities_required",
                "execution_mode=workflow needs an explicit capability list",
                http_status=422,
            )
        workflow_context = dict(getattr(agent_request, "workflow", None) or {})
        if getattr(agent_request, "conversation_id", None):
            workflow_context.setdefault("conversation_id", agent_request.conversation_id)
        if getattr(agent_request, "workspace_id", None):
            workflow_context.setdefault("workspace_id", agent_request.workspace_id)
        if getattr(agent_request, "session_id", None):
            workflow_context.setdefault("session_id", agent_request.session_id)
        budget = dict(workflow_context.pop("budget", {}) or {})
        options = getattr(agent_request, "options", None)
        if options is not None and getattr(options, "max_steps", None):
            budget.setdefault("max_steps", int(options.max_steps))
        if options is not None and getattr(options, "max_tool_calls", None):
            budget.setdefault("max_tool_calls", int(options.max_tool_calls))
        plan_steps = workflow_context.pop("plan_steps", None)
        return WorkflowRunRequest(
            query=getattr(agent_request, "message", "") or "",
            context=workflow_context,
            execution_mode="workflow",
            capabilities=explicit,
            budget=budget,
            plan_steps=plan_steps,
            replan_on_failure=workflow_context.get("replan_on_failure"),
        )

    async def start_from_agent_request(self, agent_request: Any, scope: Any) -> WorkflowRunResult:
        request = self.workflow_request(agent_request, scope=scope)
        context = self.service.execution_context(scope, request)
        return await self.runtime.start(request, context)

    # -- shared hooks -------------------------------------------------------------------
    async def dispatch_session_start(self, agent_request: Any, scope: Any) -> None:
        """Run ``session_start`` on the common hook registry (fail-open for a plain chat)."""
        hooks = self.runtime.hooks
        if hooks is None:
            return
        try:
            await hooks.dispatch(
                HookEvent.SESSION_START,
                {
                    "run_id": None,
                    "executor": "omni",
                    "service_id": getattr(scope, "service_id", None),
                    "tenant_id": getattr(scope, "tenant_id", None),
                    "owner_id": getattr(scope, "owner_id", None),
                    "conversation_id": getattr(agent_request, "conversation_id", None),
                    "has_workspace": bool(getattr(agent_request, "workspace_id", None)),
                },
                granted_permission="admin",
            )
        except Exception:  # noqa: BLE001 - a chat must not fail because a hook did
            return


def get_omni_bridge(create: bool = False) -> Optional[OmniRuntimeBridge]:
    """Return the bridge for the process-wide workflow service, if it is wired."""
    from app.api.workflow import get_workflow_service, peek_workflow_service

    service = get_workflow_service() if create else peek_workflow_service()
    if service is None:
        return None
    return OmniRuntimeBridge(service)
