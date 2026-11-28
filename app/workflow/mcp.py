"""WP-06 MCP Client.

An MCP server is untrusted by default.  This module therefore does four things at once:

* **Lifecycle** — ``registered -> connected -> degraded -> closed`` with ``health_check``
  and bounded retries (:class:`MCPClientManager`).
* **Normalisation** — discovered tools are translated into
  :class:`~app.workflow.registry.UnifiedToolSpec` and registered under the
  ``mcp.<server_id>.<tool>`` namespace so the planner sees one catalogue.
* **Allowlist** — an empty ``allowed_tools`` list means *no tool is exposed*.  Anything
  not listed is reported as blocked instead of being registered.
* **Isolation** — credentials live only in :class:`CredentialStore` (referenced by
  ``credential_ref``, never serialised), and every response body is tagged as untrusted
  data by the prompt-injection guard before it can reach a prompt.

Transport clients are injected (``client_factory``).  The default factory returns a stub
that reports the MCP runtime as unavailable, so importing this module never opens a
socket.
"""
from __future__ import annotations

import inspect
import json
import re
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.workflow.registry import UnifiedToolRegistry, UnifiedToolSpec
from app.workflow.safety import ExecutionContext, PromptInjectionGuard

__all__ = [
    "MCPTransport",
    "MCPState",
    "MCPServerConfig",
    "MCPToolDefinition",
    "MCPResource",
    "MCPPrompt",
    "MCPDiscoveryReport",
    "MCPToolResult",
    "MCPHealth",
    "MCPError",
    "MCPConfigError",
    "MCPRegistrationError",
    "MCPUnavailable",
    "MCPToolDenied",
    "MCPApprovalRequired",
    "MCPClient",
    "MCPClientFactory",
    "CredentialStore",
    "MCPClientManager",
    "default_client_factory",
]

MCPTransport = str
_SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
_TOOL_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,95}$")
_INLINE_SECRET = re.compile(r"(?:bearer\s+[A-Za-z0-9\-._~+/]{12,}|^sk-[A-Za-z0-9_\-]{8,}$|^ghp_[A-Za-z0-9]{16,}$)", re.I)


class MCPState(str, Enum):
    REGISTERED = "registered"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    CLOSED = "closed"


class MCPError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


class MCPConfigError(MCPError, ValueError):
    pass


class MCPRegistrationError(MCPError):
    pass


class MCPUnavailable(MCPError):
    pass


class MCPToolDenied(MCPError, PermissionError):
    pass


class MCPApprovalRequired(MCPError, PermissionError):
    pass


