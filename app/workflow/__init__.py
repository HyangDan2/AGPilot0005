"""R3 Workflow Engines (WO-R3-PREDEV: WP-03, WP-04, WP-05, WP-06, WP-07, WP-08, WP-10).

The package is additive: R1.3/R2.1 modules (``app/runtime/langgraph_r1.py``,
``app/workspace/*``, ``app/canvas/*``) are untouched.  Each engine is importable on its
own, and :func:`build_workflow_stack` wires a default set for an acceptance run.

    safety      WP-10  execution scope, approvals, prompt-injection defence, idempotency
    registry    WP-07  unified tool catalogue (builtin / agtools / mcp / skill / downstream)
    rules       WP-04  four-layer rules resolution with protected-category invariants
    skills      WP-03  SKILL.md discovery, lazy loading and capability grants
    hooks       WP-05  eleven lifecycle events with fail-open/fail-closed policies
    mcp         WP-06  MCP client lifecycle, tool normalisation, credential isolation
    budget      WP-08  context-window allocation, compaction and execution limits
    office_adapter     R2.1/AGTools capability interface (Mock and Runtime)
    planner     WP-01  direct-chat fast path, execution plan DAG, conditional replanning
    checkpoint  WP-02  durable run state, scope re-verified resume
    trajectory  WP-09  structured, secret-masked run record
    runtime     WP-02  LangGraph execution graph (plan -> DAG -> hooks -> report)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.workflow.budget import (
    BudgetExceeded,
    BudgetPlan,
    CompactionResult,
    ContextBudgetManager,
    ContextKind,
    ContextSegment,
)
from app.workflow.hooks import (
    HOOK_EVENTS,
    FailurePolicy,
    HookDefinition,
    HookDispatchResult,
    HookEvent,
    HookExecutionRecord,
    HookRecursionError,
    HookRegistry,
    HookSecurityError,
    HookValidationError,
)
from app.workflow.mcp import (
    CredentialStore,
    MCPApprovalRequired,
    MCPClientManager,
    MCPConfigError,
    MCPDiscoveryReport,
    MCPError,
    MCPHealth,
    MCPPrompt,
    MCPRegistrationError,
    MCPResource,
    MCPServerConfig,
    MCPState,
    MCPToolDenied,
    MCPToolResult,
    MCPUnavailable,
    TOOL_POLICY_VALUES,
    default_client_factory,
    unavailable_client_factory,
)
from app.workflow.office_adapter import (
    MockOfficeAdapter,
    OFFICE_CAPABILITIES,
    OfficeAdapterError,
    OfficeAdapterUnavailable,
    OfficeCapabilityAdapter,
    OfficeResult,
    RuntimeOfficeAdapter,
    assert_adapter_allowed,
    get_office_adapter,
)
from app.workflow.registry import (
    DuplicateCapabilityError,
    ToolCollisionError,
    ToolNotFound,
    UnifiedToolRegistry,
    UnifiedToolSpec,
)
from app.workflow.rules import (
    PROTECTED_CATEGORIES,
    AppliedRuleRecord,
    RuleConflict,
    RuleDefinition,
    RuleLayer,
    RulesEngine,
    RulesResolution,
    load_workspace_rules,
)
from app.workflow.safety import (
    ApprovalAuthority,
    ApprovalBinding,
    ApprovalError,
    ApprovalRecord,
    ApprovalToken,
    ExecutionContext,
    IdempotencyConflict,
    IdempotencyLedger,
    IdempotencyRecord,
    PromptInjectionError,
    PromptInjectionGuard,
    ScopeViolation,
    approval_args_hash,
    assert_scope_access,
    classify_principal,
    ensure_human_approver,
)
from app.workflow.ledger import (
    ExecutionLedger,
    LedgerClaim,
    RunBusy,
    RunExecutionGuard,
    StepLedgerRecord,
)
from app.workflow.scope import (
    ExecutionScopeAuthority,
    R2WorkspaceResolver,
    ScopeEvidence,
    ToolAccessPolicy,
    WorkspaceToolPolicy,
)
from app.workflow.transport import (
    MCPTransportError,
    MCPTransportTimeout,
    StdioMCPClient,
    StreamableHTTPMCPClient,
)
from app.workflow.planner import (
    BindingResolutionError,
    BindingResolver,
    ExecutionPlan,
    LLMPlannerAdapter,
    LLMPlannerError,
    PlanStep,
    Planner,
    PlannerDecision,
    PlannerError,
    PlannerRequest,
    build_llm_planner,
    resolve_bindings,
    validate_plan_dag,
)
from app.workflow.checkpoint import (
    CheckpointError,
    WorkflowCheckpoint,
    WorkflowCheckpointStore,
    default_checkpoint_path,
)
from app.workflow.trajectory import (
    ArtifactRecord,
    FailureRecord,
    MCPExecutionRecord,
    ReplanRecord,
    RetryRecord,
    SkillSelectionRecord,
    StepRecord,
    TokenUsage,
    ToolCallRecord,
    WorkflowTrajectory,
)
from app.workflow.runtime import (
    WORKFLOW_EVENT_TYPES,
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkflowRuntime,
    WorkflowRuntimeError,
    get_compiled_workflow_graph,
)
from app.workflow.skills import (
    BUILTIN_SKILL_NAMES,
    LoadedSkill,
    SkillExecutionGrant,
    SkillManifest,
    SkillNotFound,
    SkillPrivilegeError,
    SkillSummary,
    SkillsEngine,
)

__all__ = [
    # safety
    "ExecutionContext", "assert_scope_access", "ScopeViolation",
    "ApprovalToken", "ApprovalBinding", "ApprovalRecord", "ApprovalError", "ApprovalAuthority",
    "approval_args_hash",
    "PromptInjectionGuard", "PromptInjectionError",
    "IdempotencyLedger", "IdempotencyRecord", "IdempotencyConflict",
    # ledger / concurrency (FIX-02)
    "ExecutionLedger", "LedgerClaim", "StepLedgerRecord", "RunExecutionGuard", "RunBusy",
    # scope authority (FIX-01 / FIX-02)
    "ExecutionScopeAuthority", "R2WorkspaceResolver", "ScopeEvidence", "ToolAccessPolicy",
    "WorkspaceToolPolicy", "classify_principal", "ensure_human_approver",
    # transport (FIX-03)
    "StdioMCPClient", "StreamableHTTPMCPClient", "MCPTransportError", "MCPTransportTimeout",
    # registry
    "UnifiedToolSpec", "UnifiedToolRegistry", "ToolCollisionError",
    "DuplicateCapabilityError", "ToolNotFound",
    # rules
    "RuleLayer", "RuleDefinition", "RuleConflict", "AppliedRuleRecord",
    "RulesResolution", "RulesEngine", "PROTECTED_CATEGORIES", "load_workspace_rules",
    # skills
    "SkillsEngine", "SkillManifest", "SkillSummary", "LoadedSkill", "SkillExecutionGrant",
    "SkillNotFound", "SkillPrivilegeError", "BUILTIN_SKILL_NAMES",
    # hooks
    "HookEvent", "HookDefinition", "HookRegistry", "HookExecutionRecord",
    "HookDispatchResult", "HookValidationError", "HookSecurityError", "HookRecursionError",
    "FailurePolicy", "HOOK_EVENTS",
    # mcp
    "MCPServerConfig", "MCPClientManager", "MCPState", "MCPDiscoveryReport",
    "MCPToolResult", "MCPHealth", "MCPResource", "MCPPrompt", "CredentialStore",
    "MCPConfigError", "MCPUnavailable", "MCPToolDenied", "MCPApprovalRequired",
    "MCPRegistrationError", "MCPError", "TOOL_POLICY_VALUES",
    "default_client_factory", "unavailable_client_factory",
    # budget
    "ContextBudgetManager", "ContextSegment", "ContextKind", "BudgetPlan",
    "CompactionResult", "BudgetExceeded",
    # office adapter
    "OfficeCapabilityAdapter", "MockOfficeAdapter", "RuntimeOfficeAdapter",
    "OfficeResult", "OfficeAdapterError", "OfficeAdapterUnavailable",
    "OFFICE_CAPABILITIES", "get_office_adapter", "assert_adapter_allowed",
    # planner
    "Planner", "PlannerRequest", "PlannerDecision", "PlannerError",
    "LLMPlannerAdapter", "LLMPlannerError", "build_llm_planner",
    "ExecutionPlan", "PlanStep", "BindingResolver", "BindingResolutionError",
    "resolve_bindings", "validate_plan_dag",
    # checkpoint
    "WorkflowCheckpoint", "WorkflowCheckpointStore", "CheckpointError",
    "default_checkpoint_path",
    # trajectory
    "WorkflowTrajectory", "StepRecord", "ToolCallRecord", "SkillSelectionRecord",
    "RetryRecord", "ReplanRecord", "FailureRecord", "ArtifactRecord",
    "MCPExecutionRecord", "TokenUsage",
    # runtime
    "WorkflowRuntime", "WorkflowRunRequest", "WorkflowRunResult", "WorkflowRuntimeError",
    "WORKFLOW_EVENT_TYPES", "get_compiled_workflow_graph",
    # wiring
    "WorkflowStack", "build_workflow_stack",
]


@dataclass
class WorkflowStack:
    """A default, fully wired set of R3 engines (one per execution environment)."""

    registry: UnifiedToolRegistry
    skills: SkillsEngine
    rules: RulesEngine
    hooks: HookRegistry
    budget: ContextBudgetManager
    safety: dict = field(default_factory=dict)
    mcp: Optional[MCPClientManager] = None
    office: Optional[OfficeCapabilityAdapter] = None
    planner: Optional[Planner] = None


def build_workflow_stack(
    *,
    max_context_tokens: int = 12_000,
    max_step_budget: int = 10,
    max_tool_calls: int = 6,
    workspace_root: Optional[str] = None,
    office_adapter: Optional[OfficeCapabilityAdapter] = None,
    approval_secret: Optional[str] = None,
    ledger_path: Optional[str] = None,
    ledger_backend: str = "memory",
    gateway: Optional[object] = None,
    enable_llm_planner: Optional[bool] = None,
) -> WorkflowStack:
    """Construct the engines in dependency order (no network, no FastAPI).

    ``approval_secret`` defaults to a fresh random secret: approval tokens are only
    meaningful inside the process that issued them, never across restarts.
    ``ledger_backend="sqlite"`` gives the FIX-02 execution ledger a durable store; the
    default in-memory ledger is for tests.  The LLM planner (FIX-05) is only attached when
    it is explicitly enabled, so the deterministic planner remains the default.
    """
    import secrets

    registry = UnifiedToolRegistry()
    registry.bridge_legacy()
    skills = SkillsEngine()
    rules = RulesEngine(workspace_root=workspace_root)
    hooks = HookRegistry()
    budget = ContextBudgetManager(
        max_context_tokens=max_context_tokens,
        max_step_budget=max_step_budget,
        max_tool_calls=max_tool_calls,
    )
    safety = {
        "approvals": ApprovalAuthority(approval_secret or secrets.token_urlsafe(32)),
        "injection": PromptInjectionGuard(),
        "idempotency": IdempotencyLedger(),
        "execution_ledger": ExecutionLedger(path=ledger_path, backend=ledger_backend),  # type: ignore[arg-type]
        "run_guard": RunExecutionGuard(),
    }
    mcp = MCPClientManager(registry, injection_guard=safety["injection"])
    llm = build_llm_planner(gateway, registry=registry, enabled=enable_llm_planner)
    planner = Planner(
        skills=skills, registry=registry, rules=rules, max_steps=max_step_budget,
        max_estimated_tokens=max_context_tokens, llm=llm,
    )
    return WorkflowStack(
        registry=registry,
        skills=skills,
        rules=rules,
        hooks=hooks,
        budget=budget,
        safety=safety,
        mcp=mcp,
        office=office_adapter,
        planner=planner,
    )
