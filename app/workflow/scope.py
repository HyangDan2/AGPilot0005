"""FIX-01 Execution scope authority.

An R3 run may only touch the tenant / owner / service / conversation / workspace the
*authenticated credential* already owns.  Three things follow and are enforced here:

1. **The request body never holds authority.**  ``tenant_id``, ``owner_id``, ``role`` or
   ``scope`` inside the workflow request context is rejected outright instead of being
   trusted (or silently ignored), so a caller can neither escalate nor mis-attribute a run.
2. **Server-side attribution is the source of truth.**  ``conversation_id`` and
   ``workspace_id`` are re-resolved against the server-side binding (R2
   ``app/workspace/binding.py`` / ``WorkspaceManager``).  A conversation bound to another
   workspace, or a workspace the caller cannot open, is refused before anything runs.
3. **Resume re-verifies.**  A checkpoint carries the scope of the run that created it;
   :meth:`ExecutionScopeAuthority.revalidate` re-checks that stored scope against the
   *current* credential and the *current* binding, so a revoked workspace or a withdrawn
   grant blocks the resume instead of continuing an old decision.
4. **A general chat session never gains a workspace (FIX-02 P0).**  R2 session binding is
   immutable: a conversation that is bound to a plain chat (``workspace_id is None``)
   cannot be given a workspace by a workflow request, and a conversation bound to another
   workspace cannot be redirected.  Both are refused instead of "repaired".

The R2 workspace engine is optional (``AGCORE_WORKSPACE_ENABLED``).  When it is disabled
there is no server-side registry to consult; the workspace id is then kept as an opaque,
owner-scoped handle recorded as ``workspace_verified=False`` instead of being treated as
proof of access - and :class:`WorkspaceToolPolicy` refuses to execute a workspace/canvas
capability that the runtime itself would have to run on that unverified handle (FIX-02 P0).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from app.core.auth import ExecutionScope, validate_scope_identifier
from app.workflow.safety import ExecutionContext, ScopeViolation

__all__ = [
    "BODY_AUTHORITY_KEYS",
    "ScopeEvidence",
    "WorkspaceResolver",
    "R2WorkspaceResolver",
    "ExecutionScopeAuthority",
    "ToolAccessPolicy",
    "WorkspaceToolPolicy",
    "workspace_tool_kind",
]

#: Keys a request body may never use to describe *who* it is.
BODY_AUTHORITY_KEYS: Tuple[str, ...] = (
    "tenant_id",
    "owner_id",
    "scope",
    "role",
    "is_admin",
    "authenticated",
    "auth_source",
    "allowed_tenants",
    "allowed_services",
)


@dataclass(frozen=True)
class ScopeEvidence:
    """What the server could prove about the requested conversation/workspace."""

    identity_source: str
    conversation_bound: bool = False
    workspace_verified: bool = False
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "identity_source": self.identity_source,
            "conversation_bound": self.conversation_bound,
            "workspace_verified": self.workspace_verified,
            "conversation_id": self.conversation_id,
            "workspace_id": self.workspace_id,
            "detail": self.detail,
        }


class WorkspaceResolver(Protocol):
    """Server-side authority for conversation/workspace attribution."""

    def resolve(
        self,
        scope: ExecutionScope,
        *,
        conversation_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> ScopeEvidence: ...


class R2WorkspaceResolver:
    """Resolve attribution through the R2 Session Binding and Workspace Manager.

    ``binding_lookup`` / ``workspace_opener`` are injectable so the API process uses the
    real R2 stores while tests can exercise the policy without a database.
    """

    def __init__(
        self,
        *,
        binding_lookup: Optional[Callable[[ExecutionScope, Optional[str]], Optional[Any]]] = None,
        workspace_opener: Optional[Callable[[str, ExecutionScope], Any]] = None,
        enabled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._binding_lookup = binding_lookup
        self._workspace_opener = workspace_opener
        self._enabled = enabled

    # -- defaults ----------------------------------------------------------------------
    def _workspace_engine_enabled(self) -> bool:
        if self._enabled is not None:
            return bool(self._enabled())
        from app.config import is_workspace_enabled

        return bool(is_workspace_enabled())

    def _lookup_binding(self, scope: ExecutionScope, conversation_id: Optional[str]) -> Optional[Any]:
        if not conversation_id:
            return None
        if self._binding_lookup is not None:
            return self._binding_lookup(scope, conversation_id)
        from app.workspace.binding import find_conversation_workspace

        return find_conversation_workspace(scope, conversation_id)

    def _open_workspace(self, workspace_id: str, scope: ExecutionScope) -> Any:
        if self._workspace_opener is not None:
            return self._workspace_opener(workspace_id, scope)
        from app.workspace.manager import WorkspaceFailure
        from app.workspace.runtime import get_runtime

        try:
            return get_runtime().manager.ensure_open(workspace_id, scope)
        except WorkspaceFailure as exc:
            raise ScopeViolation(
                "workspace_not_accessible",
                detail=f"{workspace_id}: {getattr(getattr(exc, 'error', None), 'code', exc)}",
            ) from exc

    # -- policy ------------------------------------------------------------------------
    def resolve(
        self,
        scope: ExecutionScope,
        *,
        conversation_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> ScopeEvidence:
        if not getattr(scope, "authenticated", False):
            raise ScopeViolation("unauthenticated_scope")

        conversation_id = (conversation_id or "").strip() or None
        workspace_id = (workspace_id or "").strip() or None

        binding = None
        try:
            binding = self._lookup_binding(scope, conversation_id)
        except ScopeViolation:
            raise
        except Exception as exc:  # noqa: BLE001 - an unreadable binding store is a refusal, not a pass
            raise ScopeViolation("conversation_binding_unavailable", detail=type(exc).__name__) from exc

        if binding is not None:
            bound_workspace = getattr(binding, "workspace_id", None)
            if bound_workspace is None:
                # FIX-02 P0: R2 session binding is immutable.  A conversation registered as
                # a plain chat has no workspace, and a request may not attach one to it.
                if workspace_id is not None:
                    raise ScopeViolation(
                        "conversation_workspace_unbound",
                        detail=f"conversation={conversation_id} is a plain chat session",
                    )
            elif workspace_id is not None and str(workspace_id) != str(bound_workspace):
                raise ScopeViolation(
                    "conversation_workspace_mismatch",
                    detail=f"conversation={conversation_id} bound={bound_workspace} requested={workspace_id}",
                )
            elif workspace_id is None:
                # The server-side binding owns the value; adopt it rather than the body.
                workspace_id = str(bound_workspace)

        if workspace_id is None:
            return ScopeEvidence(
                identity_source="authenticated_scope",
                conversation_bound=binding is not None,
                conversation_id=conversation_id,
            )

        if not self._workspace_engine_enabled():
            return ScopeEvidence(
                identity_source="authenticated_scope",
                conversation_bound=binding is not None,
                workspace_verified=False,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                detail="workspace_registry_disabled",
            )

        identity = self._open_workspace(workspace_id, scope)
        workspace_service = getattr(identity, "service_id", None)
        if workspace_service is not None and scope.service_id and str(workspace_service) != str(scope.service_id):
            raise ScopeViolation("workspace_not_accessible", detail=f"{workspace_id}: service mismatch")
        return ScopeEvidence(
            identity_source="workspace_manager",
            conversation_bound=binding is not None,
            workspace_verified=True,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
        )


class ExecutionScopeAuthority:
    """Turn an authenticated :class:`ExecutionScope` plus a request context into a
    verified :class:`ExecutionContext`."""

    def __init__(self, resolver: Optional[WorkspaceResolver] = None) -> None:
        self.resolver = resolver or R2WorkspaceResolver()

    # -- helpers -----------------------------------------------------------------------
    @staticmethod
    def _reject_body_authority(context: Optional[Mapping[str, Any]]) -> None:
        if not context:
            return
        offending = [key for key in BODY_AUTHORITY_KEYS if key in context]
        if offending:
            raise ScopeViolation("body_scope_overrides_forbidden", detail=",".join(sorted(offending)))

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    # -- public API --------------------------------------------------------------------
    def resolve(
        self,
        scope: ExecutionScope,
        context: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[ExecutionContext, ScopeEvidence]:
        """Resolve a new run's execution context (FIX-01)."""
        if not getattr(scope, "authenticated", False):
            raise ScopeViolation("unauthenticated_scope")
        self._reject_body_authority(context)
        payload: Mapping[str, Any] = context or {}

        conversation_id = self._clean(payload.get("conversation_id"))
        workspace_id = self._clean(payload.get("workspace_id"))
        evidence = self.resolver.resolve(
            scope, conversation_id=conversation_id, workspace_id=workspace_id,
        )
        try:
            resolved = ExecutionContext(
                tenant_id=validate_scope_identifier(scope.tenant_id, "tenant_id"),
                owner_id=validate_scope_identifier(scope.owner_id, "owner_id"),
                service_id=self._clean(scope.service_id),
                session_id=self._clean(payload.get("session_id")),
                conversation_id=evidence.conversation_id,
                workspace_id=evidence.workspace_id,
            )
        except ValueError as exc:
            raise ScopeViolation("invalid_execution_context", detail=str(exc)) from exc
        return resolved, evidence

    def revalidate(
        self,
        scope: ExecutionScope,
        stored_scope: Mapping[str, Any],
        *,
        identity: Optional[Tuple[str, str, Optional[str]]] = None,
    ) -> Tuple[ExecutionContext, ScopeEvidence]:
        """Re-verify a stored run scope against the current credential (resume path).

        ``identity`` overrides the (tenant, owner, service) triple, which lets an admin
        transfer keep the stored owner while the workspace binding is still re-checked
        against the *stored* conversation/workspace under the current caller's authority.
        """
        if not getattr(scope, "authenticated", False):
            raise ScopeViolation("unauthenticated_scope")
        if not stored_scope or not stored_scope.get("tenant_id"):
            raise ScopeViolation("unscoped_checkpoint")

        tenant_id, owner_id, service_id = identity or (scope.tenant_id, scope.owner_id, scope.service_id)
        conversation_id = self._clean(stored_scope.get("conversation_id"))
        workspace_id = self._clean(stored_scope.get("workspace_id"))

        # The binding lookup must run under the stored owner's identity, otherwise a
        # workspace the *caller* can see but the run's owner cannot would be admitted.
        authority_scope = scope
        if (tenant_id, owner_id, service_id) != (scope.tenant_id, scope.owner_id, scope.service_id):
            authority_scope = ExecutionScope(
                tenant_id=str(tenant_id),
                owner_id=str(owner_id),
                service_id=service_id,
                role=scope.role,
                authenticated=scope.authenticated,
                auth_source=scope.auth_source,
                allowed_tenants=scope.allowed_tenants,
                allowed_services=scope.allowed_services,
                principal_kind=getattr(scope, "principal_kind", "user"),
            )
        evidence = self.resolver.resolve(
            authority_scope, conversation_id=conversation_id, workspace_id=workspace_id,
        )
        try:
            revalidated = ExecutionContext(
                tenant_id=validate_scope_identifier(str(tenant_id), "tenant_id"),
                owner_id=validate_scope_identifier(str(owner_id), "owner_id"),
                service_id=self._clean(service_id),
                session_id=self._clean(stored_scope.get("session_id")),
                conversation_id=evidence.conversation_id,
                workspace_id=evidence.workspace_id,
            )
        except ValueError as exc:
            raise ScopeViolation("invalid_execution_context", detail=str(exc)) from exc
        return revalidated, evidence


