"""FIX-201 & FIX-203: Caller Authentication, ExecutionScope, Token Role Scoping & Admin Guards."""
from __future__ import annotations

import hmac
import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Protocol

from fastapi import HTTPException, Request

from app.config import settings


ADMIN_ROLES: FrozenSet[str] = frozenset({"admin", "admiral"})


class CredentialScopeViolation(PermissionError):
    """Raised when an authenticated credential attempts to exceed its bound service/tenant/role scope (NG-03)."""


SCOPE_DELIMITER = "::"
FORBIDDEN_SCOPE_CHAR = ":"


def validate_scope_identifier(value: str, field_name: str) -> str:
    """RB-01: Shared input validation blocking ':' (including '::' boundary collisions) in tenant_id and owner_id."""
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"empty_{field_name}")
    if FORBIDDEN_SCOPE_CHAR in cleaned:
        raise ValueError(f"invalid_{field_name}_delimiter")
    return cleaned


current_execution_scope: ContextVar[Optional["ExecutionScope"]] = ContextVar("current_execution_scope", default=None)
current_run_approvals: ContextVar[FrozenSet[str]] = ContextVar("current_run_approvals", default=frozenset())
current_run_conversation_id: ContextVar[Optional[str]] = ContextVar("current_run_conversation_id", default=None)


@dataclass(frozen=True)
class ConversationWorkspace:
    conversation_id: str
    workspace_id: Optional[str]


current_run_workspace_binding: ContextVar[Optional[ConversationWorkspace]] = ContextVar(
    "current_run_workspace_binding", default=None,
)


def has_user_approval(
    approvals: FrozenSet[str],
    *,
    tool_name: str,
    action: str,
    target_path: Optional[str] = None,
) -> bool:
    """R2-INT-FIX-03-BE: Verify explicit server-side user approval from AgentRunRequest.approvals.

    Never trusts LLM-supplied tool arguments (e.g., confirm=True).
    Accepted approval tokens:
    - Exact path-scoped:  ``<tool_name>:<path>``, ``<action>:<path>``
    - Tool-scoped:        ``<tool_name>``
    - Action-scoped:      ``workspace:<action>``
    - General write grant (NOT valid for irreversible delete): ``workspace:write_approved``
    """
    if not approvals:
        return False
    norm_path = (target_path or "").strip().lstrip("/")
    candidates = {tool_name, f"workspace:{action}"}
    if norm_path:
        candidates.add(f"{tool_name}:{norm_path}")
        candidates.add(f"{action}:{norm_path}")
    if action != "delete":
        candidates.add("workspace:write_approved")
    return any(tok in approvals for tok in candidates)
_FORBIDDEN_TOKEN_PLACEHOLDERS: FrozenSet[str] = frozenset(
    {
        "changeme",
        "placeholder",
        "replace_me",
        "replace-me",
        "your-token-here",
        "your_token_here",
        "secret",
        "token",
        "test",
        "dummy",
        "none",
        "null",
        "hd2-doc-secret-token-key-2026",  # retired legacy default token
    }
)


@dataclass(frozen=True)
class ExecutionScope:
    """Verified caller identity and execution boundary scope."""

    tenant_id: str
    owner_id: str
    service_id: str
    role: str
    authenticated: bool
    auth_source: str  # "credential_scope:<id>" | "trusted_proxy" | "dev_local" | "unauthenticated"
    allowed_tenants: Optional[FrozenSet[str]] = None
    allowed_services: Optional[FrozenSet[str]] = None
    #: FIX-02 P1: what kind of actor this credential represents (``user``/``machine``/
    #: ``anonymous``).  A role string alone never proves a human, so anything that needs a
    #: person (a Human Approval) asks this, not ``role``.  An empty value means "not
    #: classified" and is resolved fail-closed (a credential scope is a machine).
    principal_kind: str = ""

    @property
    def is_admin(self) -> bool:
        return self.authenticated and self.role in ADMIN_ROLES

    @property
    def is_machine_principal(self) -> bool:
        """True for service credentials, API keys, bots and automated principals."""
        declared = (self.principal_kind or "").strip().lower()
        if declared in ("machine", "anonymous"):
            return True
        if (self.auth_source or "").startswith("credential_scope"):
            return True
        return (self.role or "").strip().lower() in ("service", "tool", "system", "automation", "bot", "ai", "assistant", "guest")

    def can_access_tenant(self, target_tenant: Optional[str]) -> bool:
        """HF-02 / ADD-01 / RB-02: Strict tenant isolation; admin with None/empty scope explicitly fails closed."""
        if not self.authenticated:
            return False
        if not self.is_admin:
            return bool(target_tenant) and target_tenant == self.tenant_id
        if not self.allowed_tenants:
            return False
        if "*" in self.allowed_tenants:
            return True
        return bool(target_tenant) and target_tenant in self.allowed_tenants

    def can_access_service(self, target_service: Optional[str]) -> bool:
        """ADD-01 / RB-02: Strict service isolation; admin with None/empty scope explicitly fails closed."""
        if not self.authenticated:
            return False
        if not self.is_admin:
            return bool(target_service) and target_service == self.service_id
        if not self.allowed_services:
            return False
        if "*" in self.allowed_services:
            return True
        return bool(target_service) and target_service in self.allowed_services