class MCPServerConfig(BaseModel):
    """Declaration of one MCP server.  Holds a credential *reference*, never a secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    server_id: str
    transport: MCPTransport = "streamable_http"
    command_or_url: str
    allowed_tools: Tuple[str, ...] = ()
    timeout_seconds: float = 15.0
    max_retries: int = 2
    credential_ref: Optional[str] = None
    description: str = ""

    @field_validator("server_id")
    @classmethod
    def _server_id(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if not _SERVER_ID.match(cleaned):
            raise ValueError("invalid_mcp_server_id")
        return cleaned

    @field_validator("transport")
    @classmethod
    def _transport(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in ("stdio", "streamable_http"):
            raise ValueError("invalid_mcp_transport")
        return normalized

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def _allowed_tools(cls, value: Any) -> Tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(item) for item in value)

    @field_validator("credential_ref")
    @classmethod
    def _credential_ref(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if _INLINE_SECRET.search(cleaned):
            raise ValueError("inline_credential_rejected")
        return cleaned

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout(cls, value: float) -> float:
        if not (0 < float(value) <= 300):
            raise ValueError("invalid_mcp_timeout")
        return float(value)

    @field_validator("max_retries")
    @classmethod
    def _retries(cls, value: int) -> int:
        if int(value) < 0 or int(value) > 10:
            raise ValueError("invalid_mcp_retries")
        return int(value)

    @model_validator(mode="after")
    def _target_matches_transport(self) -> "MCPServerConfig":
        target = (self.command_or_url or "").strip()
        if not target:
            raise ValueError("mcp_target_required")
        if self.transport == "streamable_http" and not target.startswith(("http://", "https://")):
            raise ValueError("mcp_http_url_required")
        if self.transport == "stdio" and target.startswith(("http://", "https://")):
            raise ValueError("mcp_stdio_command_required")
        return self


class MCPToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    annotations: Dict[str, Any] = Field(default_factory=dict)


class MCPResource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    server_id: str
    uri: str
    name: str = ""
    mime_type: Optional[str] = None


class MCPPrompt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    server_id: str
    name: str
    description: str = ""
    arguments: Tuple[Dict[str, Any], ...] = ()


class MCPDiscoveryReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    server_id: str
    registered: Tuple[str, ...] = ()
    blocked: Tuple[str, ...] = ()
    reasons: Dict[str, str] = Field(default_factory=dict)

    @property
    def tool_count(self) -> int:
        return len(self.registered)


class MCPToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    server_id: str
    tool: str
    content: Any = None
    tagged: str = ""
    untrusted: bool = True
    suspicious: bool = False
    injection_matches: Tuple[str, ...] = ()


class MCPHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    server_id: str
    state: MCPState
    healthy: bool
    attempts: int = 0
    detail: Optional[str] = None


class CredentialStore:
    """In-memory credential references.

    ``get`` is the only way to obtain a secret and is used by the transport layer.
    Everything that can be logged or serialised goes through :meth:`describe` or
    :meth:`redact`.
    """

    def __init__(self) -> None:
        self._secrets: Dict[str, str] = {}

    def put(self, ref: str, secret: str) -> None:
        if not ref or not ref.strip():
            raise MCPConfigError("credential_ref_required")
        if not secret:
            raise MCPConfigError("credential_secret_required")
        self._secrets[ref.strip()] = secret

    def get(self, ref: Optional[str]) -> Optional[str]:
        if not ref:
            return None
        return self._secrets.get(ref.strip())

    def has(self, ref: Optional[str]) -> bool:
        return bool(ref) and ref.strip() in self._secrets

    def drop(self, ref: str) -> None:
        self._secrets.pop(ref.strip(), None)

    def describe(self) -> List[Dict[str, Any]]:
        return [{"credential_ref": ref, "configured": True} for ref in sorted(self._secrets)]

    def redact(self, text: str) -> str:
        cleaned = text or ""
        for secret in self._secrets.values():
            if secret and secret in cleaned:
                cleaned = cleaned.replace(secret, "[REDACTED]")
        return _INLINE_SECRET.sub("[REDACTED]", cleaned)

    def __repr__(self) -> str:  # never expose secrets through repr
        return f"CredentialStore(refs={len(self._secrets)})"

    def __len__(self) -> int:
        return len(self._secrets)


class MCPClient(Protocol):
    async def initialize(self) -> Any: ...
    async def list_tools(self) -> Sequence[Mapping[str, Any]]: ...
    async def list_resources(self) -> Sequence[Mapping[str, Any]]: ...
    async def list_prompts(self) -> Sequence[Mapping[str, Any]]: ...
    async def read_resource(self, uri: str) -> Any: ...
    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


MCPClientFactory = Callable[[MCPServerConfig, Optional[str]], MCPClient]


class _UnavailableClient:
    """Default transport placeholder: reports the MCP runtime as not wired yet."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config

    async def initialize(self) -> Any:
        raise MCPUnavailable("mcp_runtime_client_unavailable", self.config.server_id)

    async def list_tools(self) -> Sequence[Mapping[str, Any]]:
        raise MCPUnavailable("mcp_runtime_client_unavailable", self.config.server_id)

    async def list_resources(self) -> Sequence[Mapping[str, Any]]:
        raise MCPUnavailable("mcp_runtime_client_unavailable", self.config.server_id)

    async def list_prompts(self) -> Sequence[Mapping[str, Any]]:
        raise MCPUnavailable("mcp_runtime_client_unavailable", self.config.server_id)

    async def read_resource(self, uri: str) -> Any:
        raise MCPUnavailable("mcp_runtime_client_unavailable", self.config.server_id)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        raise MCPUnavailable("mcp_runtime_client_unavailable", self.config.server_id)

    async def ping(self) -> bool:
        return False

    async def close(self) -> None:
        return None


