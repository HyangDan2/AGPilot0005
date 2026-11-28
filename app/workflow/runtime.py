"""WP-02 R3 Workspace Runtime — LangGraph execution graph.

The runtime executes an :class:`~app.workflow.planner.ExecutionPlan` as a compiled
LangGraph state machine::

    context_resolve -> rules_resolve -> capability_discover -> plan -> execute_dag
        -> validate_hooks -> store_artifacts -> report

``execute_dag`` is a deterministic scheduler over the plan DAG (sequential, parallel
fan-out/fan-in per ``parallel_group``, bounded retry/timeout, conditional skip,
cancellation) while the outer graph keeps every phase observable and checkpointed.

Human approval is a hard gate: when a step requires approval the run stops with
``status="awaiting_approval"`` and is checkpointed.  Only an :class:`ApprovalToken`
issued by :meth:`ApprovalAuthority.issue_user_approval` (``approved_by_user=True``) can
resume it — a model-generated token is rejected structurally by
:meth:`ApprovalAuthority.verify_user_approval`.

Cancellation, SSE progress and checkpoint saves are observable through the ``emit`` sink
of :meth:`WorkflowRuntime.start` / :meth:`WorkflowRuntime.resume`.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    TypedDict,
)

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.workflow.budget import BudgetExceeded, ContextBudgetManager
from app.workflow.checkpoint import WorkflowCheckpoint, WorkflowCheckpointStore
from app.workflow.hooks import HookEvent, HookValidationError
from app.workflow.ledger import (
    ExecutionLedger,
    RunBusy,
    RunExecutionGuard,
    SIDE_EFFECT_STEPS,
    StepLedgerRecord,
)
from app.workflow.mcp import (
    MCPApprovalRequired,
    MCPConfigError,
    MCPError,
    MCPRegistrationError,
    MCPToolDenied,
    MCPToolResult,
    MCPUnavailable,
)
from app.workflow.office_adapter import (
    AdapterCallScope,
    OfficeAdapterError,
    OfficeAdapterUnavailable,
    OfficeCapabilityAdapter,
    OfficeResult,
)
from app.workflow.planner import (
    BindingResolutionError,
    BindingResolver,
    ExecutionPlan,
    PlanStep,
    Planner,
    PlannerError,
    PlannerRequest,
    resolve_bindings,
)
from app.workflow.safety import (
    ApprovalAuthority,
    ApprovalBinding,
    ApprovalError,
    ApprovalToken,
    ExecutionContext,
    IdempotencyConflict,
    IdempotencyLedger,
    ScopeViolation,
    approval_args_hash,
)
from app.workflow.scope import ToolAccessPolicy
from app.workflow.trajectory import (
    ArtifactRecord,
    FailureRecord,
    MCPExecutionRecord,
    ReplanRecord,
    RetryRecord,
    SkillSelectionRecord,
    StepRecord,
    ToolCallRecord,
    WorkflowTrajectory,
)

__all__ = [
    "WorkflowRuntimeError",
    "WorkflowRunRequest",
    "WorkflowRunResult",
    "WorkflowRuntime",
    "WORKFLOW_EVENT_TYPES",
    "get_compiled_workflow_graph",
]

#: The nine additive SSE event types this runtime publishes.
WORKFLOW_EVENT_TYPES: tuple = (
    "workflow.started",
    "workflow.plan.created",
    "workflow.step.started",
    "workflow.step.completed",
    "workflow.approval.required",
    "workflow.checkpoint.saved",
    "workflow.completed",
    "workflow.failed",
    "workflow.cancelled",
)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_CHECKPOINT_STATUSES = frozenset(
    {"pending", "running", "awaiting_approval", "recovery_required", "completed", "failed", "cancelled"}
)
#: Failure codes that mean "the call may or may not have happened": never auto-retried
#: against a side effect (FIX-02).
AMBIGUOUS_ERROR_CODES = frozenset(
    {
        "tool_timeout", "mcp_timeout", "mcp_http_unavailable", "mcp_transport_error",
        "transport_error", "upstream_unavailable", "timeout", "mcp_stream_closed",
    }
)
#: Parameter names used to describe *what* an approval is for (in that order).
_APPROVAL_TARGET_PARAMS = ("path", "target_path", "id", "uri", "url", "name", "key", "recipient")
_COMPARISON_RE = re.compile(r"^\s*(\$[^\s]+)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------
class WorkflowRuntimeError(RuntimeError):
    """A run could not start, resume or continue under the current state."""

    def __init__(self, code: str, detail: str = "", *, http_status: int = 409) -> None:
        self.code = code
        self.detail = detail
        self.http_status = http_status
        super().__init__(code if not detail else f"{code}: {detail}")


# --------------------------------------------------------------------------------------
# Request / result models
# --------------------------------------------------------------------------------------
class WorkflowRunRequest(BaseModel):
    """API/runtime input for one workflow run."""

    model_config = ConfigDict(extra="ignore")

    query: str = ""
    context: Dict[str, Any] = Field(default_factory=dict)
    execution_mode: Literal["auto", "direct", "workflow"] = "workflow"
    capabilities: Sequence[str] = Field(default_factory=list)
    budget: Dict[str, Any] = Field(default_factory=dict)
    skills: Sequence[str] = Field(default_factory=list)
    plan_steps: Optional[List[PlanStep]] = None
    run_id: Optional[str] = None
    max_steps: Optional[int] = None
    max_tool_calls: Optional[int] = None
    max_retries: Optional[int] = None
    replan_on_failure: Optional[bool] = None

    @field_validator("capabilities", "skills", mode="before")
    @classmethod
    def _tuple(cls, value: Any) -> tuple:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(dict.fromkeys(str(item) for item in value))

    def planner_request(self) -> PlannerRequest:
        budget = dict(self.budget)
        if self.max_steps is not None:
            budget["max_steps"] = self.max_steps
        if self.max_retries is not None:
            budget["max_retries"] = self.max_retries
        if self.replan_on_failure is not None:
            budget["replan_on_failure"] = self.replan_on_failure
        return PlannerRequest(
            query=self.query,
            context=dict(self.context),
            budget=budget,
            execution_mode=self.execution_mode,
            available_capabilities=tuple(self.capabilities),
        )


class WorkflowRunResult(BaseModel):
    """The observable outcome of a start/resume/cancel call."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    route: str = "workflow"
    reason: str = ""
    plan_id: Optional[str] = None
    plan_version: int = 1
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    answer: Dict[str, Any] = Field(default_factory=dict)
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    failures: List[Dict[str, Any]] = Field(default_factory=list)
    pending_approval: Optional[Dict[str, Any]] = None
    recovery: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    token_usage: Dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0
    trajectory: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Graph state
# --------------------------------------------------------------------------------------
class WorkflowGraphState(TypedDict, total=False):
    run_id: str
    entry: str
    stage: str
    status: str
    route: str
    reason: str
    plan_id: Optional[str]


async def _node_context_resolve(state, config):  # noqa: ANN001 - langgraph node signature
    return await config["configurable"]["run_ctx"].context_resolve(state)


async def _node_rules_resolve(state, config):  # noqa: ANN001
    return await config["configurable"]["run_ctx"].rules_resolve(state)


async def _node_capability_discover(state, config):  # noqa: ANN001
    return await config["configurable"]["run_ctx"].capability_discover(state)


async def _node_plan(state, config):  # noqa: ANN001
    return await config["configurable"]["run_ctx"].plan_node(state)


async def _node_execute_dag(state, config):  # noqa: ANN001
    return await config["configurable"]["run_ctx"].execute_dag(state)


async def _node_validate_hooks(state, config):  # noqa: ANN001
    return await config["configurable"]["run_ctx"].validate_hooks(state)


async def _node_store_artifacts(state, config):  # noqa: ANN001
    return await config["configurable"]["run_ctx"].store_artifacts(state)


async def _node_report(state, config):  # noqa: ANN001
    return await config["configurable"]["run_ctx"].report(state)


def _route_entry(state: WorkflowGraphState) -> str:
    entry = state.get("entry") or "context_resolve"
    return entry if entry in ("context_resolve", "execute_dag", "report") else "context_resolve"


def _route_after_rules(state: WorkflowGraphState) -> str:
    return "report" if state.get("status") == "failed" else "capability_discover"


def _route_after_plan(state: WorkflowGraphState) -> str:
    return "report" if state.get("status") == "failed" else "execute_dag"


def _route_after_execute(state: WorkflowGraphState) -> str:
    return "validate_hooks" if state.get("status") == "completed" else "report"


_COMPILED_GRAPH: Optional[Any] = None


def get_compiled_workflow_graph() -> Any:
    """Compile the R3 workflow graph once per process (R1 convention: compile once)."""
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is not None:
        return _COMPILED_GRAPH
    builder = StateGraph(WorkflowGraphState)
    builder.add_node("context_resolve", _node_context_resolve)
    builder.add_node("rules_resolve", _node_rules_resolve)
    builder.add_node("capability_discover", _node_capability_discover)
    builder.add_node("plan", _node_plan)
    builder.add_node("execute_dag", _node_execute_dag)
    builder.add_node("validate_hooks", _node_validate_hooks)
    builder.add_node("store_artifacts", _node_store_artifacts)
    builder.add_node("report", _node_report)
    builder.add_conditional_edges(
        START, _route_entry,
        {"context_resolve": "context_resolve", "execute_dag": "execute_dag", "report": "report"},
    )
    builder.add_edge("context_resolve", "rules_resolve")
    builder.add_conditional_edges(
        "rules_resolve", _route_after_rules,
        {"capability_discover": "capability_discover", "report": "report"},
    )
    builder.add_edge("capability_discover", "plan")
    builder.add_conditional_edges(
        "plan", _route_after_plan, {"execute_dag": "execute_dag", "report": "report"},
    )
    builder.add_conditional_edges(
        "execute_dag", _route_after_execute,
        {"validate_hooks": "validate_hooks", "report": "report"},
    )
    builder.add_edge("validate_hooks", "store_artifacts")
    builder.add_edge("store_artifacts", "report")
    builder.add_edge("report", END)
    _COMPILED_GRAPH = builder.compile()
    return _COMPILED_GRAPH


