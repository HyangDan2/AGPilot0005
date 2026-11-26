"""R1-FIX-02 & R1-FIX-05: Caller Authentication, ExecutionScope, and Admin Access Guards."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import FrozenSet, Optional, Protocol

from fastapi import HTTPException, Request

from app.config import settings


ADMIN_ROLES: FrozenSet[str] = frozenset({"admin", "admiral"})


@dataclass(frozen=True)
class ExecutionScope:
    """Verified caller identity and execution boundary scope."""

    tenant_id: str
    owner_id: str
    service_id: str
    role: str
    authenticated: bool
    auth_source: str  # "service_token" | "trusted_proxy" | "dev_local" | "unauthenticated"

    @property
    def is_admin(self) -> bool:
        return self.authenticated and self.role in ADMIN_ROLES


class DesktopAuthProvider(Protocol):
    """Reserved interface for future Desktop Local Runtime authentication (R1-FIX-05 A.7)."""

    def verify_local_token(self, token: str) -> Optional[ExecutionScope]:
        ...


def validate_service_token(provided: Optional[str], configured_tokens: str) -> bool:
    """Fail-closed constant-time service token validation. Rejects None, empty, or whitespace."""
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
    """Fail-closed check for AGTools outbound service token."""
    return bool(token and token.strip())


def _is_trusted_proxy_client(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    host = (client.host or "").strip()
    trusted = {ip.strip() for ip in settings.TRUSTED_PROXY_IPS.split(",") if ip.strip()}
    return host in trusted


def resolve_caller_scope(request: Request) -> ExecutionScope:
    """Resolves verified Caller Identity into ExecutionScope.

    - Never trusts arbitrary external HTTP headers as authentication in production.
    - Distinguishes Service Token callers, Trusted Proxy (Nginx/Admin-SSO), and local dev/test.
    """
    is_prod = settings.ENVIRONMENT.lower() == "production"
    has_service_tokens = bool(settings.SERVICE_TOKENS and settings.SERVICE_TOKENS.strip())
    provided_token = request.headers.get("X-Service-Token")

    # 1. Explicit Service Token authentication (direct API / trusted service-to-service)
    if has_service_tokens and validate_service_token(provided_token, settings.SERVICE_TOKENS):
        owner = (request.headers.get("X-HD2Lab-User") or "").strip()
        service = (request.headers.get("X-HD2Lab-Service") or "").strip()
        role = (request.headers.get("X-HD2Lab-Role") or "service").strip().lower()
        if is_prod and (not owner or not service):
            return ExecutionScope(
                tenant_id="unknown",
                owner_id="",
                service_id="",
                role="guest",
                authenticated=False,
                auth_source="unauthenticated",
            )
        return ExecutionScope(
            tenant_id="public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT,
            owner_id=owner or "service_caller",
            service_id=service or "hd2ai-admin",
            role=role,
            authenticated=True,
            auth_source="service_token",
        )

    # 2. Trusted Proxy (Nginx + Admin SSO on loopback) when SERVICE_TOKENS is not enforced
    if _is_trusted_proxy_client(request) and not has_service_tokens:
        owner = (request.headers.get("X-HD2Lab-User") or "").strip()
        service = (request.headers.get("X-HD2Lab-Service") or "").strip()
        role = (request.headers.get("X-HD2Lab-Role") or "user").strip().lower()
        if is_prod:
            if not owner or not service:
                return ExecutionScope(
                    tenant_id="unknown",
                    owner_id="",
                    service_id="",
                    role="guest",
                    authenticated=False,
                    auth_source="unauthenticated",
                )
            return ExecutionScope(
                tenant_id="public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT,
                owner_id=owner,
                service_id=service,
                role=role,
                authenticated=True,
                auth_source="trusted_proxy",
            )
        # Development / Test mode on loopback or ASGI TestClient
        return ExecutionScope(
            tenant_id="public_tenant" if service == "hd2ai-public" else settings.AGTOOLS_TENANT,
            owner_id=owner or "default_user",
            service_id=service or "hd2ai-admin",
            role=role,
            authenticated=True,
            auth_source="dev_local",
        )

    # 3. Development ASGI TestClient (client.host == "testclient") when SERVICE_TOKENS not configured
    if not is_prod and not has_service_tokens and request.client and request.client.host == "testclient":
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

    # 4. Fail-closed: untrusted / unauthenticated caller (ignore forged role headers!)
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
    has_service_tokens = bool(settings.SERVICE_TOKENS and settings.SERVICE_TOKENS.strip())
    if (is_prod or has_service_tokens) and not scope.is_admin:
        raise HTTPException(status_code=403, detail="admin_permission_required")
    return scope
