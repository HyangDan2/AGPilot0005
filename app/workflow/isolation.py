"""FIX-07 MCP & tool isolation policy.

MCP servers are third-party processes and endpoints.  Three boundaries are enforced here,
in one place so the API, the manager and the tests cannot drift apart:

* **Administrator-only management** — :func:`require_mcp_admin` gates server registration,
  removal and global settings on an administrator credential (``admin``/``admiral``).
* **Scope isolation** — a server belongs to a tenant/owner/service (or is explicitly
  ``shared``); :func:`server_access_allowed` refuses cross-tenant discovery, description and
  calls, and :func:`mask_endpoint` keeps credentials/hosts out of diagnostics.
* **Egress policy** — :func:`validate_endpoint` (scheme + SSRF/private-network +
  optional host allowlist) and :func:`validate_command` (trusted interpreter allowlist,
  no shell metacharacters) decide what may be started at all.

Nothing here trusts an MCP server's self-description: annotations are handled by
``app.workflow.mcp`` and only ever *fill a gap* when the server policy declares nothing.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from app.workflow.safety import ExecutionContext, ScopeViolation

__all__ = [
    "MCPSecurityError",
    "DEFAULT_COMMAND_ALLOWLIST",
    "BLOCKED_HOSTNAMES",
    "validate_endpoint",
    "validate_command",
    "mask_endpoint",
    "MCPServerAccess",
    "server_access_allowed",
    "assert_server_access",
    "require_mcp_admin",
    "MANAGEMENT_TOOL_ROLES",
]

#: Interpreter/launcher names an operator may start over stdio.  Everything else (a shell,
#: a package manager that can run arbitrary post-install scripts, …) is refused.
DEFAULT_COMMAND_ALLOWLIST: Tuple[str, ...] = (
    "python",
    "python3",
    "python3.11",
    "python3.12",
    "node",
    "deno",
    "bun",
    "uvx",
    "uv",
    "ruby",
    "java",
    "dotnet",
    "go",
    "mcp-server",
)

BLOCKED_HOSTNAMES: Tuple[str, ...] = (
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "instance-data",
)

_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)

_SHELL_METACHARS = re.compile(r"[;&|`$<>\n\r\\]")
_ALLOWED_SCHEMES = ("http", "https")


class MCPSecurityError(ValueError):
    """A server declaration violates the isolation/egress policy."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def _is_blocked_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if address.is_loopback or address.is_private or address.is_link_local or address.is_reserved:
        return True
    return any(address in network for network in _BLOCKED_NETWORKS)


def validate_endpoint(
    url: str,
    *,
    allowed_hosts: Iterable[str] = (),
    allow_private_network: bool = False,
) -> str:
    """Validate an HTTP(S) MCP endpoint against the egress policy (SSRF defence)."""
    target = (url or "").strip()
    if not target:
        raise MCPSecurityError("mcp_target_required")
    parts = urlsplit(target)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise MCPSecurityError("mcp_scheme_not_allowed", parts.scheme or "<none>")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise MCPSecurityError("mcp_host_required")
    if parts.username or parts.password:
        raise MCPSecurityError("mcp_url_credentials_forbidden")
    if host in BLOCKED_HOSTNAMES:
        raise MCPSecurityError("mcp_private_endpoint_blocked", host)
    allowlist = {str(item).strip().lower() for item in allowed_hosts if str(item).strip()}
    if allowlist and host not in allowlist and not any(host.endswith("." + item) for item in allowlist):
        raise MCPSecurityError("mcp_host_not_allowlisted", host)
    if not allow_private_network and _is_blocked_ip(host):
        raise MCPSecurityError("mcp_private_endpoint_blocked", host)
    return target


def validate_command(
    command: str,
    *,
    allowlist: Iterable[str] = (),
    extra_env_denied: Sequence[str] = (),
) -> str:
    """Validate a stdio command: trusted launcher only, no shell, no inline secrets."""
    target = (command or "").strip()
    if not target:
        raise MCPSecurityError("mcp_command_required")
    if _SHELL_METACHARS.search(target):
        raise MCPSecurityError("mcp_command_shell_forbidden", target[:80])
    try:
        argv = shlex.split(target)
    except ValueError as exc:
        raise MCPSecurityError("mcp_command_unparsable", str(exc)) from exc
    if not argv:
        raise MCPSecurityError("mcp_command_required")
    launcher = os.path.basename(argv[0]).lower()
    trusted = {str(item).strip().lower() for item in (allowlist or DEFAULT_COMMAND_ALLOWLIST) if str(item).strip()}
    if launcher not in trusted:
        raise MCPSecurityError("mcp_command_not_allowlisted", launcher)
    return target