def default_client_factory(config: MCPServerConfig, _secret: Optional[str]) -> MCPClient:
    return _UnavailableClient(config)


def _text_of(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(payload)


class MCPClientManager:
    """Owns MCP server lifecycles and keeps the unified registry in sync."""

    def __init__(
        self,
        registry: UnifiedToolRegistry,
        *,
        credentials: Optional[CredentialStore] = None,
        client_factory: Optional[MCPClientFactory] = None,
        injection_guard: Optional[PromptInjectionGuard] = None,
    ) -> None:
        self.registry = registry
        self.credentials = credentials or CredentialStore()
        self._factory = client_factory or default_client_factory
        self._guard = injection_guard or PromptInjectionGuard(on_detection="tag")
        self._configs: Dict[str, MCPServerConfig] = {}
        self._clients: Dict[str, MCPClient] = {}
        self._states: Dict[str, MCPState] = {}
        self._resources: Dict[str, List[MCPResource]] = {}
        self._prompts: Dict[str, List[MCPPrompt]] = {}
        self._tools: Dict[str, List[MCPToolDefinition]] = {}
        self.last_errors: Dict[str, str] = {}

    # -- registration ------------------------------------------------------------------
    def register_server(self, config: MCPServerConfig) -> MCPServerConfig:
        if not isinstance(config, MCPServerConfig):
            raise MCPConfigError("config_must_be_MCPServerConfig")
        if config.server_id in self._configs:
            raise MCPRegistrationError("duplicate_mcp_server", config.server_id)
        if config.credential_ref and not self.credentials.has(config.credential_ref):
            raise MCPConfigError("unknown_credential_ref", config.credential_ref)
        self._configs[config.server_id] = config
        self._states[config.server_id] = MCPState.REGISTERED
        return config

    def unregister_server(self, server_id: str) -> None:
        self._states.pop(server_id, None)
        self._configs.pop(server_id, None)
        client = self._clients.pop(server_id, None)
        if client is not None:
            self._close_client(client)
        self._resources.pop(server_id, None)
        self._prompts.pop(server_id, None)
        self._tools.pop(server_id, None)

    def state(self, server_id: str) -> MCPState:
        return self._states.get(server_id, MCPState.CLOSED)

    def config(self, server_id: str) -> MCPServerConfig:
        config = self._configs.get(server_id)
        if config is None:
            raise MCPRegistrationError("unknown_mcp_server", server_id)
        return config

    def servers(self) -> List[str]:
        return sorted(self._configs)

    def _close_client(self, client: MCPClient) -> None:
        """Best-effort close (called from sync paths)."""
        try:
            result = client.close()
            if inspect.iscoroutine(result):
                # Sync path without a running loop: dispose instead of leaving it unawaited.
                result.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass

    # -- lifecycle ---------------------------------------------------------------------
    async def connect(self, server_id: str) -> MCPState:
        config = self.config(server_id)
        state = self.state(server_id)
        if state == MCPState.CONNECTED and server_id in self._clients:
            return state
        if state == MCPState.CLOSED and server_id in self._clients:
            self._clients.pop(server_id, None)
        secret = self.credentials.get(config.credential_ref)
        last_error = "unavailable"
        attempts = max(1, config.max_retries + 1)
        for attempt in range(attempts):
            try:
                client = self._clients.get(server_id) or self._factory(config, secret)
                await client.initialize()
            except Exception as exc:  # noqa: BLE001 - degrade, never crash the workflow
                last_error = self.credentials.redact(f"{type(exc).__name__}: {exc}")
                self._states[server_id] = MCPState.DEGRADED
                self.last_errors[server_id] = last_error
                continue
            self._clients[server_id] = client
            self._states[server_id] = MCPState.CONNECTED
            self.last_errors.pop(server_id, None)
            return MCPState.CONNECTED
        raise MCPUnavailable("mcp_connect_failed", f"{server_id} ({attempts} attempts, {last_error})")

    async def close(self, server_id: str) -> None:
        client = self._clients.pop(server_id, None)
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        if server_id in self._configs:
            self._states[server_id] = MCPState.CLOSED

    async def close_all(self) -> None:
        for server_id in list(self._clients):
            await self.close(server_id)
        for server_id in list(self._configs):
            self._states[server_id] = MCPState.CLOSED

    async def health_check(self, server_id: str) -> MCPHealth:
        config = self.config(server_id)
        if self.state(server_id) == MCPState.CLOSED:
            return MCPHealth(server_id=server_id, state=MCPState.CLOSED, healthy=False, detail="closed")
        if self.state(server_id) != MCPState.CONNECTED:
            try:
                await self.connect(server_id)
            except MCPUnavailable as exc:
                return MCPHealth(server_id=server_id, state=MCPState.DEGRADED, healthy=False,
                                 attempts=config.max_retries + 1, detail=exc.code)
        client = self._clients.get(server_id)
        attempts = 0
        for attempt in range(config.max_retries + 1):
            attempts = attempt + 1
            try:
                healthy = bool(await client.ping()) if client is not None else False
            except Exception:  # noqa: BLE001
                healthy = False
            if healthy:
                self._states[server_id] = MCPState.CONNECTED
                return MCPHealth(server_id=server_id, state=MCPState.CONNECTED, healthy=True, attempts=attempts)
        self._states[server_id] = MCPState.DEGRADED
        self.last_errors[server_id] = "ping_failed"
        return MCPHealth(server_id=server_id, state=MCPState.DEGRADED, healthy=False, attempts=attempts,
                         detail="ping_failed")

    async def _connected_client(self, server_id: str) -> MCPClient:
        self.config(server_id)
        if self.state(server_id) != MCPState.CONNECTED or server_id not in self._clients:
            await self.connect(server_id)
        return self._clients[server_id]

    # -- discovery ---------------------------------------------------------------------
    async def discover_tools(self, server_id: str) -> MCPDiscoveryReport:
        client = await self._connected_client(server_id)
        config = self.config(server_id)
        try:
            raw_tools = await client.list_tools()
        except Exception as exc:  # noqa: BLE001
            self._states[server_id] = MCPState.DEGRADED
            raise MCPUnavailable("mcp_list_tools_failed", self.credentials.redact(str(exc))) from exc

        registered: List[str] = []
        blocked: List[str] = []
        reasons: Dict[str, str] = {}
        definitions: List[MCPToolDefinition] = []
        allowlist = {name.strip() for name in config.allowed_tools if name.strip()}

        for raw in raw_tools or ():
            name = str((raw or {}).get("name") or "").strip()
            if not name or not _TOOL_NAME.match(name):
                blocked.append(name or "<unnamed>")
                reasons[name or "<unnamed>"] = "invalid_tool_name"
                continue
            if name not in allowlist:
                blocked.append(name)
                reasons[name] = "not_in_allowlist"
                continue
            description = str(raw.get("description") or "")
            # Tool poisoning: an instruction-shaped description would enter the prompt
            # through the schema, so it is quarantined instead of registered.
            if self._guard.is_suspicious(description):
                blocked.append(name)
                reasons[name] = "suspicious_tool_description"
                continue
            definition = MCPToolDefinition(
                name=name,
                description=self._guard.neutralize(description),
                input_schema=dict(raw.get("inputSchema") or raw.get("input_schema") or {"type": "object"}),
                annotations=dict(raw.get("annotations") or {}),
            )
            definitions.append(definition)
            spec = self._to_spec(server_id, definition, config)
            self.registry.register(spec, replace=True)
            registered.append(spec.qualified_name)

        self._tools[server_id] = definitions
        return MCPDiscoveryReport(
            server_id=server_id, registered=tuple(registered), blocked=tuple(blocked), reasons=reasons,
        )

    def _to_spec(self, server_id: str, definition: MCPToolDefinition, config: MCPServerConfig) -> UnifiedToolSpec:
        annotations = definition.annotations or {}
        destructive = bool(annotations.get("destructiveHint", False))
        read_only = bool(annotations.get("readOnlyHint", False)) and not destructive
        side_effect = "destructive" if destructive else ("read" if read_only else "external")
        # Destructive MCP tools always pass through the human approval gate.
        approval = destructive or bool(annotations.get("requiresApproval", False))
        return UnifiedToolSpec(
            name=definition.name,
            namespace=f"mcp.{server_id}",
            version="1.0.0",
            description=definition.description,
            input_schema=definition.input_schema or {"type": "object", "properties": {}},
            output_schema={"type": "object"},
            capability=f"mcp.{server_id}.{definition.name}",
            permission="read" if read_only else "write",
            side_effect=side_effect,
            timeout_seconds=config.timeout_seconds,
            error_contract={"type": "object", "error_codes": ["mcp_error", "mcp_timeout", "mcp_denied"]},
            approval_required=approval,
            source="mcp",
        )

    async def discover_resources(self, server_id: str) -> List[MCPResource]:
        client = await self._connected_client(server_id)
        try:
            raw = await client.list_resources()
        except Exception as exc:  # noqa: BLE001
            self._states[server_id] = MCPState.DEGRADED
            raise MCPUnavailable("mcp_list_resources_failed", self.credentials.redact(str(exc))) from exc
        resources = [
            MCPResource(
                server_id=server_id,
                uri=str(item.get("uri") or ""),
                name=str(item.get("name") or ""),
                mime_type=item.get("mimeType"),
            )
            for item in raw or ()
            if isinstance(item, Mapping) and item.get("uri")
        ]
        self._resources[server_id] = resources
        return resources

    async def discover_prompts(self, server_id: str) -> List[MCPPrompt]:
        client = await self._connected_client(server_id)
        try:
            raw = await client.list_prompts()
        except Exception as exc:  # noqa: BLE001
            self._states[server_id] = MCPState.DEGRADED
            raise MCPUnavailable("mcp_list_prompts_failed", self.credentials.redact(str(exc))) from exc
        prompts = [
            MCPPrompt(
                server_id=server_id,
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                arguments=tuple(dict(arg) for arg in item.get("arguments") or () if isinstance(arg, Mapping)),
            )
            for item in raw or ()
            if isinstance(item, Mapping) and item.get("name")
        ]
        self._prompts[server_id] = prompts
        return prompts

    # -- calls -------------------------------------------------------------------------
    async def read_resource(self, server_id: str, uri: str, *, context: Optional[ExecutionContext] = None) -> MCPToolResult:
        self.config(server_id)
        client = await self._connected_client(server_id)
        try:
            payload = await client.read_resource(uri)
        except Exception as exc:  # noqa: BLE001
            raise MCPUnavailable("mcp_resource_read_failed", self.credentials.redact(str(exc))) from exc
        text = _text_of(payload)
        guarded = self._guard.guard(text, source=f"mcp:{server_id}:{uri}")
        return MCPToolResult(
            server_id=server_id, tool=f"resource:{uri}", content=payload, tagged=guarded.tagged,
            suspicious=guarded.scan.is_suspicious, injection_matches=guarded.scan.matches,
        )

    async def bind_resource(self, server_id: str, uri: str, *, context: Optional[ExecutionContext] = None) -> MCPToolResult:
        """Bind a discovered MCP resource into the workflow as untrusted data."""
        known = self._resources.get(server_id)
        if known is None:
            known = await self.discover_resources(server_id)
        if known and uri not in {resource.uri for resource in known}:
            raise MCPToolDenied("mcp_resource_not_discovered", f"{server_id}:{uri}")
        return await self.read_resource(server_id, uri, context=context)

    async def bind_prompt(
        self,
        server_id: str,
        name: str,
        arguments: Optional[Mapping[str, Any]] = None,
    ) -> MCPToolResult:
        """Bind an MCP prompt template; the rendered text is tagged untrusted."""
        prompts = self._prompts.get(server_id)
        if prompts is None:
            prompts = await self.discover_prompts(server_id)
        if name not in {prompt.name for prompt in prompts}:
            raise MCPToolDenied("mcp_prompt_not_found", f"{server_id}:{name}")
        client = await self._connected_client(server_id)
        getter = getattr(client, "get_prompt", None)
        try:
            payload = await getter(name, dict(arguments or {})) if getter else {"name": name}
        except Exception as exc:  # noqa: BLE001
            raise MCPUnavailable("mcp_prompt_bind_failed", self.credentials.redact(str(exc))) from exc
        text = _text_of(payload)
        guarded = self._guard.guard(text, source=f"mcp:{server_id}:prompt:{name}")
        return MCPToolResult(
            server_id=server_id, tool=f"prompt:{name}", content=payload, tagged=guarded.tagged,
            suspicious=guarded.scan.is_suspicious, injection_matches=guarded.scan.matches,
        )

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        user_approved: bool = False,
        context: Optional[ExecutionContext] = None,
    ) -> MCPToolResult:
        config = self.config(server_id)
        allowlist = {name.strip() for name in config.allowed_tools if name.strip()}
        if tool_name not in allowlist:
            raise MCPToolDenied("mcp_tool_not_allowlisted", f"{server_id}:{tool_name}")
        qualified = f"mcp.{server_id}.{tool_name}"
        spec = self.registry.get(qualified)
        if spec is None:
            raise MCPToolDenied("mcp_tool_not_discovered", qualified)
        if spec.approval_required and not user_approved:
            raise MCPApprovalRequired("mcp_user_approval_required", qualified)
        if context is not None and not isinstance(context, ExecutionContext):
            raise MCPConfigError("invalid_execution_context")

        client = await self._connected_client(server_id)
        try:
            payload = await client.call_tool(tool_name, dict(arguments or {}))
        except Exception as exc:  # noqa: BLE001
            self._states[server_id] = MCPState.DEGRADED
            raise MCPUnavailable("mcp_tool_call_failed", self.credentials.redact(f"{qualified}: {exc}")) from exc

        text = _text_of(payload)
        guarded = self._guard.guard(text, source=f"mcp:{server_id}:{tool_name}")
        return MCPToolResult(
            server_id=server_id, tool=tool_name, content=payload, tagged=guarded.tagged,
            suspicious=guarded.scan.is_suspicious, injection_matches=guarded.scan.matches,
        )

    # -- diagnostics -------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        servers = []
        for server_id, config in sorted(self._configs.items()):
            servers.append({
                "server_id": server_id,
                "transport": config.transport,
                "state": self.state(server_id).value,
                "allowed_tools": list(config.allowed_tools),
                "credential_ref": config.credential_ref,
                "credential_configured": self.credentials.has(config.credential_ref),
                "registered_tools": len(self._tools.get(server_id, ())),
            })
        return {"servers": servers, "credentials": self.credentials.describe()}