def _coerce(raw: str) -> Any:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("none", "null"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


# --------------------------------------------------------------------------------------
# Per-run context
# --------------------------------------------------------------------------------------
class _RunContext:
    """Mutable state of exactly one run; also the langgraph ``run_ctx``."""

    def __init__(
        self,
        runtime: "WorkflowRuntime",
        *,
        run_id: str,
        request: WorkflowRunRequest,
        context: ExecutionContext,
        plan: Optional[ExecutionPlan] = None,
        completed_steps: Optional[Sequence[str]] = None,
        outputs: Optional[Mapping[str, Any]] = None,
        replans: int = 0,
        granted_approvals: Optional[Mapping[str, bool]] = None,
        trajectory: Optional[WorkflowTrajectory] = None,
        emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
        entry: str = "context_resolve",
    ) -> None:
        self.runtime = runtime
        self.run_id = run_id
        self.request = request
        self.context = context
        self.plan = plan
        self.entry = entry
        self.completed_steps: List[str] = list(completed_steps or ())
        self.skipped_steps: List[str] = []
        self.outputs: Dict[str, Any] = dict(outputs or {})
        self.replans = int(replans)
        self.granted_approvals: Dict[str, bool] = dict(granted_approvals or {})
        self.trajectory = trajectory or WorkflowTrajectory(run_id=run_id)
        self.emit = emit
        self.status = "running"
        self.pending_approval_id: Optional[str] = None
        self.failures: List[Dict[str, Any]] = []
        self.artifacts: List[ArtifactRecord] = []
        self.rules_effective: Dict[str, Any] = {}
        self.skill_grants: Dict[str, Any] = {}
        self.route = "workflow"
        self.reason = ""
        self.error: Optional[Dict[str, Any]] = None
        self.recovery: Optional[Dict[str, Any]] = None
        self.capabilities: List[str] = []
        self.started_at = time.perf_counter()
        self.budget = runtime.new_budget(request)
        self._token_usage = self.trajectory.token_usage

    # -- shortcuts --------------------------------------------------------------------
    @property
    def store(self) -> WorkflowCheckpointStore:
        return self.runtime.store

    @property
    def registry(self) -> Any:
        return self.runtime.registry

    @property
    def skills(self) -> Any:
        return self.runtime.skills

    @property
    def hooks(self) -> Any:
        return self.runtime.hooks

    @property
    def office(self) -> Optional[OfficeCapabilityAdapter]:
        return self.runtime.office

    @property
    def planner(self) -> Planner:
        return self.runtime.planner

    @property
    def ledger(self) -> IdempotencyLedger:
        return self.runtime.ledger

    @property
    def approvals(self) -> ApprovalAuthority:
        return self.runtime.approvals

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000

    async def emit_event(self, event: str, data: Optional[Mapping[str, Any]] = None) -> None:
        if self.emit is None:
            return
        payload = {"run_id": self.run_id, "event": event, "status": self.status}
        payload.update(dict(data or {}))
        try:
            result = self.emit(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - a broken consumer must not fail the run
            pass

    # -- checkpoint -------------------------------------------------------------------
    def _build_checkpoint(
        self, *, status: Optional[str] = None, existing: Optional[WorkflowCheckpoint] = None
    ) -> WorkflowCheckpoint:
        now = _utcnow()
        resolved = status or self.status
        if resolved not in _CHECKPOINT_STATUSES:
            resolved = "running"
        return WorkflowCheckpoint(
            run_id=self.run_id,
            scope=self.context.scope_map(),
            plan=self.plan.model_dump(mode="json") if self.plan else {},
            completed_steps=list(self.completed_steps),
            step_outputs=self.outputs,
            pending_approval_id=self.pending_approval_id,
            status=resolved,  # type: ignore[arg-type]
            created_at=existing.created_at if existing else now,
            updated_at=now,
            plan_version=self.plan.version if self.plan else 1,
            replans=self.replans,
            failures=list(self.failures),
            metadata={
                "request": self.request.model_dump(mode="json"),
                "route": self.route,
                "reason": self.reason,
                "capabilities": list(self.request.capabilities),
                "pending_approval": self.trajectory.metadata.get("pending_approval"),
                "recovery": self.recovery or self.trajectory.metadata.get("recovery"),
            },
        )

    async def save_checkpoint(self, *, status: Optional[str] = None, step_id: Optional[str] = None) -> None:
        existing = self.store.load_raw(self.run_id)
        resolved = status or self.status
        # FIX-02 cancel/completion race: once a cancellation is persisted it wins, and a
        # recovery hold can only be left by an explicit decision (never by a late task
        # finishing on its own).
        if existing is not None:
            if existing.status == "cancelled" and resolved not in ("cancelled", "failed"):
                self.status = "cancelled"
                resolved = "cancelled"
                self.error = self.error or {"code": "workflow_cancelled", "message": "run cancelled by caller"}
            elif existing.status == "recovery_required" and resolved in ("running", "completed"):
                resolved = "recovery_required"
        checkpoint = self._build_checkpoint(status=resolved, existing=existing)
        self.store.save(checkpoint, trajectory=self.trajectory.to_dict())
        await self.emit_event(
            "workflow.checkpoint.saved",
            {
                "checkpoint_status": checkpoint.status,
                "step_id": step_id,
                "completed_steps": len(checkpoint.completed_steps),
            },
        )

    # -- hooks ------------------------------------------------------------------------
    async def dispatch_hook(
        self,
        event: HookEvent,
        payload: Mapping[str, Any],
        *,
        fail_open_on_error: bool = False,
    ) -> Any:
        """Dispatch one lifecycle hook.

        The runtime is the platform principal, so hooks are dispatched with admin
        authority; a hook that fails closed (``HookValidationError``) aborts the action it
        guards unless ``fail_open_on_error`` is set for purely observational events.
        """
        if self.hooks is None:
            return None
        before = len(self.hooks.records())
        try:
            result = await self.hooks.dispatch(event, dict(payload), granted_permission="admin")
        except HookValidationError:
            # A fail-closed hook aborts the guarded action; its execution record is in the
            # registry history (dispatch raised instead of returning it), so copy it over
            # to keep the failure visible in the trajectory.
            for record in self.hooks.records()[before:]:
                self.trajectory.add_hook_records(getattr(event, "value", str(event)), [record])
            if fail_open_on_error:
                return None
            raise
        self.trajectory.add_hook_records(getattr(event, "value", str(event)), result.records)
        if getattr(result, "blocked", False):
            raise HookValidationError("hook_blocked_event", detail=str(event))
        return result

    # -- phase 1: context -------------------------------------------------------------
    async def context_resolve(self, state: WorkflowGraphState) -> WorkflowGraphState:
        await self.emit_event(
            "workflow.started",
            {"query": self.request.query, "execution_mode": self.request.execution_mode, "entry": self.entry},
        )
        await self.dispatch_hook(
            HookEvent.SESSION_START,
            {"run_id": self.run_id, "query": self.request.query, "scope": self.context.scope_map()},
            fail_open_on_error=True,
        )
        context_tokens = self.budget.count(
            json.dumps(
                {"query": self.request.query, "context": self.request.context},
                ensure_ascii=False, default=str,
            )
        )
        self._token_usage = self._token_usage.add(context_tokens=context_tokens)
        self.trajectory.set_token_usage(self._token_usage)
        await self.save_checkpoint(status="running")
        return {"stage": "context_resolve", "status": self.status, "run_id": self.run_id}

    # -- phase 2: rules ---------------------------------------------------------------
    async def rules_resolve(self, state: WorkflowGraphState) -> WorkflowGraphState:
        try:
            engine = self.runtime.build_rules_engine(self.request)
            resolution = engine.resolve()
        except Exception as exc:  # noqa: BLE001 - rules are mandatory context: fail closed
            return await self._fail_phase("rules", "rule_resolution_failed", f"{type(exc).__name__}: {exc}")
        self.rules_effective = dict(resolution.effective)
        self.trajectory.add_rules([record.model_dump(mode="json") for record in resolution.applied])
        if resolution.blocked:
            self.trajectory.metadata["rule_conflicts_blocked"] = [
                {"key": conflict.key, "reason": conflict.reason} for conflict in resolution.blocked
            ]
        return {"stage": "rules_resolve"}

    # -- phase 3: capabilities & skills -----------------------------------------------
    async def capability_discover(self, state: WorkflowGraphState) -> WorkflowGraphState:
        available = [str(item) for item in self.request.capabilities]
        discovered: List[str] = list(available)
        if self.office is not None:
            discovered.extend(cap for cap in self.office.capabilities if cap not in discovered)
        if self.registry is not None:
            for spec in self.registry.list_specs():
                if spec.capability in available and spec.capability not in discovered:
                    discovered.append(spec.capability)
        self.capabilities = discovered
        # Discovery only — skill bodies are loaded lazily, when a plan step names one.
        if self.skills is not None and self.request.execution_mode != "direct":
            summaries = self.skills.discover_skills(query=self.request.query or None)
            self.trajectory.metadata["skill_candidates"] = [summary.name for summary in summaries]
        return {"stage": "capability_discover"}

    # -- phase 4: plan ----------------------------------------------------------------
    async def plan_node(self, state: WorkflowGraphState) -> WorkflowGraphState:
        await self.dispatch_hook(
            HookEvent.BEFORE_PLANNING, {"run_id": self.run_id, "query": self.request.query},
            fail_open_on_error=True,
        )
        if self.plan is None:
            try:
                if self.request.plan_steps:
                    plan = self._plan_from_explicit_steps(self.request.plan_steps)
                    decision_route, decision_reason = "workflow", "explicit_plan"
                else:
                    decision = await self.planner.aplan(self.request.planner_request())
                    decision_route, decision_reason = decision.route, decision.reason
                    plan = decision.plan
            except PlannerError as exc:
                return await self._fail_phase("planner", exc.code, str(exc))
            self.route, self.reason = decision_route, decision_reason
            self.trajectory.metadata["planner_source"] = getattr(self.planner, "last_plan_source", "deterministic")
            if decision_route == "direct_chat":
                self.status = "direct_chat"
                self.trajectory.metadata["route"] = "direct_chat"
                self.trajectory.metadata["reason"] = decision_reason
                await self.save_checkpoint(status="completed")
                return {"stage": "plan", "status": "direct_chat", "route": "direct_chat", "reason": decision_reason}
            self.plan = plan

        assert self.plan is not None
        try:
            await self._bind_skills(self.plan)
        except PlannerError as exc:
            return await self._fail_phase("planner", exc.code, str(exc))
        self.trajectory.set_plan(self.plan.summary())
        self.trajectory.set_token_usage(self._token_usage.add(context_tokens=int(self.plan.estimated_tokens or 0)))
        await self.save_checkpoint(status="running")
        await self.dispatch_hook(
            HookEvent.AFTER_PLANNING,
            {"run_id": self.run_id, "plan_id": self.plan.plan_id, "step_count": len(self.plan.steps)},
            fail_open_on_error=True,
        )
        await self.emit_event("workflow.plan.created", {"plan": self.plan.summary(), "replan": False})
        return {"stage": "plan", "status": "running", "plan_id": self.plan.plan_id, "route": "workflow"}

    def _plan_from_explicit_steps(self, steps: Sequence[PlanStep]) -> ExecutionPlan:
        budget = dict(self.request.budget or {})
        max_steps = int(self.request.max_steps or budget.get("max_steps") or max(len(steps), 1))
        if len(steps) > max_steps:
            raise PlannerError("max_steps_exceeded", f"steps={len(steps)} max_steps={max_steps}")
        replan_on_failure = (
            self.request.replan_on_failure
            if self.request.replan_on_failure is not None
            else bool(budget.get("replan_on_failure", True))
        )
        plan = ExecutionPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:16]}",
            goal=(self.request.query or "workflow")[:280],
            steps=tuple(steps),
            max_steps=max_steps,
            max_retries=int(self.request.max_retries if self.request.max_retries is not None else budget.get("max_retries", 2)),
            estimated_tokens=sum(len(json.dumps(step.model_dump(mode="json"))) // 4 for step in steps),
            replan_on_failure=replan_on_failure,
        )
        self.planner.plans_built += 1
        return plan

    async def _bind_skills(self, plan: ExecutionPlan) -> None:
        if self.skills is None:
            return
        held = [str(item) for item in self.request.capabilities]
        for step in plan.steps:
            name = step.skill_name
            if not name:
                continue
            try:
                grant = self.skills.authorize_execution(name, held)
                loaded = self.skills.load_skill(name)
            except Exception as exc:  # noqa: BLE001 - privilege/format problems are plan problems
                raise PlannerError("skill_privilege_denied", f"{name}: {type(exc).__name__}: {exc}") from exc
            self.skill_grants[name] = grant
            self.trajectory.add_skill(
                SkillSelectionRecord(
                    skill=name,
                    version=getattr(loaded.manifest, "version", None),
                    reason=f"planned for step {step.step_id}",
                    loaded=True,
                    allowed_capabilities=list(grant.allowed_capabilities),
                )
            )

    # -- phase 5: DAG execution -------------------------------------------------------
    async def execute_dag(self, state: WorkflowGraphState) -> WorkflowGraphState:
        if self.status == "direct_chat":
            return {"stage": "execute_dag", "status": "direct_chat"}
        if self.plan is None:
            return await self._fail_phase("planner", "missing_plan", "no plan to execute")
        self.status = await self._run_dag()
        if self.status == "failed" and self.error is None:
            self.error = {"code": "workflow_failed", "message": "workflow failed"}
        return {
            "stage": "execute_dag",
            "status": self.status,
            "plan_id": self.plan.plan_id if self.plan else None,
        }

    async def _run_dag(self) -> str:
        while True:
            if self.runtime.is_cancelled(self.run_id):
                return await self._cancel_run()
            assert self.plan is not None
            pending = [
                step for step in self.plan.steps
                if step.step_id not in self.completed_steps and step.step_id not in self.skipped_steps
            ]
            if not pending:
                return "completed"
            ready = [
                step for step in pending
                if all(dep in self.completed_steps or dep in self.skipped_steps for dep in step.depends_on)
            ]
            if not ready:
                for step in pending:
                    self._mark_skipped(step, "dependency_unavailable")
                await self.save_checkpoint()
                return "completed"

            parallel = [step for step in ready if step.parallel_group]
            sequential = [step for step in ready if not step.parallel_group]
            if parallel:
                outcome = await self._run_parallel(parallel)
                if outcome in ("awaiting_approval", "failed", "cancelled", "recovery_required"):
                    return outcome
                if outcome == "replanned":
                    continue
            for step in sequential:
                outcome = await self.run_step(step)
                if outcome in ("awaiting_approval", "failed", "cancelled", "recovery_required"):
                    return outcome
                if outcome == "replanned":
                    break

    async def _run_parallel(self, steps: Sequence[PlanStep]) -> str:
        groups: Dict[str, List[PlanStep]] = {}
        for step in steps:
            groups.setdefault(str(step.parallel_group), []).append(step)
        for members in groups.values():
            results = await asyncio.gather(*(self.run_step(step) for step in members), return_exceptions=True)
            for step, result in zip(members, results):
                if isinstance(result, BaseException):
                    failure = await self._register_failure(
                        step, code="parallel_step_crashed", message=f"{type(result).__name__}: {result}",
                        cause_type=type(result).__name__, attempts=1, source="runtime",
                    )
                    return await self._finalize_failure(step, failure)
                if result in ("awaiting_approval", "failed", "cancelled", "replanned", "recovery_required"):
                    return str(result)
        return "completed"

    def _mark_skipped(self, step: PlanStep, reason: str) -> None:
        if step.step_id in self.skipped_steps:
            return
        self.skipped_steps.append(step.step_id)
        self.trajectory.add_step(
            StepRecord(
                step_id=step.step_id, name=step.name, capability=step.capability,
                status="skipped", started_at=_utcnow(), ended_at=_utcnow(), error=reason,
            )
        )

    async def run_step(self, step: PlanStep) -> str:
        if self.runtime.is_cancelled(self.run_id):
            return await self._cancel_run()
        if step.step_id in self.completed_steps:
            self.trajectory.add_step(
                StepRecord(step_id=step.step_id, name=step.name, capability=step.capability,
                           status="replayed", output_key=step.key)
            )
            return "replayed"

        if step.condition and not self._condition_holds(step.condition):
            self._mark_skipped(step, "condition_false")
            return "skipped"

        idempotency_key = f"{self.run_id}:{step.step_id}"
        try:
            ledger_record = self.ledger.lookup(step.step_id, idempotency_key, context=self.context)
        except IdempotencyConflict:
            ledger_record = None
        if ledger_record is not None and ledger_record.status == "completed":
            self.outputs[step.key] = ledger_record.result
            if step.step_id not in self.completed_steps:
                self.completed_steps.append(step.step_id)
            self.trajectory.add_step(
                StepRecord(step_id=step.step_id, name=step.name, capability=step.capability,
                           status="replayed", output_key=step.key)
            )
            return "replayed"

        try:
            params = self._resolve_params(step)
        except BindingResolutionError as exc:
            failure = await self._register_failure(
                step, code=exc.code, message=str(exc), cause_type=type(exc).__name__,
                attempts=1, source="planner",
            )
            return await self._finalize_failure(step, failure)

        # The approval gate binds the token to the exact step *and* its resolved arguments
        # (FIX-06), so the parameters must be settled before the pause.
        if self._needs_approval(step) and not self.granted_approvals.get(step.step_id):
            return await self._pause_for_approval(step, params)

        try:
            self.budget.guard_step()
        except BudgetExceeded as exc:
            failure = await self._register_failure(
                step, code=exc.code, message=str(exc), cause_type=type(exc).__name__,
                attempts=0, source="budget",
            )
            return await self._finalize_failure(step, failure)

        # FIX-02: side effects are claimed in the durable ledger *before* they run, so a
        # retry, a resume or a second worker can never repeat them.
        side_effect = self._side_effect_of(step)
        ledger_claimed = False
        if side_effect in SIDE_EFFECT_STEPS:
            claim = self.runtime.execution_ledger.claim(
                self.run_id, step.step_id,
                idempotency_key=idempotency_key, scope_key=self.context.scope_hash(),
                side_effect=side_effect, attempt=1,
            )
            if not claim.granted:
                if claim.status == "replay":
                    self.outputs[step.key] = claim.replay_result
                    if step.step_id not in self.completed_steps:
                        self.completed_steps.append(step.step_id)
                    self.trajectory.add_step(
                        StepRecord(step_id=step.step_id, name=step.name, capability=step.capability,
                                   status="replayed", output_key=step.key)
                    )
                    await self.save_checkpoint(step_id=step.step_id)
                    return "replayed"
                if claim.status == "recovery_required":
                    return await self._enter_recovery(step, claim.record, reason=claim.detail)
                failure = await self._register_failure(
                    step, code="step_in_flight", message=claim.detail or "another attempt is running",
                    cause_type="LedgerClaim", attempts=0, source="runtime",
                )
                return await self._finalize_failure(step, failure)
            ledger_claimed = True
            await self.emit_event(
                "workflow.step.started",
                {"step_id": step.step_id, "capability": step.capability, "name": step.name,
                 "ledger": claim.status, "side_effect": side_effect},
            )

        self.trajectory.add_step(
            StepRecord(step_id=step.step_id, name=step.name, capability=step.capability,
                       status="pending", output_key=step.key, started_at=_utcnow())
        )
        if not ledger_claimed:
            await self.emit_event(
                "workflow.step.started",
                {"step_id": step.step_id, "capability": step.capability, "name": step.name},
            )

        attempts = max(0, int(step.max_retries)) + 1
        last_code, last_message, last_cause = "step_failed", "", ""
        transport = "unknown"
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                await self.dispatch_hook(
                    HookEvent.BEFORE_TOOL_CALL,
                    {"run_id": self.run_id, "step_id": step.step_id, "capability": step.capability,
                     "tool_name": step.tool_name or step.capability, "arguments": params},
                )
                if step.capability == "workspace.file.write":
                    await self.dispatch_hook(
                        HookEvent.BEFORE_FILE_WRITE,
                        {"run_id": self.run_id, "step_id": step.step_id,
                         "workspace_id": params.get("workspace_id"), "path": params.get("path"),
                         "overwrite": bool(params.get("overwrite"))},
                    )
                value, transport, meta = await asyncio.wait_for(
                    self._invoke_capability(step, params), timeout=float(step.timeout_seconds)
                )
            except HookValidationError as exc:
                duration = (time.perf_counter() - started) * 1000
                failure = await self._register_failure(
                    step, code=exc.code, message=str(exc), cause_type=type(exc).__name__,
                    attempts=attempt, source="hook", duration_ms=duration,
                )
                return await self._finalize_failure(step, failure)
            except asyncio.TimeoutError:
                duration = (time.perf_counter() - started) * 1000
                last_code, last_message, last_cause = "tool_timeout", f"timeout after {step.timeout_seconds}s", "TimeoutError"
                self.trajectory.add_tool_call(
                    ToolCallRecord(
                        step_id=step.step_id, capability=step.capability, transport=transport,
                        tool_name=step.tool_name or step.capability, attempt=attempt, arguments=params,
                        status="timeout", duration_ms=duration, error_code=last_code, error=last_message,
                    )
                )
                retryable = True
            except Exception as exc:  # noqa: BLE001 - classified into the failure contract
                duration = (time.perf_counter() - started) * 1000
                last_code, last_message, retryable, last_cause = self.runtime.classify_error(exc)
                self.trajectory.add_tool_call(
                    ToolCallRecord(
                        step_id=step.step_id, capability=step.capability, transport=transport,
                        tool_name=step.tool_name or step.capability, attempt=attempt, arguments=params,
                        status="error", duration_ms=duration, error_code=last_code, error=last_message,
                    )
                )
                if isinstance(exc, (ScopeViolation, ApprovalError, IdempotencyConflict)):
                    retryable = False
            else:
                duration = (time.perf_counter() - started) * 1000
                self.trajectory.add_tool_call(
                    ToolCallRecord(
                        step_id=step.step_id, capability=step.capability, transport=transport,
                        tool_name=step.tool_name or step.capability, attempt=attempt, arguments=params,
                        result_summary=value, status="ok", duration_ms=duration,
                    )
                )
                if transport == "mcp":
                    self.trajectory.add_mcp(
                        MCPExecutionRecord(
                            server_id=str(meta.get("server_id", "")),
                            tool=str(meta.get("tool", step.capability)),
                            status="ok", duration_ms=duration,
                            suspicious_output=bool(meta.get("suspicious")),
                        )
                    )
                try:
                    await self.dispatch_hook(
                        HookEvent.AFTER_TOOL_CALL,
                        {"run_id": self.run_id, "step_id": step.step_id,
                         "capability": step.capability, "status": "ok"},
                    )
                    if step.capability == "workspace.file.write":
                        await self.dispatch_hook(
                            HookEvent.AFTER_FILE_WRITE,
                            {"run_id": self.run_id, "step_id": step.step_id,
                             "workspace_id": params.get("workspace_id"), "path": params.get("path")},
                        )
                except HookValidationError as exc:
                    # The side effect already happened: the output is kept, the run fails.
                    self.outputs[step.key] = value
                    failure = await self._register_failure(
                        step, code=exc.code, message=str(exc), cause_type=type(exc).__name__,
                        attempts=attempt, source="hook", duration_ms=duration,
                    )
                    return await self._finalize_failure(step, failure)

                self._complete_step(step, value, duration=duration, attempt=attempt)
                await self.save_checkpoint(step_id=step.step_id)
                await self.emit_event(
                    "workflow.step.completed",
                    {"step_id": step.step_id, "capability": step.capability, "output_key": step.key,
                     "duration_ms": round(duration, 3), "attempt": attempt},
                )
                return "completed"

            if attempt < attempts and retryable:
                if side_effect in SIDE_EFFECT_STEPS and last_code in AMBIGUOUS_ERROR_CODES:
                    # The call was dispatched and its outcome is unknown: a retry could
                    # duplicate the side effect, so the run holds for recovery (FIX-02).
                    self.runtime.execution_ledger.mark_uncertain(
                        self.run_id, step.step_id, f"{last_code}: {last_message}",
                        idempotency_key=idempotency_key, scope_key=self.context.scope_hash(),
                    )
                    failure = await self._register_failure(
                        step, code=last_code, message=last_message, cause_type=last_cause,
                        attempts=attempt, source="adapter", duration_ms=duration,
                    )
                    return await self._enter_recovery(step, None, reason=last_code, failure=failure)
                self.trajectory.add_retry(
                    RetryRecord(
                        step_id=step.step_id, capability=step.capability, attempt=attempt,
                        error_code=last_code, error=last_message, timeout=last_code == "tool_timeout",
                    )
                )
                continue
            failure = await self._register_failure(
                step, code=last_code, message=last_message, cause_type=last_cause,
                attempts=attempt, source="adapter", duration_ms=duration,
            )
            if side_effect in SIDE_EFFECT_STEPS:
                if last_code in AMBIGUOUS_ERROR_CODES:
                    self.runtime.execution_ledger.mark_uncertain(
                        self.run_id, step.step_id, f"{last_code}: {last_message}",
                        idempotency_key=idempotency_key, scope_key=self.context.scope_hash(),
                    )
                    return await self._enter_recovery(step, None, reason=last_code, failure=failure)
                self.runtime.execution_ledger.fail(
                    self.run_id, step.step_id, f"{last_code}: {last_message}",
                    idempotency_key=idempotency_key, scope_key=self.context.scope_hash(),
                )
            return await self._finalize_failure(step, failure)

        raise WorkflowRuntimeError("unreachable_step_loop", step.step_id)  # pragma: no cover

    # -- step plumbing ----------------------------------------------------------------
    def _resolve_params(self, step: PlanStep) -> Dict[str, Any]:
        resolver = BindingResolver(
            request={"query": self.request.query},
            context=self.request.context,
            scope=self.context.scope_map(),
            outputs=self.outputs,
            step_outputs={sid: self.outputs.get(key) for sid, key in self._step_output_keys().items()},
            rules=self.rules_effective,
        )
        return resolve_bindings(step.input_bindings, resolver)

    def _step_output_keys(self) -> Dict[str, str]:
        assert self.plan is not None
        return {step.step_id: step.key for step in self.plan.steps}

    def _condition_holds(self, condition: str) -> bool:
        resolver = BindingResolver(
            request={"query": self.request.query}, context=self.request.context,
            scope=self.context.scope_map(), outputs=self.outputs, rules=self.rules_effective,
        )
        try:
            match = _COMPARISON_RE.match(condition)
            if not match:
                return bool(resolver.resolve(condition.strip()))
            left_ref, operator, right_raw = match.groups()
            left = resolver.resolve(left_ref)
            right = resolver.resolve(right_raw) if right_raw.startswith("$") else _coerce(right_raw)
            if operator == "==":
                return left == right
            if operator == "!=":
                return left != right
            left_num, right_num = float(left), float(right)
            return {
                ">": left_num > right_num,
                ">=": left_num >= right_num,
                "<": left_num < right_num,
                "<=": left_num <= right_num,
            }[operator]
        except Exception:  # noqa: BLE001 - an unevaluable condition never runs the step
            self.trajectory.metadata.setdefault("condition_errors", []).append(condition)
            return False

    def _needs_approval(self, step: PlanStep) -> bool:
        if step.approval_required:
            return True
        spec = self._spec_for(step)
        return bool(getattr(spec, "approval_required", False))

    def _side_effect_of(self, step: PlanStep) -> str:
        """Classify a step's side effect from the registry (never from the plan body)."""
        spec = self._spec_for(step)
        if spec is not None:
            return str(getattr(spec, "side_effect", "write") or "write")
        if step.capability.startswith("mcp."):
            return "external"
        if step.capability in ("workspace.rag.search", "office.xlsx.analyze", "office.docx.compare"):
            return "read"
        if step.capability == "office.pptx.generate":
            return "write"
        if step.capability == "workspace.file.write":
            return "write"
        # Unknown capability: assume it changes something and claim it.
        return "write"

    @staticmethod
    def _approval_target(step: PlanStep, params: Mapping[str, Any]) -> Optional[str]:
        for name in _APPROVAL_TARGET_PARAMS:
            value = params.get(name)
            if value not in (None, ""):
                return f"{name}:{value}"[:200]
        return None

    async def _enter_recovery(
        self,
        step: PlanStep,
        record: Optional[StepLedgerRecord],
        *,
        reason: str = "",
        failure: Optional[FailureRecord] = None,
    ) -> str:
        """Hold the run: the previous attempt may have happened, so nothing is retried."""
        self.status = "recovery_required"
        self.recovery = {
            "step_id": step.step_id,
            "capability": step.capability,
            "reason": reason or "unknown_outcome",
            "ledger_status": getattr(record, "status", "uncertain") if record is not None else "uncertain",
            "failure_code": (failure.code if failure is not None else reason),
            "attempts": (failure.attempts if failure is not None else 0),
        }
        self.trajectory.metadata["recovery"] = dict(self.recovery)
        self.error = {
            "code": "recovery_required",
            "message": f"step {step.step_id} ({step.capability}) outcome is unknown: {reason}",
            "step_id": step.step_id,
            "capability": step.capability,
        }
        await self.save_checkpoint(status="recovery_required", step_id=step.step_id)
        return "recovery_required"

    def _spec_for(self, step: PlanStep) -> Any:
        if self.registry is None:
            return None
        for name in (step.tool_name, step.capability):
            if not name:
                continue
            spec = self.registry.get(name)
            if spec is not None:
                return spec
        return None

    async def _invoke_capability(self, step: PlanStep, params: Mapping[str, Any]) -> tuple:
        capability = step.capability
        if capability not in self.request.capabilities:
            raise WorkflowRuntimeError("capability_not_granted", capability)
        workspace_id = params.get("workspace_id")
        if workspace_id is not None:
            if self.context.workspace_id is None or str(workspace_id) != str(self.context.workspace_id):
                raise ScopeViolation("scope_mismatch", fields=["workspace_id"])
        if step.skill_name and step.skill_name in self.skill_grants and self.skills is not None:
            self.skills.enforce_tool_call(self.skill_grants[step.skill_name], capability)

        office = self.office
        if office is not None and capability in office.capabilities:
            call_scope = AdapterCallScope.from_context(self.context)
            self.budget.guard_tool_call()
            result = await office.invoke(capability, dict(params), call_scope=call_scope)
            if isinstance(result, OfficeResult) and not result.ok:
                raise OfficeAdapterError("capability_failed", str(result.data)[:200])
            return (result.data if isinstance(result, OfficeResult) else result), "office", {}

        if capability.startswith("mcp."):
            parts = capability.split(".")
            if len(parts) < 3 or self.runtime.mcp is None:
                raise WorkflowRuntimeError("mcp_unavailable", capability)
            server_id, tool_name = parts[1], ".".join(parts[2:])
            await self.runtime.ensure_mcp_tool(server_id)
            self.budget.guard_tool_call()
            result = await self.runtime.mcp.call_tool(
                server_id, tool_name, dict(params),
                user_approved=bool(self.granted_approvals.get(step.step_id)),
                context=self.context,
            )
            return result.content, "mcp", {
                "server_id": server_id, "tool": tool_name, "suspicious": result.suspicious,
            }

        spec = self._spec_for(step)
        if spec is None or self.registry is None:
            raise WorkflowRuntimeError("unknown_capability", capability)
        # FIX-01: the AGCore tool policy decides whether *this service* may run the tool.
        if self.runtime.tool_policy is not None:
            self.runtime.tool_policy.authorize(spec, self.context)
        self.budget.guard_tool_call()
        granted = "read" if getattr(spec, "is_read_only", False) else "write"
        value = await self.registry.execute(
            spec.qualified_name, dict(params), context=self.context, granted_permission=granted,
        )
        if isinstance(value, MCPToolResult):
            return value.content, "mcp", {"server_id": "", "tool": spec.qualified_name}
        return value, "registry", {}

    def _complete_step(self, step: PlanStep, value: Any, *, duration: float, attempt: int) -> None:
        self.outputs[step.key] = value
        if step.step_id not in self.completed_steps:
            self.completed_steps.append(step.step_id)
        idempotency_key = f"{self.run_id}:{step.step_id}"
        try:
            if self.ledger.lookup(step.step_id, idempotency_key, context=self.context) is None:
                self.ledger.begin(step.step_id, idempotency_key, context=self.context)
            self.ledger.complete(step.step_id, idempotency_key, value, context=self.context)
        except IdempotencyConflict:
            pass
        if self._side_effect_of(step) in SIDE_EFFECT_STEPS:
            self.runtime.execution_ledger.succeed(
                self.run_id, step.step_id, value,
                idempotency_key=idempotency_key, scope_key=self.context.scope_hash(),
            )
        self.trajectory.close_step(step.step_id, status="completed", duration_ms=duration, attempt=attempt)

    async def _pause_for_approval(self, step: PlanStep, params: Optional[Mapping[str, Any]] = None) -> str:
        self.pending_approval_id = f"apreq_{self.run_id}_{step.step_id}"
        self.status = "awaiting_approval"
        args_hash = approval_args_hash(dict(params or {}))
        target = self._approval_target(step, dict(params or {}))
        self.trajectory.metadata["pending_approval"] = {
            "step_id": step.step_id,
            "capability": step.capability,
            "approval_id": self.pending_approval_id,
            # FIX-06: what exactly the human is approving; the token is bound to these.
            "target": target,
            "args_hash": args_hash,
        }
        await self.dispatch_hook(
            HookEvent.ON_APPROVAL_REQUIRED,
            {"run_id": self.run_id, "step_id": step.step_id,
             "capability": step.capability, "approval_id": self.pending_approval_id},
            fail_open_on_error=True,
        )
        await self.save_checkpoint(status="awaiting_approval", step_id=step.step_id)
        await self.emit_event(
            "workflow.approval.required",
            {"step_id": step.step_id, "capability": step.capability,
             "approval_id": self.pending_approval_id, "reason": "step_declared_approval_required"},
        )
        return "awaiting_approval"

    def _ensure_step_record(self, step: PlanStep, *, code: str, message: str, attempts: int) -> None:
        for record in reversed(self.trajectory.step_records):
            if record.step_id == step.step_id and record.status == "pending":
                return
        self.trajectory.add_step(
            StepRecord(
                step_id=step.step_id, name=step.name, capability=step.capability,
                status="failed", started_at=_utcnow(), ended_at=_utcnow(),
                output_key=step.key, attempt=attempts, error_code=code, error=message[:500],
            )
        )

    async def _register_failure(
        self,
        step: PlanStep,
        *,
        code: str,
        message: str,
        cause_type: str,
        attempts: int,
        source: str,
        duration_ms: float = 0.0,
    ) -> FailureRecord:
        record = FailureRecord(
            step_id=step.step_id, capability=step.capability, code=code,
            message=message[:1_000], attempts=attempts, source=source, cause_type=cause_type or None,
        )
        self.trajectory.add_failure(record)
        self._ensure_step_record(step, code=code, message=message, attempts=attempts)
        self.trajectory.close_step(
            step.step_id, status="failed", duration_ms=duration_ms,
            error_code=code, error=record.message, attempt=attempts,
        )
        self.failures.append(record.model_dump(mode="json"))
        await self.dispatch_hook(
            HookEvent.ON_ERROR,
            {"run_id": self.run_id, "step_id": step.step_id, "capability": step.capability,
             "code": code, "message": record.message},
            fail_open_on_error=True,
        )
        return record

    async def _finalize_failure(self, step: PlanStep, failure: FailureRecord) -> str:
        assert self.plan is not None
        replannable = (
            self.plan.replan_on_failure
            and failure.source in ("adapter", "runtime")
            and failure.code not in ("capability_not_granted", "unknown_capability")
            and self.replans < self.runtime.max_replans
        )
        if replannable:
            try:
                new_plan = self.planner.replan(
                    self.plan,
                    failed_step_id=step.step_id,
                    error_code=failure.code,
                    completed_steps=self.completed_steps,
                    replans_used=self.replans,
                )
            except PlannerError as exc:
                self.trajectory.metadata["replan_error"] = {"code": exc.code, "detail": exc.detail}
            else:
                self.replans += 1
                self.trajectory.add_replan(
                    ReplanRecord(
                        from_plan_id=self.plan.plan_id, to_plan_id=new_plan.plan_id,
                        version=new_plan.version, failed_step_id=step.step_id,
                        error_code=failure.code, drop_completed=list(self.completed_steps),
                        note="; ".join(new_plan.notes),
                    )
                )
                self.failures[-1]["replanned"] = True
                self.trajectory.failures[-1] = self.trajectory.failures[-1].model_copy(
                    update={"replanned": True}
                )
                self.plan = new_plan
                self.trajectory.set_plan(new_plan.summary())
                await self.save_checkpoint()
                await self.emit_event("workflow.plan.created", {"plan": new_plan.summary(), "replan": True})
                return "replanned"

        self.status = "failed"
        self.error = {
            "code": failure.code, "message": failure.message, "step_id": failure.step_id,
            "capability": failure.capability, "attempts": failure.attempts, "source": failure.source,
        }
        await self.save_checkpoint(status="failed", step_id=step.step_id)
        return "failed"

    async def _fail_phase(self, phase: str, code: str, message: str) -> WorkflowGraphState:
        record = FailureRecord(code=code, message=message[:1_000], source=phase)
        self.trajectory.add_failure(record)
        self.failures.append(record.model_dump(mode="json"))
        self.status = "failed"
        self.error = {"code": code, "message": message, "phase": phase}
        await self.save_checkpoint(status="failed")
        return {"stage": phase, "status": "failed"}

    async def _cancel_run(self) -> str:
        self.status = "cancelled"
        self.error = {"code": "workflow_cancelled", "message": "run cancelled by caller"}
        await self.save_checkpoint(status="cancelled")
        return "cancelled"

    # -- phase 6..8 -------------------------------------------------------------------
    async def validate_hooks(self, state: WorkflowGraphState) -> WorkflowGraphState:
        try:
            await self.dispatch_hook(
                HookEvent.WORKFLOW_COMPLETED,
                {"run_id": self.run_id, "plan_id": self.plan.plan_id if self.plan else None,
                 "completed_steps": list(self.completed_steps)},
            )
        except HookValidationError as exc:
            record = FailureRecord(code=exc.code, message=str(exc), source="hook")
            self.trajectory.add_failure(record)
            self.failures.append(record.model_dump(mode="json"))
            self.status = "failed"
            self.error = {"code": exc.code, "message": str(exc), "phase": "validate_hooks"}
            await self.save_checkpoint(status="failed")
            return {"stage": "validate_hooks", "status": "failed"}
        return {"stage": "validate_hooks", "status": self.status}

    async def store_artifacts(self, state: WorkflowGraphState) -> WorkflowGraphState:
        assert self.plan is not None
        for step in self.plan.steps:
            value = self.outputs.get(step.key)
            if not isinstance(value, Mapping):
                continue
            if step.capability == "workspace.file.write" and value.get("path"):
                self.artifacts.append(
                    ArtifactRecord(
                        artifact_id=str(value.get("sha256") or value.get("path")),
                        kind="file", path=str(value.get("path")), sha256=value.get("sha256"),
                        bytes=value.get("bytes"), produced_by_step=step.step_id,
                    )
                )
            elif value.get("artifact_id"):
                self.artifacts.append(
                    ArtifactRecord(
                        artifact_id=str(value.get("artifact_id")),
                        kind=step.capability.split(".")[1] if "." in step.capability else "artifact",
                        path=value.get("path"), sha256=value.get("checksum"),
                        bytes=value.get("estimated_bytes"), produced_by_step=step.step_id,
                    )
                )
        for artifact in self.artifacts:
            self.trajectory.add_artifact(artifact)
        return {"stage": "store_artifacts", "status": self.status}

    async def report(self, state: WorkflowGraphState) -> WorkflowGraphState:
        status = self.status
        if status == "awaiting_approval":
            self.trajectory.metadata["status_reason"] = "awaiting_human_approval"
        elif status == "recovery_required":
            self.trajectory.metadata["status_reason"] = "previous_attempt_outcome_unknown"
        await self.dispatch_hook(
            HookEvent.SESSION_END,
            {"run_id": self.run_id, "status": status, "completed_steps": list(self.completed_steps)},
            fail_open_on_error=True,
        )
        self.trajectory.finish(status=status, latency_ms=self.elapsed_ms())
        await self.save_checkpoint(status="completed" if status == "direct_chat" else status)
        if self.status == "cancelled" and status != "cancelled":
            # A cancellation landed while this task was finishing; cancel wins (FIX-02).
            status = "cancelled"
            self.error = self.error or {"code": "workflow_cancelled", "message": "run cancelled by caller"}
        if status == "completed":
            await self.emit_event(
                "workflow.completed",
                {"steps": len(self.completed_steps), "artifacts": len(self.artifacts),
                 "duration_ms": round(self.elapsed_ms(), 3)},
            )
        elif status == "failed":
            await self.emit_event("workflow.failed", {"error": self.error or {}})
        elif status == "cancelled":
            await self.emit_event("workflow.cancelled", {"reason": "cancelled_by_caller"})
        return {"stage": "report", "status": status}

    # -- result -----------------------------------------------------------------------
    def build_result(self) -> WorkflowRunResult:
        steps = [
            {
                "step_id": step.step_id,
                "name": step.name,
                "capability": step.capability,
                "status": "completed" if step.step_id in self.completed_steps
                else ("skipped" if step.step_id in self.skipped_steps else "pending"),
                "output_key": step.key,
            }
            for step in (self.plan.steps if self.plan else ())
        ]
        pending = None
        if self.status == "awaiting_approval" and self.pending_approval_id:
            pending_info = self.trajectory.metadata.get("pending_approval") or {}
            pending = {
                "approval_id": self.pending_approval_id,
                "step_id": pending_info.get("step_id"),
                "capability": pending_info.get("capability"),
            }
        return WorkflowRunResult(
            run_id=self.run_id,
            status=self.status,
            route=self.route,
            reason=self.reason,
            plan_id=self.plan.plan_id if self.plan else None,
            plan_version=self.plan.version if self.plan else 1,
            steps=steps,
            answer={
                "status": self.status,
                "route": self.route,
                "goal": self.plan.goal if self.plan else None,
                "artifacts": [artifact.model_dump(mode="json") for artifact in self.artifacts],
            },
            artifacts=[artifact.model_dump(mode="json") for artifact in self.artifacts],
            failures=list(self.failures),
            pending_approval=pending,
            recovery=dict(self.recovery) if self.recovery else None,
            error=self.error,
            token_usage=self.trajectory.token_usage.model_dump(),
            duration_ms=round(self.elapsed_ms(), 3),
            trajectory=self.trajectory.to_dict(),
        )


