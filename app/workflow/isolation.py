"""FIX-07 MCP & tool isolation policy.

MCP servers are third-party processes and endpoints.  Three boundaries are enforced here,
in one place so the API, the manager and the tests cannot drift apart:

* **Administrator-only management** — :func:`require_mcp_admin` gates server registration,
  removal and global settings on an administrator credential (``admin``/``admiral``).
* **Scope isolation** — a server belongs to a tenant/owner/service (or is explicitly
  ``shared``); :func:`server_access_allowed` refuses cross-tenant discovery, description and
  calls, and :func:`mask_endpoint` keeps credentials/hosts out of diagnostics.
* **Egress policy** — :func:`validate_endpoint` (scheme + SSRF/private-network + host
  allowlist + **real DNS resolution** of the host, so a name pointing at a private /
  loopback / link-local / reserved address is refused) and :func:`validate_command`
  (trusted interpreter allowlist, resolved real path, dangerous inline-execution flags,
  no shell metacharacters) decide what may be started at all.
* **Child-process isolation** — :func:`sanitize_child_env` (whitelist of harmless variables,
  sensitive secrets stripped) and :func:`limit_child_process` (resource limits) run in the
  child; see :mod:`app.workflow.transport` for the process-group lifecycle.

Nothing here trusts an MCP server's self-description: annotations are handled by
``app.workflow.mcp`` and only ever *fill a gap* when the server policy declares nothing.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
import shutil
import socket
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from app.workflow.safety import ExecutionContext, ScopeViolation

__all__ = [
    "MCPSecurityError",
    "DEFAULT_COMMAND_ALLOWLIST",
    "BLOCKED_HOSTNAMES",
    "DANGEROUS_ARG_FLAGS",
    "SENSITIVE_ENV_PREFIXES",
    "SAFE_ENV_KEYS",
    "validate_endpoint",
    "validate_command",
    "resolve_host_addresses",
    "assert_egress_target",
    "resolve_executable",
    "validate_exec_arguments",
    "is_sensitive_env_key",
    "sanitize_child_env",
    "limit_child_process",
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

#: Interpreter flags that execute code supplied on the command line (or open an
#: interactive session).  An MCP server declaration never needs them.
DANGEROUS_ARG_FLAGS: Tuple[str, ...] = (
    "-c", "--code", "--command",
    "-e", "--eval", "-p", "--print",
    "--require", "-r", "--import", "--loader", "--experimental-loader",
    "--inspect", "--inspect-brk", "-i", "--interactive", "--exec", "--system",
)
_SHORT_FLAG_CLUSTER = re.compile(r"^-[A-Za-z]{1,6}$")

#: Environment variables that never reach a third-party server process.
SENSITIVE_ENV_PREFIXES: Tuple[str, ...] = (
    "AWS_", "AZURE_", "AZ_", "GCP_", "GOOGLE_", "OCI_",
    "OPENAI_", "ANTHROPIC_", "OPENROUTER_", "HUGGINGFACE_", "HF_",
    "GITHUB_", "GITLAB_", "GH_", "DATABASE_", "DB_", "REDIS_",
    "SMTP_", "MAIL_", "SENDGRID_", "STRIPE_", "SLACK_", "DISCORD_",
    "NAVER_", "KAKAO_", "TWILIO_", "TELEGRAM_", "PICKLE_",
)
_SENSITIVE_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "DATABASE_URL",
        "REDIS_URL", "SECRET_KEY", "PRIVATE_KEY", "APPROVAL_SECRET", "AGCORE_APPROVAL_SECRET",
        "CREDENTIAL_SCOPES_JSON", "CREDENTIAL_SECRETS_JSON", "TRUSTED_PROXY_SECRET",
        "AUTHORIZATION", "COOKIE", "SESSION", "PASSWORD", "API_KEY", "TOKEN", "ACCESS_KEY",
    }
)
_SENSITIVE_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD", "_API_KEY", "_CREDENTIAL", "_CREDENTIALS")

#: Variables a server process may inherit (a launcher needs no more than these).
SAFE_ENV_KEYS = frozenset(
    {
        "PATH", "HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
        "TZ", "TERM", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC",
        "PYTHONIOENCODING", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8",
        "NODE_ENV", "VIRTUAL_ENV", "CONDA_PREFIX",
    }
)
_MAX_RESOLVED_ADDRESSES = 16


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
    if address.is_multicast or address.is_unspecified:
        return True
    return any(address in network for network in _BLOCKED_NETWORKS)


def resolve_host_addresses(host: str, *, port: Optional[int] = None) -> List[str]:
    """Resolve ``host`` to every address it currently points at (FIX-02 P1).

    Used to judge the *real* destination instead of the URL string: a name that looks
    public but resolves to ``127.0.0.1`` / ``169.254.169.254`` / an RFC1918 address is an
    SSRF, and DNS rebinding must be re-checked at connect time as well as at declaration
    time.  Returns an empty list when the name does not resolve at all.
    """
    name = (host or "").strip().strip("[]")
    if not name:
        return []
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return [str(ip)]
    try:
        infos = socket.getaddrinfo(name, port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    addresses: List[str] = []
    for info in infos[:_MAX_RESOLVED_ADDRESSES]:
        try:
            address = str(ipaddress.ip_address(info[4][0].split("%")[0]))
        except (IndexError, ValueError):  # pragma: no cover - defensive
            continue
        if address not in addresses:
            addresses.append(address)
    return addresses


def _blocked_resolved_address(
    host: str, *, port: Optional[int] = None, resolver: Optional[Callable[..., List[str]]] = None,
) -> Optional[str]:
    addresses = resolver(host) if resolver is not None else resolve_host_addresses(host, port=port)
    for address in addresses:
        if _is_blocked_ip(address):
            return address
    return None


def validate_endpoint(
    url: str,
    *,
    allowed_hosts: Iterable[str] = (),
    allow_private_network: bool = False,
    resolve_dns: bool = True,
    resolver: Optional[Callable[..., List[str]]] = None,
) -> str:
    """Validate an HTTP(S) MCP endpoint against the egress policy (SSRF defence).

    Beyond scheme/userinfo/allowlist checks this resolves the host and refuses it when any
    address it points at is loopback, private, link-local, multicast, reserved or a known
    cloud metadata address.  ``allow_private_network=True`` is the explicit test/local
    opt-in and is never the default.  A host that does not resolve is refused at connect
    time (:func:`assert_egress_target`), not silently trusted here, so a declaration can
    still be stored while its DNS is temporarily unavailable.
    """
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
    if not allow_private_network and resolve_dns:
        blocked = _blocked_resolved_address(host, port=parts.port, resolver=resolver)
        if blocked is not None:
            raise MCPSecurityError("mcp_private_endpoint_blocked", f"{host} -> {blocked}")
    return target


def assert_egress_target(
    url: str,
    *,
    allowed_hosts: Iterable[str] = (),
    allow_private_network: bool = False,
    resolver: Optional[Callable[..., List[str]]] = None,
) -> str:
    """Re-validate an endpoint immediately before a request (DNS-rebinding defence).

    Redirects are refused by the transport itself: an MCP endpoint answers JSON-RPC, so a
    ``3xx`` is treated as an attempted egress pivot instead of being followed.
    """
    target = validate_endpoint(
        url, allowed_hosts=allowed_hosts, allow_private_network=allow_private_network,
    )
    if not allow_private_network:
        parts = urlsplit(target)
        host = (parts.hostname or "").lower()
        addresses = resolver(host) if resolver is not None else resolve_host_addresses(host, port=parts.port)
        if not addresses:
            raise MCPSecurityError("mcp_host_unresolvable", host)
        for address in addresses:
            if _is_blocked_ip(address):
                raise MCPSecurityError("mcp_private_endpoint_blocked", f"{host} -> {address}")
    return target


def resolve_executable(
    argv0: str,
    *,
    allowlist: Iterable[str] = (),
    search_path: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve a stdio launcher to its real path and check it against the allowlist.

    ``PYTHONPATH``-style tricks and a shadowing symlink in ``PATH`` are what this stops: the
    *resolved* file name must be an allowlisted interpreter, and both the declared name and
    the resolved name are checked so ``python`` -> ``/tmp/evil`` cannot pass.
    """
    declared = (argv0 or "").strip()
    if not declared:
        raise MCPSecurityError("mcp_command_required")
    trusted = {str(item).strip().lower() for item in (allowlist or DEFAULT_COMMAND_ALLOWLIST) if str(item).strip()}
    declared_name = os.path.basename(declared).lower()
    if declared_name not in trusted:
        raise MCPSecurityError("mcp_command_not_allowlisted", declared_name)
    if os.sep in declared or (os.altsep and os.altsep in declared):
        resolved = os.path.realpath(declared)
    else:
        resolved = shutil.which(declared, path=(search_path or {}).get("PATH") if search_path else None)
        if not resolved:
            raise MCPSecurityError("mcp_command_not_found", declared_name)
        resolved = os.path.realpath(resolved)
    if not os.path.isfile(resolved):
        raise MCPSecurityError("mcp_command_not_a_file", resolved[:200])
    if not os.access(resolved, os.X_OK):
        raise MCPSecurityError("mcp_command_not_executable", resolved[:200])
    resolved_name = os.path.basename(resolved).lower()
    # A symlink/shadow that resolves to a different, non-allowlisted program is refused.
    if resolved_name not in trusted:
        raise MCPSecurityError("mcp_command_path_escape", f"{declared_name} -> {resolved_name}")
    return resolved