def scoped_analysis_owner(scope: ExecutionScope) -> str:
    """HF-02: Binds tenant_id into the durable analysis owner key without guessing legacy tenants."""
    return f"{scope.tenant_id}::{scope.owner_id}"


class DesktopAuthProvider(Protocol):
    """Reserved interface for future Desktop Local Runtime authentication (R1-FIX-05 A.7)."""

    def verify_local_token(self, token: str) -> Optional[ExecutionScope]:
        ...


def validate_service_token(provided: Optional[str], configured_tokens: str) -> bool:
    """Fail-closed constant-time service token validation supporting comma-separated key rotation."""
    if not provided or not provided.strip():
        return False
    if not configured_tokens or not configured_tokens.strip():
        return False
    candidate = provided.strip()
    valid_tokens = [tok.strip() for tok in configured_tokens.split(",") if tok.strip()]
    if not valid_tokens:
        return False
    return any(hmac.compare_digest(candidate, valid) for valid in valid_tokens)


def is_agtools_token_valid(token: Optional[str]) -> bool:
    """FIX-203: Fail-closed check rejecting empty, whitespace, placeholder, and retired default tokens."""
    if not token or not token.strip():
        return False
    norm = token.strip().lower()
    if norm in _FORBIDDEN_TOKEN_PLACEHOLDERS:
        return False
    if len(norm) < 8:
        return False
    return True