# --------------------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------------------
class WorkflowRuntime:
    """Executes plans; owns the LangGraph graph, the checkpoint store and the gates."""

    def __init__(
        self,
        *,
        registry: Any = None,
        skills: Any = None,
        rules: Any = None,
        hooks: Any = None,
        mcp: Any = None,
        office: Optional[OfficeCapabilityAdapter] = None,
        store: Optional[WorkflowCheckpointStore] = None,
        approvals: Optional[ApprovalAuthority] = None,
        ledger: Optional[IdempotencyLedger] = None,
        execution_ledger: Optional[ExecutionLedger] = None,
        run_guard: Optional[RunExecutionGuard] = None,
        tool_policy: Optional[ToolAccessPolicy] = None,
        planner: Optional[Planner] = None,
        max_steps: int = 10,
        max_retries: int = 2,
        max_replans: int = 1,
        max_tool_calls: int = 24,
        max_context_tokens: int = 12_000,
        workspace_root: Optional[str] = None,
    ) -> None:
        self.registry = registry
        self.skills = skills
        self.rules = rules
        self.hooks = hooks
        self.mcp = mcp
        self.office = office
        self.store = store if store is not None else WorkflowCheckpointStore(backend="memory")
        # ``or`` would drop a valid but empty ledger/store because both define __len__.
        self.ledger = ledger if ledger is not None else IdempotencyLedger()
        # FIX-02: durable, atomically claimed execution records for side-effecting steps.
        self.execution_ledger = execution_ledger if execution_ledger is not None else ExecutionLedger()
        self.run_guard = run_guard if run_guard is not None else RunExecutionGuard()
        # FIX-01: optional AGCore tool policy gate for registry-backed capabilities.
        self.tool_policy = tool_policy
        self.approvals = approvals if approvals is not None else ApprovalAuthority(uuid.uuid4().hex)
        self.planner = planner or Planner(
            skills=skills, registry=registry, rules=rules,
            max_steps=max_steps, max_retries=max_retries, max_replans=max_replans,
            max_estimated_tokens=max_context_tokens,
        )
        self.max_steps = max(1, int(max_steps))
        self.max_retries = max(0, int(max_retries))
        self.max_replans = max(0, int(max_replans))
        self.max_tool_calls = max(0, int(max_tool_calls))
        self.max_context_tokens = max(1, int(max_context_tokens))
        self.workspace_root = workspace_root
        self.graph_runs = 0
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._sinks: Dict[str, Optional[Callable[[Dict[str, Any]], Any]]] = {}

    # -- helpers ----------------------------------------------------------------------
    def new_budget(self, request: WorkflowRunRequest) -> ContextBudgetManager:
        """One budget per run: one workflow can never exhaust another's allowance."""
        budget = dict(request.budget or {})
        return ContextBudgetManager(
            max_context_tokens=int(budget.get("max_context_tokens", self.max_context_tokens)),
            max_step_budget=int(request.max_steps or budget.get("max_steps") or self.max_steps),
            max_tool_calls=int(request.max_tool_calls or budget.get("max_tool_calls") or self.max_tool_calls),
        )

    def build_rules_engine(self, request: WorkflowRunRequest) -> Any:
        from app.workflow.rules import RuleLayer, RulesEngine

        root = request.context.get("workspace_root") or self.workspace_root
        engine = RulesEngine(workspace_root=root)
        raw_rules = request.context.get("rules") or ()
        if not isinstance(raw_rules, (list, tuple)):
            raise WorkflowRuntimeError("invalid_rules", "rules must be a list", http_status=400)
        for raw in raw_rules:
            if not isinstance(raw, Mapping):
                raise WorkflowRuntimeError("invalid_rules", "rule must be an object", http_status=400)
            key = str(raw.get("key") or "").strip()
            if not key:
                raise WorkflowRuntimeError("invalid_rules", "rule key required", http_status=400)
            try:
                layer = RuleLayer(str(raw.get("layer") or "workspace"))
            except ValueError as exc:
                raise WorkflowRuntimeError("invalid_rule_layer", str(raw.get("layer")), http_status=400) from exc
            engine.add(
                layer, key, raw.get("value"),
                category=str(raw.get("category") or "general"),
                rule_id=raw.get("rule_id"),
                priority=int(raw.get("priority") or 0),
            )
        return engine

    def is_cancelled(self, run_id: str) -> bool:
        event = self._cancel_events.get(run_id)
        return bool(event and event.is_set())

    @staticmethod
    def classify_error(exc: BaseException) -> tuple:
        """Map an adapter/tool exception to ``(code, message, retryable, cause_type)``."""
        from app.workflow.registry import ToolNotFound

        if isinstance(exc, OfficeAdapterError):
            code = exc.code or "office_adapter_error"
            return code, str(exc), Planner.is_retryable(code), type(exc).__name__
        if isinstance(exc, (MCPRegistrationError, MCPConfigError)):
            return "mcp_server_unavailable", str(exc), False, type(exc).__name__
        if isinstance(exc, MCPToolDenied):
            return "mcp_tool_denied", str(exc), False, type(exc).__name__
        if isinstance(exc, MCPApprovalRequired):
            return "mcp_user_approval_required", str(exc), False, type(exc).__name__
        if isinstance(exc, MCPUnavailable):
            return exc.code, str(exc), True, type(exc).__name__
        if isinstance(exc, MCPError):
            return "mcp_error", str(exc), True, type(exc).__name__
        if isinstance(exc, ScopeViolation):
            return exc.code, str(exc), False, type(exc).__name__
        if isinstance(exc, ApprovalError):
            return exc.code, str(exc), False, type(exc).__name__
        if isinstance(exc, HookValidationError):
            return exc.code, str(exc), False, type(exc).__name__
        if isinstance(exc, BindingResolutionError):
            return exc.code, str(exc), False, type(exc).__name__
        if isinstance(exc, PlannerError):
            return exc.code, str(exc), False, type(exc).__name__
        if isinstance(exc, WorkflowRuntimeError):
            return exc.code, str(exc), False, type(exc).__name__
        if isinstance(exc, BudgetExceeded):
            return exc.code, str(exc), False, type(exc).__name__
        if isinstance(exc, ToolNotFound):
            return "tool_not_found", str(exc), False, type(exc).__name__
        if isinstance(exc, PermissionError):
            return "permission_denied", str(exc), False, type(exc).__name__
        return "tool_error", str(exc), True, type(exc).__name__

    async def ensure_mcp_tool(self, server_id: str) -> None:
        """Connect + discover an MCP server once when its tools are not registered yet."""
        if self.mcp is None:
            raise WorkflowRuntimeError("mcp_unavailable", server_id)
        prefix = f"mcp.{server_id}."
        registered = (
            any(spec.qualified_name.startswith(prefix) for spec in self.registry.list_specs())
            if self.registry is not None else False
        )
        if registered:
            return
        await self.mcp.connect(server_id)
        await self.mcp.discover_tools(server_id)

    def _validate_context(self, context: ExecutionContext) -> None:
        if not isinstance(context, ExecutionContext):
            raise WorkflowRuntimeError("invalid_execution_context", type(context).__name__, http_status=400)

    def _new_run_id(self, requested: Optional[str]) -> str:
        if requested:
            run_id = requested.strip()
            if not run_id or len(run_id) > 128:
                raise WorkflowRuntimeError("invalid_run_id", requested[:64], http_status=400)
            if self.store.exists(run_id):
                raise WorkflowRuntimeError("run_id_conflict", run_id, http_status=409)
            return run_id
        return f"wfrun_{uuid.uuid4().hex[:20]}"

    def _register_sink(self, run_id: str, emit: Optional[Callable[[Dict[str, Any]], Any]]) -> None:
        self._sinks[run_id] = emit

    async def _notify(self, run_id: str, event: str, data: Mapping[str, Any]) -> None:
        sink = self._sinks.get(run_id)
        if sink is None:
            return
        try:
            result = sink({"run_id": run_id, "event": event, "status": "cancelled", **dict(data)})
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            pass

    # -- lifecycle --------------------------------------------------------------------
    async def start(
        self,
        request: WorkflowRunRequest,
        context: ExecutionContext,
        *,
        emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
        run_id: Optional[str] = None,
    ) -> WorkflowRunResult:
        self._validate_context(context)
        resolved_run_id = self._new_run_id(run_id or request.run_id)
        lease = self._acquire_lease(resolved_run_id)
        self._cancel_events[resolved_run_id] = asyncio.Event()
        self._register_sink(resolved_run_id, emit)
        rc = _RunContext(self, run_id=resolved_run_id, request=request, context=context, emit=emit)
        try:
            return await self._execute(rc, entry="context_resolve")
        finally:
            self._sinks.pop(resolved_run_id, None)
            self._release_lease(resolved_run_id, lease)

    async def resume(
        self,
        run_id: str,
        context: ExecutionContext,
        *,
        approval: Optional[ApprovalToken | Mapping[str, Any]] = None,
        emit: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> WorkflowRunResult:
        self._validate_context(context)
        checkpoint = self.store.load(run_id, context=context)
        if checkpoint.status == "cancelled":
            raise WorkflowRuntimeError("workflow_run_cancelled", run_id)
        if checkpoint.status == "completed":
            return self._stored_result(checkpoint)
        if checkpoint.status == "recovery_required":
            # FIX-02: an unknown-outcome side effect is never replayed automatically.
            raise WorkflowRuntimeError(
                "recovery_required",
                f"{run_id}: resolve the uncertain step before resuming",
            )

        if not (checkpoint.plan or {}).get("plan_id"):
            raise WorkflowRuntimeError("workflow_not_planned", run_id)
        plan = ExecutionPlan.model_validate(checkpoint.plan or {})
        granted: Dict[str, bool] = {}
        if checkpoint.pending_approval_id:
            pending_step = self._pending_step_id(checkpoint, plan)
            pending = (checkpoint.metadata or {}).get("pending_approval") or {}
            # FIX-06: the token must match the run, the step, the capability *and* the
            # exact arguments the pause recorded - not just "some valid approval".
            expected = ApprovalBinding(
                run_id=run_id,
                step_id=pending.get("step_id") or pending_step,
                capability=pending.get("capability"),
                target=pending.get("target"),
                args_hash=pending.get("args_hash"),
            )
            self.approvals.verify_user_approval(context, approval, expected=expected)
            token_approval_id = str(
                getattr(approval, "approval_id", None)
                or (approval.get("approval_id") if isinstance(approval, Mapping) else "")
            )
            if token_approval_id and token_approval_id != checkpoint.pending_approval_id:
                raise ApprovalError(
                    "approval_mismatch", f"{token_approval_id} != {checkpoint.pending_approval_id}"
                )
            if pending_step:
                granted[pending_step] = True

        lease = self._acquire_lease(run_id)
        self._cancel_events[run_id] = asyncio.Event()
        self._register_sink(run_id, emit)
        trajectory = WorkflowTrajectory.restore(self.store.load_trajectory(run_id), run_id=run_id)
        rc = _RunContext(
            self, run_id=run_id, request=self._request_from_checkpoint(checkpoint), context=context,
            plan=plan, completed_steps=checkpoint.completed_steps, outputs=checkpoint.step_outputs,
            replans=checkpoint.replans, granted_approvals=granted, trajectory=trajectory, emit=emit,
            entry="execute_dag",
        )
        rc.pending_approval_id = None
        rc.status = "running"
        rc.route = str((checkpoint.metadata or {}).get("route") or "workflow")
        try:
            await rc.save_checkpoint(status="running")
            return await self._execute(rc, entry="execute_dag")
        finally:
            self._sinks.pop(run_id, None)
            self._release_lease(run_id, lease)

    # -- FIX-02 leases ------------------------------------------------------------------
    def _acquire_lease(self, run_id: str) -> str:
        try:
            token = self.run_guard.acquire(run_id)
        except RunBusy as exc:
            raise WorkflowRuntimeError("run_in_progress", str(exc)) from exc
        claim = getattr(self.store, "claim_run", None)
        if claim is not None:
            try:
                claimed = bool(claim(run_id, holder=token))
            except Exception:  # noqa: BLE001 - a store without leases must not block the run
                claimed = True
            if not claimed:
                self.run_guard.release(run_id, token)
                raise WorkflowRuntimeError("run_in_progress", f"{run_id}: lease held by another executor")
        return token

    def _release_lease(self, run_id: str, token: Optional[str]) -> None:
        if not token:
            return
        release = getattr(self.store, "release_run", None)
        if release is not None:
            try:
                release(run_id, token)
            except Exception:  # noqa: BLE001
                pass
        self.run_guard.release(run_id, token)

    async def approve(
        self,
        run_id: str,
        context: ExecutionContext,
        *,
        approved_by: Optional[str] = None,
        ttl_seconds: int = 300,
    ) -> ApprovalToken:
        checkpoint = self.store.load(run_id, context=context)
        if not checkpoint.pending_approval_id:
            raise WorkflowRuntimeError("approval_not_required", run_id)
        if checkpoint.status in ("completed", "cancelled", "failed"):
            raise WorkflowRuntimeError("workflow_run_terminal", f"{run_id}:{checkpoint.status}")
        pending = (checkpoint.metadata or {}).get("pending_approval") or {}
        pending_step_id = pending.get("step_id")
        if not pending_step_id and checkpoint.plan:
            try:
                pending_step_id = self._pending_step_id(
                    checkpoint, ExecutionPlan.model_validate(checkpoint.plan)
                )
            except Exception:  # noqa: BLE001 - a corrupt plan must not block the approval
                pending_step_id = None
        # FIX-06: the approver is the authenticated owner, and the token is bound to the
        # exact run/step/capability/arguments recorded when the pause was written.
        return self.approvals.issue_user_approval(
            context,
            approved_by=(approved_by or context.owner_id),
            ttl_seconds=ttl_seconds,
            approval_id=checkpoint.pending_approval_id,
            run_id=run_id,
            step_id=pending_step_id,
            capability=pending.get("capability"),
            target=pending.get("target"),
            args_hash=pending.get("args_hash"),
        )

    async def resolve_recovery(
        self,
        run_id: str,
        context: ExecutionContext,
        *,
        action: str,
        resolved_by: Optional[str] = None,
    ) -> WorkflowCheckpoint:
        """Explicitly leave a ``recovery_required`` hold (FIX-02).

        ``retry``     — the operator confirmed the side effect did not happen; the ledger
                        record is released so the next resume may claim it again.
        ``abandon``   — the operator accepts the ambiguity; the run is failed and stays
                        failed (no side effect is repeated).
        """
        checkpoint = self.store.load(run_id, context=context)
        if checkpoint.status != "recovery_required":
            raise WorkflowRuntimeError("workflow_not_in_recovery", f"{run_id}:{checkpoint.status}")
        recovery = (checkpoint.metadata or {}).get("recovery") or {}
        step_id = recovery.get("step_id")
        if action not in ("retry", "abandon"):
            raise WorkflowRuntimeError("invalid_recovery_action", action, http_status=400)

        if action == "retry":
            if step_id:
                record = self.execution_ledger.lookup(
                    run_id, step_id, idempotency_key=f"{run_id}:{step_id}",
                    scope_key=context.scope_hash(),
                )
                if record is not None and record.status == "uncertain":
                    # Turn the ambiguous record into a recorded failure: the *operator*
                    # decided nothing happened, so a retry may claim it again.
                    self.execution_ledger.fail(
                        run_id, step_id, "retry_confirmed_by_operator",
                        idempotency_key=f"{run_id}:{step_id}", scope_key=context.scope_hash(),
                    )
            updated = self.store.compare_and_set_status(
                run_id, expected=("recovery_required",), status="awaiting_approval" if checkpoint.pending_approval_id else "running",
                metadata={"recovery": None, "recovery_resolved_by": resolved_by or context.owner_id,
                          "recovery_action": action},
                context=context,
            )
            if updated is None:
                raise WorkflowRuntimeError("recovery_resolution_conflict", run_id)
            return updated

        updated = self.store.compare_and_set_status(
            run_id, expected=("recovery_required",), status="failed",
            clear_pending_approval=True,
            metadata={"recovery_action": action, "recovery_resolved_by": resolved_by or context.owner_id},
            context=context,
        )
        if updated is None:
            raise WorkflowRuntimeError("recovery_resolution_conflict", run_id)
        return updated

    async def cancel(self, run_id: str, context: ExecutionContext) -> WorkflowCheckpoint:
        checkpoint = self.store.load(run_id, context=context)
        if checkpoint.status == "completed":
            raise WorkflowRuntimeError("workflow_run_completed", run_id)
        if checkpoint.status == "cancelled":
            return checkpoint
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        # FIX-02: cancellation is an atomic compare-and-set, so a completion that lands in
        # the same instant cannot overwrite it (and vice versa: cancel cannot overwrite an
        # already completed run, which is refused above).
        updated = self.store.compare_and_set_status(
            run_id,
            expected=("pending", "running", "awaiting_approval", "recovery_required"),
            status="cancelled", clear_pending_approval=True, context=context,
        )
        if updated is None:
            current = self.store.load(run_id, context=context)
            if current.status == "completed":
                raise WorkflowRuntimeError("workflow_run_completed", run_id)
            if current.status == "cancelled":
                return current
            raise WorkflowRuntimeError("workflow_cancel_conflict", f"{run_id}:{current.status}")
        trajectory = WorkflowTrajectory.restore(self.store.load_trajectory(run_id), run_id=run_id)
        trajectory.finish(status="cancelled", latency_ms=trajectory.execution_latency_ms)
        self.store.save(updated, trajectory=trajectory.to_dict())
        await self._notify(run_id, "workflow.cancelled", {"reason": "cancelled_by_caller"})
        return updated

    def get(self, run_id: str, *, context: Optional[ExecutionContext] = None) -> Dict[str, Any]:
        checkpoint = self.store.load(run_id, context=context)
        return {
            "checkpoint": checkpoint.public_view(),
            "trajectory": self.store.load_trajectory(run_id) or {},
            "steps": [
                {"step_id": step.get("step_id"), "capability": step.get("capability"),
                 "output_key": step.get("output_key"),
                 "status": "completed" if step.get("step_id") in checkpoint.completed_steps else "pending"}
                for step in (checkpoint.plan or {}).get("steps") or ()
            ],
        }

    # -- execution --------------------------------------------------------------------
    async def _execute(self, rc: _RunContext, *, entry: str) -> WorkflowRunResult:
        self.graph_runs += 1
        compiled = get_compiled_workflow_graph()
        init: WorkflowGraphState = {
            "run_id": rc.run_id, "entry": entry, "stage": "init",
            "status": rc.status, "route": rc.route,
        }
        try:
            await compiled.ainvoke(init, config={"configurable": {"run_ctx": rc}, "recursion_limit": 50})
        except asyncio.CancelledError:
            rc.status = "cancelled"
            await rc.save_checkpoint(status="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw stack trace as the result
            code, message, _retryable, cause = self.classify_error(exc)
            rc.status = "failed"
            rc.error = {"code": code, "message": message, "cause_type": cause}
            rc.trajectory.add_failure(
                FailureRecord(code=code, message=message[:1_000], source="runtime", cause_type=cause)
            )
            rc.trajectory.finish(status="failed", latency_ms=rc.elapsed_ms())
            await rc.save_checkpoint(status="failed")
            await rc.emit_event("workflow.failed", {"error": rc.error})
        return rc.build_result()

    def _pending_step_id(self, checkpoint: WorkflowCheckpoint, plan: ExecutionPlan) -> Optional[str]:
        pending = checkpoint.pending_approval_id or ""
        for step in plan.steps:
            if pending.endswith(f"_{step.step_id}"):
                return step.step_id
        return None

    def _request_from_checkpoint(self, checkpoint: WorkflowCheckpoint) -> WorkflowRunRequest:
        payload = dict((checkpoint.metadata or {}).get("request") or {})
        plan = checkpoint.plan or {}
        if not payload:
            payload = {
                "query": plan.get("goal") or "",
                "capabilities": (checkpoint.metadata or {}).get("capabilities") or [],
            }
        payload.pop("plan_steps", None)
        payload["run_id"] = None
        return WorkflowRunRequest(**payload)

    def _stored_result(self, checkpoint: WorkflowCheckpoint) -> WorkflowRunResult:
        trajectory = WorkflowTrajectory.restore(
            self.store.load_trajectory(checkpoint.run_id), run_id=checkpoint.run_id
        )
        plan_payload = checkpoint.plan or {}
        plan = ExecutionPlan.model_validate(plan_payload) if plan_payload.get("plan_id") else None
        return WorkflowRunResult(
            run_id=checkpoint.run_id,
            status=checkpoint.status,
            route=str((checkpoint.metadata or {}).get("route") or "workflow"),
            reason="already_completed",
            plan_id=plan.plan_id if plan else None,
            plan_version=plan.version if plan else 1,
            steps=[{"step_id": s.step_id, "capability": s.capability, "status": "completed"} for s in (plan.steps if plan else ())],
            answer={"status": checkpoint.status, "goal": plan.goal if plan else None},
            failures=list(checkpoint.failures),
            token_usage=trajectory.token_usage.model_dump(),
            trajectory=trajectory.to_dict(),
        )
