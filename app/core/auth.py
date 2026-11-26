"""FIX-201 & FIX-203: Caller Authentication, ExecutionScope, Token Role Scoping & Admin Guards."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import FrozenSet, Optional, Protocol

from fastapi import HTTPException, Request

from app.config import settings


ADMIN_ROLES: FrozenSet[str] = frozenset({"admin", "admiral"})
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
    auth_source: str  # "admin_service_token" | "service_token" | "trusted_proxy" | "dev_local" | "unauthenticated"

    @property
    def is_admin(self) -> bool:
        return self.authenticated and self.role in ADMIN_ROLES


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


def resolve_caller_scope(request: Request) -> ExecutionScope:
    """Resolves verified Caller Identity into ExecutionScope.

    - Standard Service Tokens CANNOT escalate to admin/admiral via HTTP headers (FIX-201).
    - Admin role requires either ADMIN_SERVICE_TOKENS or a verified Trusted Proxy (with proxy secret in prod).
    - Supports comma-separated key rotation (current + previous keys accepted simultaneously).
    """
    is_prod = settings.ENVIRONMENT.lower() == "production"
    has_service_tokens = bool((settings.SERVICE_TOKENS or "").strip())
    has_admin_tokens = bool((settings.ADMIN_SERVICE_TOKENS or "").strip())
    has_proxy_secret = bool((settings.TRUSTED_PROXY_SECRET or "").strip())
    provided_token = request.headers.get("X-Service-Token")

    # 1. Explicit Admin Service Token (grants admin/admiral capability)
    if has_admin_tokens and validate_service_token(provided_token, settings.ADMIN_SERVICE_TOKENS):
        owner = (request.headers.get("X-HD2Lab-User") or "").strip()
        service = (request.headers.get("X-HD2Lab-Service") or "").strip()
        req_role = (request.headers.get("X-HD2Lab-Role") or "admin").strip().lower()
        role = req_role if req_role in ADMIN_ROLES else "admin"
        if is_prod and (not owner or not service):
            return ExecutionScope("unknown", "", "", "guest", False, "unauthenticated")
        return ExecutionScope(
            tenant_id="public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT,
            owner_id=owner or "admin_service",
            service_id=service or "hd2ai-admin",
            role=role,
            authenticated=True,
            auth_source="admin_service_token",
        )

    # 2. Standard Service Token (NEVER allowed to claim admin/admiral via X-HD2Lab-Role!)
    if has_service_tokens and validate_service_token(provided_token, settings.SERVICE_TOKENS):
        owner = (request.headers.get("X-HD2Lab-User") or "").strip()
        service = (request.headers.get("X-HD2Lab-Service") or "").strip()
        if is_prod and (not owner or not service):
            return ExecutionScope("unknown", "", "", "guest", False, "unauthenticated")
        return ExecutionScope(
            tenant_id="public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT,
            owner_id=owner or "service_caller",
            service_id=service or "hd2ai-admin",
            role="service",  # strictly non-admin!
            authenticated=True,
            auth_source="service_token",
        )

    # 3. Verified Trusted Proxy (in prod requires explicit TRUSTED_PROXY_SECRET)
    if _is_trusted_proxy_client(request) and (has_proxy_secret or (not is_prod and not has_service_tokens and not has_admin_tokens)):
        owner = (request.headers.get("X-HD2Lab-User") or "").strip()
        service = (request.headers.get("X-HD2Lab-Service") or "").strip()
        role = (request.headers.get("X-HD2Lab-Role") or "user").strip().lower()
        if is_prod:
            if not owner or not service:
                return ExecutionScope("unknown", "", "", "guest", False, "unauthenticated")
            return ExecutionScope(
                tenant_id="public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT,
                owner_id=owner,
                service_id=service,
                role=role,
                authenticated=True,
                auth_source="trusted_proxy",
            )
        return ExecutionScope(
            tenant_id="public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT,
            owner_id=owner or "default_user",
            service_id=service or "hd2ai-admin",
            role=role,
            authenticated=True,
            auth_source="dev_local",
        )

    # 4. Development ASGI TestClient (only when not production and no token gates configured)
    if not is_prod and not has_service_tokens and not has_admin_tokens and request.client and request.client.host == "testclient":
        owner = (request.headers.get("X-HD2Lab-User") or "default_user").strip()
        service = (request.headers.get("X-HD2Lab-Service") or "hd2ai-admin").strip()
        role = (request.headers.get("X-HD2Lab-Role") or "user").strip().lower()
        return ExecutionScope(
            tenant_id="public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT,
            owner_id=owner,
            service_id=service,
            role=role,
            authenticated=True,
            auth_source="dev_local",
        )

    # 5. Fail-closed
    return ExecutionScope(
        tenant_id="unauthenticated",
        owner_id="",
        service_id="",
        role="guest",
        authenticated=False,
        auth_source="unauthenticated",
    )


def require_authenticated_caller(request: Request) -> ExecutionScope:
    scope = resolve_caller_scope(request)
    if not scope.authenticated:
        raise HTTPException(status_code=401, detail="authentication_required")
    return scope


def require_admin_caller(request: Request) -> ExecutionScope:
    scope = resolve_caller_scope(request)
    if not scope.authenticated:
        raise HTTPException(status_code=401, detail="authentication_required")
    is_prod = settings.ENVIRONMENT.lower() == "production"
    has_token_gate = bool((settings.SERVICE_TOKENS or "").strip() or (settings.ADMIN_SERVICE_TOKENS or "").strip())
    if (is_prod or has_token_gate) and not scope.is_admin:
        raise HTTPException(status_code=403, detail="admin_permission_required")
    return scope
