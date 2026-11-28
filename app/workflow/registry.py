"""WP-07 Unified Tool Registry.

R3 workflows call tools that live in five different places: AGCore built-ins, AGTools
(R2.1 parsers/writers/RAG), external MCP servers, Skill packs and downstream products.
:class:`UnifiedToolRegistry` is the single catalogue for all of them.

Design notes
------------
* One spec type (:class:`UnifiedToolSpec`) carries everything the planner, the approval
  gate and the budget manager need: schemas, capability, permission, side-effect class,
  timeout, error contract and whether user approval is required.
* Legacy AGCore tools are *bridged*, not copied: :meth:`UnifiedToolRegistry.bridge_legacy`
  reads ``app/tools/registry.py`` and maps each ``ToolRegistration`` to a unified spec.
  The legacy runtime and its policy file stay authoritative for exposure.
* Registration is strict.  A duplicate qualified name raises
  :class:`ToolCollisionError`; a second product implementing a capability that another
  source already owns raises :class:`DuplicateCapabilityError` unless the caller
  explicitly supersedes it.  That is the "제품별 중복 구현 방지" guard.
* Schemas are only returned on request (:meth:`select`) so the context budget never
  carries the full catalogue.
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ToolSource",
    "SideEffect",
    "Permission",
    "ToolCollisionError",
    "DuplicateCapabilityError",
    "ToolNotFound",
    "ToolSpecError",
    "UnifiedToolSpec",
    "ToolRegistrationResult",
    "UnifiedToolRegistry",
]

ToolSource = Literal["builtin", "agtools", "mcp", "skill", "downstream"]
SideEffect = Literal["none", "read", "write", "external", "destructive"]
Permission = Literal["read", "write", "admin"]

_TOOL_SOURCES: Tuple[str, ...] = ("builtin", "agtools", "mcp", "skill", "downstream")
_SIDE_EFFECTS: Tuple[str, ...] = ("none", "read", "write", "external", "destructive")
_PERMISSIONS: Tuple[str, ...] = ("read", "write", "admin")
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")
_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,63}$")
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}([-+][a-zA-Z0-9.\-]+)?$")


class ToolSpecError(ValueError):
    """Invalid tool specification."""


class ToolCollisionError(ToolSpecError):
    """Two tools claim the same qualified name (namespace + name)."""

    def __init__(self, qualified_name: str, existing: "UnifiedToolSpec", incoming: "UnifiedToolSpec") -> None:
        self.qualified_name = qualified_name
        self.existing = existing
        self.incoming = incoming
        super().__init__(
            f"tool_collision: {qualified_name} "
            f"(registered source={existing.source} version={existing.version}, "
            f"incoming source={incoming.source} version={incoming.version})"
        )


class DuplicateCapabilityError(ToolCollisionError):
    """A second product/source implements a capability another source already owns."""

    def __init__(self, capability: str, qualified_name: str, existing: "UnifiedToolSpec", incoming: "UnifiedToolSpec") -> None:
        self.capability = capability
        super().__init__(qualified_name, existing, incoming)
        self.args = (
            f"duplicate_capability: {capability} is already provided by {existing.qualified_name} "
            f"({existing.source}); {incoming.qualified_name} ({incoming.source}) must supersede explicitly",
        )


class ToolNotFound(KeyError):
    def __init__(self, qualified_name: str) -> None:
        self.qualified_name = qualified_name
        super().__init__(f"tool_not_found: {qualified_name}")


class UnifiedToolSpec(BaseModel):
    """Provider-agnostic description of one callable tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    namespace: str
    version: str = "1.0.0"
    description: str = ""
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    capability: str
    permission: Permission = "read"
    side_effect: SideEffect = "none"
    timeout_seconds: float = 30.0
    error_contract: Dict[str, Any] = Field(default_factory=dict)
    approval_required: bool = False
    source: ToolSource = "builtin"
    tags: Tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _NAME_RE.match(value or ""):
            raise ValueError("invalid_tool_name")
        if value.count(".") > 0 and value.split(".")[0] == value:
            raise ValueError("invalid_tool_name")
        return value

    @field_validator("namespace")
    @classmethod
    def _validate_namespace(cls, value: str) -> str:
        if not _NAMESPACE_RE.match(value or ""):
            raise ValueError("invalid_tool_namespace")
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not _VERSION_RE.match(value or ""):
            raise ValueError("invalid_tool_version")
        return value

    @field_validator("capability")
    @classmethod
    def _validate_capability(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned or len(cleaned) > 128:
            raise ValueError("invalid_capability")
        return cleaned

    @field_validator("input_schema")
    @classmethod
    def _validate_input_schema(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("input_schema_must_be_object")
        if value and value.get("type", "object") != "object":
            raise ValueError("input_schema_type_must_be_object")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: float) -> float:
        if value is None or not (0 < float(value) <= 600):
            raise ValueError("invalid_timeout")
        return float(value)

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if value not in _TOOL_SOURCES:
            raise ValueError("invalid_tool_source")
        return value

    @model_validator(mode="after")
    def _consistent_policy(self) -> "UnifiedToolSpec":
        if self.side_effect == "none" and self.permission == "read":
            return self
        # ``external`` + ``read`` is deliberately allowed: a read-only outbound call (web
        # search, remote lookup) crosses the service boundary but persists nothing, so it
        # does not need write authority.  Only state-changing side effects do.
        if self.side_effect in ("write", "destructive") and self.permission == "read":
            raise ValueError("writable_side_effect_requires_write_permission")
        if self.side_effect == "destructive" and not self.approval_required:
            raise ValueError("destructive_tools_require_user_approval")
        return self

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}"

    @property
    def is_read_only(self) -> bool:
        """True only when the tool neither persists nor leaves the process boundary."""
        return self.side_effect in ("none", "read") and self.permission == "read"

    def schema_payload(self) -> Dict[str, Any]:
        """OpenAI-compatible function schema (used for lazy prompt exposure)."""
        return {
            "type": "function",
            "function": {
                "name": self.qualified_name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class ToolRegistrationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    qualified_name: str
    source: ToolSource
    created: bool
    superseded: Optional[str] = None


class UnifiedToolRegistry:
    """Catalogue of every tool an R3 workflow may call."""

    def __init__(self, *, legacy_registry: Any = None, bridge_legacy: bool = True) -> None:
        self._specs: Dict[str, UnifiedToolSpec] = {}
        self._legacy_handlers: Dict[str, Callable[..., Any]] = {}
        self._capability_owner: Dict[str, UnifiedToolSpec] = {}
        self._namespaces: Dict[str, ToolSource] = {}
        self._legacy = legacy_registry
        self._bridge_legacy_enabled = bridge_legacy
        self._bridged = False

    # -- registration ------------------------------------------------------------------
    def register(
        self,
        spec: UnifiedToolSpec,
        *,
        handler: Optional[Callable[..., Any]] = None,
        replace: bool = False,
        supersede: bool = False,
    ) -> ToolRegistrationResult:
        """Register one tool.

        ``replace=True`` swaps the identical qualified name (used by tests/fixtures);
        ``supersede=True`` declares a deliberate override of a capability owned by
        another source (recorded in the result).
        """
        if not isinstance(spec, UnifiedToolSpec):
            raise ToolSpecError("spec_must_be_UnifiedToolSpec")
        qualified = spec.qualified_name

        owner = self._capability_owner.get(spec.capability)
        existing = self._specs.get(qualified)
        superseded: Optional[str] = None

        if existing is not None:
            identical = (
                existing.source == spec.source
                and existing.capability == spec.capability
                and existing.version == spec.version
            )
            if not (replace or supersede) and not identical:
                raise ToolCollisionError(qualified, existing, spec)

        if owner is not None and owner.qualified_name != qualified and not supersede:
            raise DuplicateCapabilityError(spec.capability, qualified, owner, spec)
        if owner is not None and owner.qualified_name != qualified and supersede:
            superseded = owner.qualified_name
            # Only one live implementation per capability: retire the previous tool.
            self._specs.pop(owner.qualified_name, None)
            self._legacy_handlers.pop(owner.qualified_name, None)
            self._capability_owner.pop(spec.capability, None)

        namespace_owner = self._namespaces.get(spec.namespace)
        if namespace_owner is not None and namespace_owner != spec.source and not supersede:
            raise ToolCollisionError(qualified, existing or spec, spec)

        self._specs[qualified] = spec
        self._capability_owner[spec.capability] = spec
        self._namespaces.setdefault(spec.namespace, spec.source)
        if handler is not None:
            if not callable(handler):
                raise ToolSpecError("handler_must_be_callable")
            self._legacy_handlers[qualified] = handler
        return ToolRegistrationResult(
            qualified_name=qualified,
            source=spec.source,
            created=existing is None,
            superseded=superseded,
        )

    def unregister(self, qualified_name: str) -> None:
        spec = self._specs.pop(qualified_name, None)
        self._legacy_handlers.pop(qualified_name, None)
        if spec is not None and self._capability_owner.get(spec.capability) is spec:
            self._capability_owner.pop(spec.capability, None)

    # -- legacy bridge -----------------------------------------------------------------
    def bridge_legacy(self, legacy_registry: Any = None, *, force: bool = False) -> List[UnifiedToolSpec]:
        """Import AGCore's existing ``ToolRegistry`` tools as ``source="builtin"``."""
        if self._bridged and not force:
            return []
        registry = legacy_registry or self._legacy
        if registry is None:
            import importlib

            # ``app.tools.__init__`` re-exports the singleton under the module's name,
            # so the module itself must be fetched through importlib.
            legacy_module = importlib.import_module("app.tools.registry")
            registry = legacy_module.registry
        self._legacy = registry

        bridged: List[UnifiedToolSpec] = []
        for registration in registry.list_tools():
            spec = self._spec_from_registration(registration)
            if spec.qualified_name in self._specs:
                continue
            self.register(spec)
            handler = registry.get_handler(registration.name)
            if handler is not None:
                self._legacy_handlers[spec.qualified_name] = handler
            bridged.append(spec)
        self._bridged = True
        return bridged

    @staticmethod
    def _spec_from_registration(registration: Any) -> UnifiedToolSpec:
        qualified = registration.name
        if "." in qualified:
            namespace, name = qualified.split(".", 1)
        else:
            namespace, name = "builtin", qualified
        read_only = bool(getattr(registration, "is_read_only", True))
        side_effect: SideEffect = "read" if read_only else "write"
        return UnifiedToolSpec(
            name=name,
            namespace=namespace,
            version="1.0.0",
            description=getattr(registration, "description", "") or "",
            input_schema=getattr(registration, "parameters", {}) or {"type": "object", "properties": {}},
            output_schema={"type": "object"},
            capability=qualified,
            permission="read" if read_only else "write",
            side_effect=side_effect,
            timeout_seconds=30.0,
            error_contract={
                "type": "object",
                "error_codes": ["tool_error", "tool_timeout", "tool_denied"],
            },
            approval_required=not read_only,
            source="builtin",
        )

    # -- lookup ------------------------------------------------------------------------
    def _ensure(self) -> None:
        if self._bridge_legacy_enabled and not self._bridged:
            self.bridge_legacy()

    def get(self, qualified_name: str) -> Optional[UnifiedToolSpec]:
        self._ensure()
        return self._specs.get(qualified_name)

    def require(self, qualified_name: str) -> UnifiedToolSpec:
        spec = self.get(qualified_name)
        if spec is None:
            raise ToolNotFound(qualified_name)
        return spec

    def __contains__(self, qualified_name: object) -> bool:
        return self.get(str(qualified_name)) is not None

    def __len__(self) -> int:
        self._ensure()
        return len(self._specs)

    def list_specs(self, *, source: Optional[ToolSource] = None) -> List[UnifiedToolSpec]:
        self._ensure()
        specs = list(self._specs.values())
        if source is not None:
            specs = [spec for spec in specs if spec.source == source]
        return sorted(specs, key=lambda spec: spec.qualified_name)

    def by_namespace(self, namespace: str) -> List[UnifiedToolSpec]:
        self._ensure()
        return [spec for spec in self.list_specs() if spec.namespace == namespace or spec.namespace.startswith(namespace + ".")]

    def by_capability(self, capability: str) -> List[UnifiedToolSpec]:
        self._ensure()
        return [spec for spec in self.list_specs() if spec.capability == capability]

    def by_permission(self, permission: Permission) -> List[UnifiedToolSpec]:
        self._ensure()
        return [spec for spec in self.list_specs() if spec.permission == permission]

    def by_side_effect(self, side_effect: SideEffect) -> List[UnifiedToolSpec]:
        self._ensure()
        return [spec for spec in self.list_specs() if spec.side_effect == side_effect]

    def requiring_approval(self) -> List[UnifiedToolSpec]:
        self._ensure()
        return [spec for spec in self.list_specs() if spec.approval_required]

    def namespaces(self) -> Dict[str, ToolSource]:
        self._ensure()
        return dict(self._namespaces)

    # -- lazy schema exposure ----------------------------------------------------------
    def select(self, qualified_names: Sequence[str]) -> List[UnifiedToolSpec]:
        """Return exactly the requested specs (unknown names are skipped)."""
        self._ensure()
        chosen: List[UnifiedToolSpec] = []
        for name in qualified_names:
            spec = self._specs.get(name)
            if spec is not None:
                chosen.append(spec)
        return chosen

    def schemas_for(self, qualified_names: Sequence[str]) -> List[Dict[str, Any]]:
        return [spec.schema_payload() for spec in self.select(qualified_names)]

    def all_schemas(self) -> List[Dict[str, Any]]:
        return [spec.schema_payload() for spec in self.list_specs()]

    # -- execution ---------------------------------------------------------------------
    def get_handler(self, qualified_name: str) -> Optional[Callable[..., Any]]:
        self._ensure()
        return self._legacy_handlers.get(qualified_name)

    async def execute(
        self,
        qualified_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        context: Any = None,
        granted_permission: Permission = "write",
    ) -> Any:
        """Call a tool through the unified catalogue.

        The registry enforces the declared permission floor; the caller must pass the
        permission it was granted (a read-only workflow cannot call a write tool).
        """
        spec = self.require(qualified_name)
        if _PERMISSIONS.index(spec.permission) > _PERMISSIONS.index(granted_permission):
            raise PermissionError(f"permission_denied: {qualified_name} requires {spec.permission}")
        handler = self._legacy_handlers.get(qualified_name)
        if handler is None:
            raise ToolNotFound(f"{qualified_name} (no handler bound)")
        kwargs = dict(arguments or {})
        result = handler(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    # -- diagnostics -------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        self._ensure()
        counts: Dict[str, int] = {}
        for spec in self._specs.values():
            counts[spec.source] = counts.get(spec.source, 0) + 1
        return {
            "total": len(self._specs),
            "by_source": counts,
            "namespaces": dict(self._namespaces),
            "approval_required": len(self.requiring_approval()),
        }
