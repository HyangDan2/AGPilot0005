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


current_execution_scope: ContextVar[Optional["ExecutionScope"]] = ContextVar("current_execution_scope", default=None)
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

    @property
    def is_admin(self) -> bool:
        return self.authenticated and self.role in ADMIN_ROLES

    def can_access_tenant(self, target_tenant: Optional[str]) -> bool:
        """HF-02: Enforces strict tenant isolation for users and explicit management scope for admins."""
        if not self.authenticated:
            return False
        if not self.is_admin:
            return bool(target_tenant) and target_tenant == self.tenant_id
        if self.allowed_tenants is None or "*" in self.allowed_tenants:
            return True
        return bool(target_tenant) and target_tenant in self.allowed_tenants


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
    allowed_tenants = [str(t).strip() for t in (cfg.get("allowed_tenants") or [settings.AGTOOLS_TENANT]) if str(t).strip()]
    owner_delegation = bool(cfg.get("owner_delegation", False))
    fixed_owner = str(cfg.get("fixed_owner") or matched_id).strip()

    req_service = (request.headers.get("X-HD2Lab-Service") or "").strip()
    if not req_service:
        req_service = allowed_services[0] if len(allowed_services) == 1 else ""
    if not req_service or req_service not in allowed_services:
        raise CredentialScopeViolation("service_not_in_credential_scope")

    req_tenant = (request.headers.get("X-HD2Lab-Tenant") or "").strip()
    if req_tenant:
        if req_tenant not in allowed_tenants:
            raise CredentialScopeViolation("tenant_not_in_credential_scope")
        tenant_id = req_tenant
    else:
        tenant_id = allowed_tenants[0] if allowed_tenants else settings.AGTOOLS_TENANT

    req_role = (request.headers.get("X-HD2Lab-Role") or "").strip().lower()
    if req_role and req_role in allowed_roles:
        role = req_role
    else:
        # Never escalate outside allowed_roles
        role = allowed_roles[0] if allowed_roles else "service"

    if owner_delegation:
        req_user = (request.headers.get("X-HD2Lab-User") or "").strip()
        if not req_user:
            raise CredentialScopeViolation("delegated_owner_header_required")
        owner_id = req_user
    else:
        owner_id = fixed_owner

    return ExecutionScope(
        tenant_id=tenant_id,
        owner_id=owner_id,
        service_id=req_service,
        role=role,
        authenticated=True,
        auth_source=f"credential_scope:{matched_id}",
        allowed_tenants=frozenset(allowed_tenants),
    )


def resolve_caller_scope(request: Request) -> ExecutionScope:
    """HF-01: Unified Caller Identity resolution (Credential Scopes + Verified Trusted Proxy only).

    Legacy SERVICE_TOKENS / ADMIN_SERVICE_TOKENS scope-bypass branches are removed.
    """
    is_prod = settings.ENVIRONMENT.lower() == "production"
    has_cred_scopes = bool((settings.CREDENTIAL_SCOPES_JSON or "").strip())
    has_proxy_secret = bool((settings.TRUSTED_PROXY_SECRET or "").strip())
    provided_token = request.headers.get("X-Service-Token")

    # 1. Explicit Credential Scope Binding (NG-03 / HF-01)
    if has_cred_scopes:
        scoped = _match_credential_scope(provided_token, request)
        if scoped is not None:
            return scoped

    # 2. Verified Trusted Proxy (in prod requires explicit TRUSTED_PROXY_SECRET)
    if _is_trusted_proxy_client(request) and (has_proxy_secret or (not is_prod and not has_cred_scopes)):
        owner = (request.headers.get("X-HD2Lab-User") or "").strip()
        service = (request.headers.get("X-HD2Lab-Service") or "").strip()
        role = (request.headers.get("X-HD2Lab-Role") or "user").strip().lower()
        req_tenant = (request.headers.get("X-HD2Lab-Tenant") or "").strip()
        admin_tenants_hdr = (request.headers.get("X-HD2Lab-Admin-Tenants") or "").strip()
        if is_prod:
            if not owner or not service:
                return ExecutionScope("unknown", "", "", "guest", False, "unauthenticated")
            tenant_id = req_tenant or ("public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT)
            if admin_tenants_hdr:
                allowed_t = frozenset(t.strip() for t in admin_tenants_hdr.split(",") if t.strip())
            else:
                allowed_t = frozenset({tenant_id})
            return ExecutionScope(
                tenant_id=tenant_id,
                owner_id=owner,
                service_id=service,
                role=role,
                authenticated=True,
                auth_source="trusted_proxy",
                allowed_tenants=allowed_t,
            )
        tenant_id = req_tenant or ("public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT)
        allowed_t = (
            frozenset(t.strip() for t in admin_tenants_hdr.split(",") if t.strip())
            if admin_tenants_hdr
            else None
        )
        return ExecutionScope(
            tenant_id=tenant_id,
            owner_id=owner or "default_user",
            service_id=service or "hd2ai-admin",
            role=role,
            authenticated=True,
            auth_source="dev_local",
            allowed_tenants=allowed_t,
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
        )

    # 4. Fail-closed
    return ExecutionScope(
        tenant_id="unauthenticated",
        owner_id="",
        service_id="",
        role="guest",
        authenticated=False,
        auth_source="unauthenticated",
    )


def _assert_registered_service(scope: ExecutionScope) -> None:
    from app.tools.policy import tool_policy

    if not tool_policy.is_registered_service(scope.service_id):
        raise HTTPException(status_code=403, detail="unregistered_service")


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