def _is_trusted_proxy_client(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    host = (client.host or "").strip()
    trusted = {ip.strip() for ip in settings.TRUSTED_PROXY_IPS.split(",") if ip.strip()}
    if host not in trusted:
        return False
    # FIX-201: Loopback alone is NOT sufficient if TRUSTED_PROXY_SECRET is configured
    proxy_secret = (settings.TRUSTED_PROXY_SECRET or "").strip()
    if proxy_secret:
        provided_proxy_secret = (request.headers.get("X-Trusted-Proxy-Secret") or "").strip()
        if not provided_proxy_secret or not hmac.compare_digest(provided_proxy_secret, proxy_secret):
            return False
    return True


def _match_credential_scope(provided_token: Optional[str], request: Request) -> Optional[ExecutionScope]:
    """NG-03: Matches a presented service token against configured Credential Scopes (secret separated from ID)."""
    if not provided_token or not provided_token.strip():
        return None
    raw_scopes = (settings.CREDENTIAL_SCOPES_JSON or "").strip()
    raw_secrets = (settings.CREDENTIAL_SECRETS_JSON or "").strip()
    if not raw_scopes or not raw_secrets:
        return None
    try:
        scopes_map: Dict[str, Any] = json.loads(raw_scopes)
        secrets_map: Dict[str, List[str]] = json.loads(raw_secrets)
    except Exception:
        return None

    candidate = provided_token.strip()
    matched_id: Optional[str] = None
    for cid, secret_list in secrets_map.items():
        if cid not in scopes_map or not isinstance(secret_list, list):
            continue
        for sec in secret_list:
            if isinstance(sec, str) and sec.strip() and hmac.compare_digest(candidate, sec.strip()):
                matched_id = cid
                break
        if matched_id:
            break

    if matched_id is None:
        return None

    cfg = scopes_map[matched_id]
    allowed_services = [str(s).strip() for s in (cfg.get("allowed_services") or []) if str(s).strip()]
    allowed_roles = [str(r).strip().lower() for r in (cfg.get("allowed_roles") or ["service"]) if str(r).strip()]
    req_role = (request.headers.get("X-HD2Lab-Role") or "").strip().lower()
    if req_role and req_role in allowed_roles:
        role = req_role
    else:
        role = allowed_roles[0] if allowed_roles else "service"

    # RB-02: For admin roles, NEVER auto-default omitted/empty allowed_tenants (must fail closed!)
    raw_tenants = cfg.get("allowed_tenants")
    if role in ADMIN_ROLES:
        allowed_tenants = [str(t).strip() for t in (raw_tenants or []) if str(t).strip()]
    else:
        allowed_tenants = [str(t).strip() for t in (raw_tenants if raw_tenants is not None else [settings.AGTOOLS_TENANT]) if str(t).strip()]

    owner_delegation = bool(cfg.get("owner_delegation", False))
    fixed_owner = str(cfg.get("fixed_owner") or matched_id).strip()

    req_service = (request.headers.get("X-HD2Lab-Service") or "").strip()
    concrete_services = [s for s in allowed_services if s != "*"]
    if not req_service:
        req_service = concrete_services[0] if len(concrete_services) == 1 else ""
    if not req_service or (req_service not in allowed_services and "*" not in allowed_services):
        raise CredentialScopeViolation("service_not_in_credential_scope")

    req_tenant = (request.headers.get("X-HD2Lab-Tenant") or "").strip()
    concrete_tenants = [t for t in allowed_tenants if t != "*"]
    if req_tenant:
        if req_tenant not in allowed_tenants and "*" not in allowed_tenants:
            raise CredentialScopeViolation("tenant_not_in_credential_scope")
        tenant_id = req_tenant
    else:
        tenant_id = concrete_tenants[0] if concrete_tenants else settings.AGTOOLS_TENANT

    if owner_delegation:
        req_user = (request.headers.get("X-HD2Lab-User") or "").strip()
        if not req_user:
            raise CredentialScopeViolation("delegated_owner_header_required")
        owner_id = req_user
    else:
        owner_id = fixed_owner

    try:
        tenant_id = validate_scope_identifier(tenant_id, "tenant_id")
        owner_id = validate_scope_identifier(owner_id, "owner_id")
    except ValueError as exc:
        raise CredentialScopeViolation(str(exc)) from exc

    # FIX-02 P1: a credential scope is a machine credential (API key / service account).
    # It becomes a *human* delegation only when the operator explicitly declares
    # ``"principal_kind": "user"`` for that credential (e.g. a sign-in flow that hands the
    # user's own session to this service); otherwise a role such as ``admin`` on an API key
    # still never counts as a person for Human Approval.
    declared_kind = str(cfg.get("principal_kind") or "").strip().lower()
    if declared_kind not in ("user", "machine"):
        declared_kind = "machine"

    return ExecutionScope(
        tenant_id=tenant_id,
        owner_id=owner_id,
        service_id=req_service,
        role=role,
        authenticated=True,
        auth_source=f"credential_scope:{matched_id}",
        allowed_tenants=frozenset(allowed_tenants),
        allowed_services=frozenset(allowed_services),
        principal_kind=declared_kind,
    )


def resolve_caller_scope(request: Request) -> ExecutionScope:
    """HF-01 / ADD-01: Unified Caller Identity resolution (Credential Scopes + Verified Trusted Proxy only).

    - Legacy SERVICE_TOKENS / ADMIN_SERVICE_TOKENS scope-bypass branches are removed.
    - Invalid X-Service-Token NEVER falls back to Trusted Proxy or dev_local (ADD-01).
    - Request headers alone CANNOT expand admin_tenants or admin_services (ADD-01).
    - No implicit wildcard for regular admins (ADD-01).
    """
    is_prod = settings.ENVIRONMENT.lower() == "production"
    has_cred_scopes = bool((settings.CREDENTIAL_SCOPES_JSON or "").strip())
    has_proxy_secret = bool((settings.TRUSTED_PROXY_SECRET or "").strip())
    provided_token = request.headers.get("X-Service-Token")

    # 1. Explicit Credential Scope Binding (NG-03 / HF-01 / ADD-01)
    # If X-Service-Token is presented, NEVER fall back to Trusted Proxy on mismatch!
    if provided_token is not None:
        if has_cred_scopes:
            scoped = _match_credential_scope(provided_token, request)
            if scoped is not None:
                return scoped
        return ExecutionScope(
            tenant_id="unauthenticated",
            owner_id="",
            service_id="",
            role="guest",
            authenticated=False,
            auth_source="invalid_credential",
            principal_kind="anonymous",
        )

    # 2. Verified Trusted Proxy (in prod requires explicit TRUSTED_PROXY_SECRET)
    if _is_trusted_proxy_client(request) and (has_proxy_secret or (not is_prod and not has_cred_scopes)):
        owner = (request.headers.get("X-HD2Lab-User") or "").strip()
        service = (request.headers.get("X-HD2Lab-Service") or "").strip()
        role = (request.headers.get("X-HD2Lab-Role") or "user").strip().lower()
        req_tenant = (request.headers.get("X-HD2Lab-Tenant") or "").strip()
        if is_prod:
            if not owner or not service:
                return ExecutionScope("unknown", "", "", "guest", False, "unauthenticated", principal_kind="anonymous")
            tenant_id = req_tenant or ("public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT)
            # ADD-01 / RB-02: Admin scope headers are ONLY trusted when TRUSTED_PROXY_SECRET is verified;
            # missing or empty admin scope MUST fail closed (frozenset()), NEVER auto-assigned!
            admin_tenants_hdr = (request.headers.get("X-HD2Lab-Admin-Tenants") or "").strip() if has_proxy_secret else ""
            admin_services_hdr = (request.headers.get("X-HD2Lab-Admin-Services") or "").strip() if has_proxy_secret else ""
            if role in ADMIN_ROLES:
                allowed_t = frozenset(t.strip() for t in admin_tenants_hdr.split(",") if t.strip())
                allowed_s = frozenset(s.strip() for s in admin_services_hdr.split(",") if s.strip())
            else:
                allowed_t = frozenset({tenant_id})
                allowed_s = frozenset({service})
            return ExecutionScope(
                tenant_id=tenant_id,
                owner_id=owner,
                service_id=service,
                role=role,
                authenticated=True,
                auth_source="trusted_proxy",
                allowed_tenants=allowed_t,
                allowed_services=allowed_s,
                principal_kind="user",
            )
        resolved_service = service or "hd2ai-admin"
        tenant_id = req_tenant or ("public_tenant" if resolved_service == "hd2ai-public" else settings.AGTOOLS_TENANT)
        # In non-prod without verified proxy secret, ignore admin expansion headers & fail closed for admin scope
        admin_tenants_hdr = (request.headers.get("X-HD2Lab-Admin-Tenants") or "").strip() if has_proxy_secret else ""
        admin_services_hdr = (request.headers.get("X-HD2Lab-Admin-Services") or "").strip() if has_proxy_secret else ""
        if role in ADMIN_ROLES:
            allowed_t = frozenset(t.strip() for t in admin_tenants_hdr.split(",") if t.strip())
            allowed_s = frozenset(s.strip() for s in admin_services_hdr.split(",") if s.strip())
        else:
            allowed_t = frozenset({tenant_id})
            allowed_s = frozenset({resolved_service})
        return ExecutionScope(
            tenant_id=tenant_id,
            owner_id=owner or "default_user",
            service_id=resolved_service,
            role=role,
            authenticated=True,
            auth_source="dev_local",
            allowed_tenants=allowed_t,
            allowed_services=allowed_s,
            principal_kind="user",
        )

    # 3. Development ASGI TestClient (only when not production and no auth gates configured)
    if not is_prod and not has_cred_scopes and not has_proxy_secret and request.client and request.client.host == "testclient":
        owner = (request.headers.get("X-HD2Lab-User") or "default_user").strip()
        service = (request.headers.get("X-HD2Lab-Service") or "hd2ai-admin").strip()
        role = (request.headers.get("X-HD2Lab-Role") or "user").strip().lower()
        req_tenant = (request.headers.get("X-HD2Lab-Tenant") or "").strip()
        tenant_id = req_tenant or ("public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT)
        return ExecutionScope(
            tenant_id=tenant_id,
            owner_id=owner,
            service_id=service,
            role=role,
            authenticated=True,
            auth_source="dev_local",
            allowed_tenants=frozenset({tenant_id}),
            allowed_services=frozenset({service}),
            principal_kind="user",
        )

    # 4. Fail-closed
    return ExecutionScope(
        tenant_id="unauthenticated",
        owner_id="",
        service_id="",
        role="guest",
        authenticated=False,
        auth_source="unauthenticated",
        principal_kind="anonymous",
    )


def _assert_registered_service(scope: ExecutionScope) -> None:
    from app.tools.policy import tool_policy

    if not tool_policy.is_registered_service(scope.service_id):
        raise HTTPException(status_code=403, detail="unregistered_service")
    try:
        validate_scope_identifier(scope.tenant_id, "tenant_id")
        validate_scope_identifier(scope.owner_id, "owner_id")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def require_authenticated_caller(request: Request) -> ExecutionScope:
    try:
        scope = resolve_caller_scope(request)
    except CredentialScopeViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not scope.authenticated:
        raise HTTPException(status_code=401, detail="authentication_required")
    _assert_registered_service(scope)
    return scope


def require_admin_caller(request: Request) -> ExecutionScope:
    try:
        scope = resolve_caller_scope(request)
    except CredentialScopeViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not scope.authenticated:
        raise HTTPException(status_code=401, detail="authentication_required")
    _assert_registered_service(scope)
    is_prod = settings.ENVIRONMENT.lower() == "production"
    has_auth_gate = bool(
        (settings.CREDENTIAL_SCOPES_JSON or "").strip()
        or (settings.TRUSTED_PROXY_SECRET or "").strip()
    )
    if (is_prod or has_auth_gate) and not scope.is_admin:
        raise HTTPException(status_code=403, detail="admin_permission_required")
    return scope