class ToolAccessPolicy:
    """Apply the existing AGCore tool policy (``app/tools/policy.py``) to workflow steps.

    The R3 registry decides *what exists*; this policy decides *whether the caller's
    service may run it*.  Both must agree before a registry-backed capability executes.
    """

    def __init__(self, policy: Optional[Any] = None) -> None:
        self._policy = policy

    def _resolve(self) -> Any:
        if self._policy is not None:
            return self._policy
        from app.tools.policy import tool_policy

        return tool_policy

    def authorize(self, spec: Any, context: ExecutionContext) -> None:
        if spec is None:
            raise ScopeViolation("unknown_capability")
        policy = self._resolve()
        checker = getattr(policy, "is_tool_allowed", None)
        if checker is None:  # pragma: no cover - defensive: policy replaced by a stub
            return
        allowed, reason = checker(getattr(spec, "qualified_name", None) or getattr(spec, "name", ""), context.service_id)
        if not allowed:
            raise ScopeViolation(
                "tool_not_permitted_for_service",
                detail=f"{getattr(spec, 'qualified_name', '?')}: {reason or 'denied'}",
            )


#: Capability families that only mean something inside a Workspace / Canvas.
_WORKSPACE_CAPABILITY_PREFIXES: Tuple[str, ...] = ("workspace.",)
_LEGACY_WORKSPACE_TOOL_PREFIXES: Tuple[str, ...] = ("workspace_",)
_CANVAS_CAPABILITY_PREFIXES: Tuple[str, ...] = ("canvas.",)