def mask_endpoint(value: str) -> str:
    """Render an endpoint for diagnostics: no userinfo, no query secrets, no env values."""
    target = (value or "").strip()
    if not target:
        return ""
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{6,}", r"\1[REDACTED]", target)
    text = re.sub(r"(?i)((?:token|key|secret|password|sig|signature)=)[^&\s]+", r"\1[REDACTED]", text)
    parts = urlsplit(text) if "://" in text else None
    if parts is not None and (parts.username or parts.password):
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        text = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return text[:300]


class MCPServerAccess:
    """Who a server belongs to (``None`` means 'platform-wide' for that dimension)."""

    __slots__ = ("tenant_id", "owner_id", "service_id", "shared")

    def __init__(
        self,
        *,
        tenant_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        service_id: Optional[str] = None,
        shared: bool = False,
    ) -> None:
        self.tenant_id = tenant_id
        self.owner_id = owner_id
        self.service_id = service_id
        self.shared = bool(shared)

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "owner_id": self.owner_id,
            "service_id": self.service_id,
            "shared": self.shared,
        }

    def matches(self, context: Optional[ExecutionContext]) -> bool:
        if self.shared or context is None:
            return True
        if self.tenant_id is not None and str(self.tenant_id) != str(context.tenant_id):
            return False
        if self.owner_id is not None and str(self.owner_id) != str(context.owner_id):
            return False
        if self.service_id is not None and context.service_id is not None and str(self.service_id) != str(context.service_id):
            return False
        return True


def server_access_allowed(access: MCPServerAccess, context: Optional[ExecutionContext]) -> bool:
    return access.matches(context)


def assert_server_access(access: MCPServerAccess, context: Optional[ExecutionContext], *, server_id: str = "?") -> None:
    if not access.matches(context):
        raise ScopeViolation("mcp_server_not_accessible", detail=f"{server_id}: scope mismatch")


#: Roles that may never act as an interactive approver or as an MCP administrator.
MANAGEMENT_TOOL_ROLES = frozenset({"service", "tool", "ai", "assistant", "automation", "system", "guest"})


def require_mcp_admin(scope: Any, *, environment: Optional[str] = None) -> None:
    """Server registration/removal/global settings are administrator actions, always.

    Unlike the R2 admin gate this is *not* relaxed for ordinary users in development:
    managing MCP servers is what grants everyone else new tools, so the credential must be
    administrative wherever the process runs.

    The single exception is the local operator credential (``auth_source="dev_local"``:
    loopback TestClient, or a developer who started the process), and only outside
    production.  A delegated ``credential_scope`` principal - the shape a real client
    presents - must always carry the ``admin``/``admiral`` role.
    """
    from app.config import settings

    if not getattr(scope, "authenticated", False):
        raise ScopeViolation("unauthenticated_scope")
    role = str(getattr(scope, "role", "") or "").lower()
    if role in ("admin", "admiral"):
        return
    auth_source = str(getattr(scope, "auth_source", "") or "")
    production = str(getattr(settings, "ENVIRONMENT", "") or "").lower() in ("production", "prod", "live")
    if auth_source == "dev_local" and not production:
        return
    raise ScopeViolation("mcp_admin_required", detail=f"role={role or 'unknown'}")


def scope_for_declaration(
    tenant_id: Optional[str],
    owner_id: Optional[str],
    service_id: Optional[str],
    *,
    shared: bool = False,
) -> MCPServerAccess:
    return MCPServerAccess(
        tenant_id=tenant_id, owner_id=owner_id, service_id=service_id, shared=shared,
    )


def access_from_mapping(payload: Mapping[str, Any]) -> MCPServerAccess:
    return MCPServerAccess(
        tenant_id=payload.get("tenant_id"),
        owner_id=payload.get("owner_id"),
        service_id=payload.get("service_id"),
        shared=bool(payload.get("shared", False)),
    )
