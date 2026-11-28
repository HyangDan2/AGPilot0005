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
    assert_scope_access,
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
    "ApprovalToken", "ApprovalRecord", "ApprovalError", "ApprovalAuthority",
    "PromptInjectionGuard", "PromptInjectionError",
    "IdempotencyLedger", "IdempotencyRecord", "IdempotencyConflict",
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
    "MCPRegistrationError", "MCPError",
    # budget
    "ContextBudgetManager", "ContextSegment", "ContextKind", "BudgetPlan",
    "CompactionResult", "BudgetExceeded",
    # office adapter
    "OfficeCapabilityAdapter", "MockOfficeAdapter", "RuntimeOfficeAdapter",
    "OfficeResult", "OfficeAdapterError", "OfficeAdapterUnavailable",
    "OFFICE_CAPABILITIES", "get_office_adapter", "assert_adapter_allowed",
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


def build_workflow_stack(
    *,
    max_context_tokens: int = 12_000,
    max_step_budget: int = 10,
    max_tool_calls: int = 6,
    workspace_root: Optional[str] = None,
    office_adapter: Optional[OfficeCapabilityAdapter] = None,
    approval_secret: Optional[str] = None,
) -> WorkflowStack:
    """Construct the engines in dependency order (no network, no FastAPI).

    ``approval_secret`` defaults to a fresh random secret: approval tokens are only
    meaningful inside the process that issued them, never across restarts.
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
    }
    mcp = MCPClientManager(registry, injection_guard=safety["injection"])
    return WorkflowStack(
        registry=registry,
        skills=skills,
        rules=rules,
        hooks=hooks,
        budget=budget,
        safety=safety,
        mcp=mcp,
        office=office_adapter,
    )