def validate_exec_arguments(argv: Sequence[str]) -> None:
    """Refuse inline-code / interactive interpreter flags (``-c``, ``-e``, ``--eval`` …)."""
    for argument in argv:
        text = str(argument or "").strip()
        lowered = text.lower()
        if not lowered.startswith("-"):
            continue
        flag = lowered.split("=", 1)[0]
        if flag in DANGEROUS_ARG_FLAGS:
            raise MCPSecurityError("mcp_command_inline_code_forbidden", flag)
        if _SHORT_FLAG_CLUSTER.match(lowered) and any(ch in lowered for ch in "cepir"):
            raise MCPSecurityError("mcp_command_inline_code_forbidden", lowered)



def validate_command(
    command: str,
    *,
    allowlist: Iterable[str] = (),
    extra_env_denied: Sequence[str] = (),
    check_executable: bool = True,
) -> str:
    """Validate a stdio command: trusted launcher only, no shell, no inline code.

    Checks, in order: shell metacharacters, argv shape, the launcher's *resolved* real path
    against the interpreter allowlist (symlink/path escape), and dangerous interpreter flags
    (``-c``/``-e``/``--eval``/``-m``-style inline execution and interactive sessions).
    """
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
    validate_exec_arguments(argv[1:])
    for denied in extra_env_denied:
        if not str(denied).strip():
            raise MCPSecurityError("mcp_command_env_denied_invalid")
    if check_executable:
        resolve_executable(argv[0], allowlist=trusted)
    return target