def workspace_tool_kind(capability: Any) -> Optional[str]:
    """``"workspace"`` / ``"canvas"`` for a workspace-backed capability, else ``None``."""
    name = str(capability or "").strip().lower()
    if not name:
        return None
    if name.startswith(_CANVAS_CAPABILITY_PREFIXES):
        return "canvas"
    if name.startswith(_WORKSPACE_CAPABILITY_PREFIXES) or name.startswith(_LEGACY_WORKSPACE_TOOL_PREFIXES):
        return "workspace"
    return None


class WorkspaceToolPolicy:
    """FIX-02 P0 - fail-closed gate in front of every workspace/canvas capability.

    A workflow step may not touch a workspace just because the request body named one.
    For capabilities the R3 runtime *executes itself* (registry, MCP, engine tools) three
    things must all hold before the handler is reached:

    1. the run is attributed to a workspace (``ExecutionContext.workspace_id``);
    2. the Workspace (or Canvas) feature is enabled **for the run's service**;
    3. the workspace can still be opened for the run's owner right now
       (``WorkspaceManager.ensure_open``), i.e. the binding is verifiably accessible.

    When the Workspace engine is disabled there is nothing to verify against, so those
    capabilities are refused instead of being executed on an opaque id.

    Adapter-delegated capabilities (the R2.1 boundary, e.g. ``workspace.file.write`` served
    by ``RuntimeOfficeAdapter``) cannot be opened by the R3 runtime, so they are gated by:
    attribution always, and the Workspace/Canvas feature flag for every **mutating** call
    (``write``/``external``/``destructive``) — a deployment with Workspace switched off can
    no longer write into one.  Read-only workspace calls stay with the adapter's own scope
    contract (``AdapterCallScope``), which is where the remaining R2.1 integration lands
    (see ``docs/r3/WORKFLOW_EXECUTION_CONTRACT.md`` §18.9).  The in-memory development mock
    (``is_mock``) is exempt because it has no Workspace engine at all and never runs in
    production.
    """

    #: Transports whose execution the R3 runtime owns end-to-end (full verification).
    VERIFIED_TRANSPORTS = frozenset({"registry", "mcp", "engine"})
    #: In-memory development adapters: no Workspace engine exists to verify against.
    DEVELOPMENT_TRANSPORTS = frozenset({"adapter_dev"})

    def __init__(
        self,
        *,
        workspace_enabled: Optional[Callable[[Optional[str]], bool]] = None,
        canvas_enabled: Optional[Callable[[Optional[str]], bool]] = None,
        verifier: Optional[Callable[[ExecutionContext], None]] = None,
    ) -> None:
        self._workspace_enabled = workspace_enabled
        self._canvas_enabled = canvas_enabled
        self._verifier = verifier

    # -- defaults ----------------------------------------------------------------------
    def _workspace_flag(self, service_id: Optional[str]) -> bool:
        if self._workspace_enabled is not None:
            return bool(self._workspace_enabled(service_id))
        from app.config import is_workspace_enabled_for_service

        return bool(is_workspace_enabled_for_service(service_id))

    def _canvas_flag(self, service_id: Optional[str]) -> bool:
        if self._canvas_enabled is not None:
            return bool(self._canvas_enabled(service_id))
        from app.config import is_canvas_tools_enabled_for_service

        return bool(is_canvas_tools_enabled_for_service(service_id))

    def _verify(self, context: ExecutionContext) -> None:
        """Prove the workspace is still open for this owner, or refuse."""
        if self._verifier is not None:
            self._verifier(context)
            return
        from app.core.auth import ExecutionScope
        from app.workspace.manager import WorkspaceFailure
        from app.workspace.runtime import get_runtime

        scope = ExecutionScope(
            tenant_id=context.tenant_id,
            owner_id=context.owner_id,
            service_id=context.service_id or "",
            role="user",
            authenticated=True,
            auth_source="workflow_runtime",
        )
        try:
            get_runtime().manager.ensure_open(str(context.workspace_id), scope)
        except WorkspaceFailure as exc:
            code = getattr(getattr(exc, "error", None), "code", None)
            raise ScopeViolation(
                "workspace_binding_unverified",
                detail=f"{context.workspace_id}: {getattr(code, 'value', code) or type(exc).__name__}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - an unreadable registry is a refusal
            raise ScopeViolation(
                "workspace_binding_unverified",
                detail=f"{context.workspace_id}: {type(exc).__name__}",
            ) from exc

    # -- policy ------------------------------------------------------------------------
    def authorize(
        self,
        capability: Any,
        context: ExecutionContext,
        *,
        transport: str = "registry",
        mutating: bool = True,
    ) -> None:
        kind = workspace_tool_kind(capability)
        if kind is None:
            return
        if context.workspace_id is None:
            raise ScopeViolation(
                "workspace_binding_required", detail=f"{capability}: run has no workspace attribution",
            )
        name = str(transport or "registry").lower()
        if name in self.DEVELOPMENT_TRANSPORTS:
            return
        verified = name in self.VERIFIED_TRANSPORTS
        flag_gate = verified or bool(mutating)
        if kind == "canvas" and flag_gate and not self._canvas_flag(context.service_id):
            raise ScopeViolation("canvas_tool_disabled", detail=f"{capability}: service={context.service_id or '-'}")
        if flag_gate and not self._workspace_flag(context.service_id):
            raise ScopeViolation(
                "workspace_tool_disabled", detail=f"{capability}: service={context.service_id or '-'}",
            )
        if verified:
            self._verify(context)