def is_sensitive_env_key(key: str) -> bool:
    """True for variables that carry a secret and must never reach a child process."""
    name = str(key or "").strip().upper()
    if not name:
        return True
    if name in _SENSITIVE_ENV_NAMES:
        return True
    if any(name.startswith(prefix) for prefix in SENSITIVE_ENV_PREFIXES):
        return True
    return any(name.endswith(suffix) for suffix in _SENSITIVE_ENV_SUFFIXES)


def sanitize_child_env(
    env: Optional[Mapping[str, str]] = None,
    *,
    extra_allow: Iterable[str] = (),
    base: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build the environment of a server process from a whitelist (FIX-02 P1).

    With ``env=None`` the process inherits only :data:`SAFE_ENV_KEYS` from the current
    environment.  An explicit ``env`` may add variables, but every key is still filtered:\n    ``OPENAI_API_KEY``, ``GITHUB_TOKEN``, ``AWS_*``, ``DATABASE_URL`` and the credential /
    approval secrets of this service never reach a third-party MCP server.
    """
    allow = {str(item).strip().upper() for item in extra_allow if str(item).strip()}
    allowed_keys = SAFE_ENV_KEYS | allow
    source = base if base is not None else os.environ
    result: Dict[str, str] = {}
    for key in allowed_keys:
        value = source.get(key)
        if value is not None and not is_sensitive_env_key(key):
            result[key] = str(value)
    for key, value in (env or {}).items():
        name = str(key or "").strip()
        if not name or is_sensitive_env_key(name):
            continue
        if name.upper() not in allowed_keys:
            continue
        result[name] = str(value)
    result.setdefault("PATH", source.get("PATH") or "/usr/local/bin:/usr/bin:/bin")
    return result


def limit_child_process(
    *,
    cpu_seconds: Optional[int] = None,
    address_space_bytes: Optional[int] = None,
    max_open_files: Optional[int] = None,
) -> Callable[[], None]:
    """Return a ``preexec_fn`` that caps a child process (POSIX only, no-op elsewhere)."""
    def _apply() -> None:  # pragma: no cover - runs in the forked child
        try:
            import resource
        except ImportError:
            return
        for name, limit in (
            ("RLIMIT_CPU", cpu_seconds),
            ("RLIMIT_AS", address_space_bytes),
            ("RLIMIT_NOFILE", max_open_files),
            ("RLIMIT_CORE", 0),
        ):
            which = getattr(resource, name, None)
            if which is None or limit is None:
                continue
            try:
                soft, hard = resource.getrlimit(which)
                target = int(limit)
                if hard != resource.RLIM_INFINITY:
                    target = min(target, hard)
                resource.setrlimit(which, (target, hard))
            except (ValueError, OSError):  # pragma: no cover - best effort per limit
                continue

    return _apply


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
