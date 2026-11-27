"""Workspace/RAG gateway (shared contract consumer).

Canvas must not implement workspace storage, indexing or authorization. It consumes the frozen
R2-W contract (``docs/r2/WORKSPACE_CONTRACT.md``, ``WORKSPACE_CONTRACT_VERSION = r2-w/v1``) through
four operations only: ``check_workspace_access``, ``get_workspace``, ``register_artifact_reference``
and ``list_artifact_references``.

Permission model (WO-C17 / C-FIX-01)
------------------------------------
The R2-W contract answers with one of four levels: ``read`` / ``write`` / ``admin`` / ``none``.
Canvas distinguishes two required levels and never conflates them:

* ``read`` — listing, reading content, revisions, diffs, previews, exports, proposals inspection.
  Satisfied by ``read``, ``write`` or ``admin``.
* ``write`` — creating an artifact *into* a workspace, linking an artifact to a workspace, renaming,
  deleting, patching, restoring, creating/applying/rejecting proposals. Satisfied by ``write`` or
  ``admin`` only. A ``read`` grant can never create or mutate workspace-linked state.

Three providers exist:

* ``RuntimeWorkspaceProvider`` — binds ``app/workspace/contracts.py::WorkspaceProvider`` as soon as
  the Workspace branch is merged. It is imported lazily so this branch builds and tests without it.
* ``LocalDevWorkspaceProvider`` — in-memory development provider. It is **not** an authorization
  layer: every response carries ``provider_mode: "dev"`` and ``enforced: false``, and the provider
  refuses to be constructed in production. Its levels are still honoured by Canvas' level checks so
  the dev preview can exercise a ``read``-only and a revoked workspace.
* ``UnavailableWorkspaceProvider`` — fail-closed default; raises ``workspace_unavailable``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from pydantic import BaseModel, ConfigDict

from app.canvas.errors import CanvasError, CanvasErrorCode

"""Permission ladder, lowest first. A higher level satisfies every lower requirement."""
PERMISSION_LADDER = ("none", "read", "write", "admin")
READ_LEVELS = ("read", "write", "admin")
WRITE_LEVELS = ("write", "admin")
LEVEL_RANK = {level: rank for rank, level in enumerate(PERMISSION_LADDER)}


class WorkspaceAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    level: str = "none"  # none | read | write | admin
    reason: str = ""
    provider_mode: str = "dev"
    enforced: bool = False
    exists: bool = True
    provider_mode_detail: Optional[str] = None

    @property
    def allowed(self) -> bool:
        """Read access (kept for callers written against the pre-WO-C17 contract)."""
        return self.can_read

    @property
    def can_read(self) -> bool:
        return self.level in READ_LEVELS

    @property
    def can_write(self) -> bool:
        return self.level in WRITE_LEVELS

    def satisfies(self, required: str) -> bool:
        required = normalise_required(required)
        held = LEVEL_RANK.get(self.level, 0)
        return held >= LEVEL_RANK[required]


def normalise_required(required: str) -> str:
    value = (required or "read").strip().lower()
    if value not in ("read", "write"):
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            "workspace requirement must be 'read' or 'write'",
            context={"required": required},
        )
    return value


class WorkspaceGateway(Protocol):
    mode: str
    enforced: bool

    def check_access(self, workspace_id: str, scope: Any) -> WorkspaceAccess: ...

    def register_reference(
        self, workspace_id: str, scope: Any, *, artifact_id: str, kind: str = "artifact",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]: ...


class UnavailableWorkspaceProvider:
    """Fail-closed provider used until the Workspace engine is available."""

    mode = "unavailable"
    enforced = True

    def __init__(self, reason: str = "workspace provider is not configured") -> None:
        self.reason = reason

    def check_access(self, workspace_id: str, scope: Any) -> WorkspaceAccess:
        raise CanvasError(
            CanvasErrorCode.WORKSPACE_UNAVAILABLE,
            self.reason,
            retryable=True,
            context={"workspace_id": workspace_id},
        )

    def register_reference(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise CanvasError(
            CanvasErrorCode.WORKSPACE_UNAVAILABLE, self.reason, retryable=True
        )


class LocalDevWorkspaceProvider:
    """Isolated development provider (never production, never an authorization claim)."""

    mode = "dev"
    enforced = False
    DEV_WORKSPACE_ID = "ws_dev00000001"

    def __init__(self, production: bool = False, extra_workspaces: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        if production:
            raise RuntimeError(
                "LocalDevWorkspaceProvider must never run in production; "
                "configure AGCORE_CANVAS_WORKSPACE_PROVIDER=workspace"
            )
        self._workspaces: Dict[str, Dict[str, Any]] = {
            self.DEV_WORKSPACE_ID: {"name": "Canvas Dev Workspace", "level": "admin"},
        }
        for workspace_id, payload in (extra_workspaces or {}).items():
            self._workspaces[workspace_id] = dict(payload)
        self._references: Dict[str, list] = {}

    def add_workspace(self, workspace_id: str, *, name: str = "", level: str = "admin") -> None:
        if level not in LEVEL_RANK:
            raise ValueError(f"unknown workspace level '{level}'")
        self._workspaces[workspace_id] = {"name": name or workspace_id, "level": level}

    def set_level(self, workspace_id: str, level: str) -> None:
        """Simulate a permission change (grant downgrade / revocation) in the dev preview."""
        if level not in LEVEL_RANK:
            raise ValueError(f"unknown workspace level '{level}'")
        record = self._workspaces.setdefault(workspace_id, {"name": workspace_id})
        record["level"] = level

    def check_access(self, workspace_id: str, scope: Any) -> WorkspaceAccess:
        record = self._workspaces.get(workspace_id)
        if record is None:
            return WorkspaceAccess(
                workspace_id=workspace_id,
                level="none",
                reason="dev_adapter_unknown_workspace",
                provider_mode=self.mode,
                enforced=False,
                exists=False,
            )
        return WorkspaceAccess(
            workspace_id=workspace_id,
            level=str(record.get("level") or "admin"),
            reason="dev_adapter_unenforced",
            provider_mode=self.mode,
            enforced=False,
            exists=True,
            provider_mode_detail="Development adapter: access level is not an authorization decision.",
        )

    def register_reference(
        self, workspace_id: str, scope: Any, *, artifact_id: str, kind: str = "artifact",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        access = self.check_access(workspace_id, scope)
        if not access.can_write:
            raise CanvasError(
                CanvasErrorCode.WORKSPACE_ACCESS_DENIED,
                "workspace write access denied",
                context={
                    "workspace_id": workspace_id,
                    "level": access.level,
                    "required_level": "write",
                    "provider_mode": self.mode,
                },
            )
        reference = {
            "artifact_ref_id": f"arf_dev{uuid_suffix()}",
            "workspace_id": workspace_id,
            "artifact_id": artifact_id,
            "kind": kind,
            "metadata": dict(metadata or {}),
            "provider_mode": self.mode,
            "enforced": False,
        }
        self._references.setdefault(workspace_id, []).append(reference)
        return reference

    def list_references(self, workspace_id: str) -> list:
        return list(self._references.get(workspace_id, []))


def uuid_suffix() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


class RuntimeWorkspaceProvider:
    """Binds the frozen R2-W provider implementation (``app/workspace/contracts.py``)."""

    mode = "workspace"
    enforced = True

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    @classmethod
    def try_build(cls) -> Optional["RuntimeWorkspaceProvider"]:
        """Return a bound provider when the Workspace branch is present, else ``None``."""
        try:  # pragma: no cover - depends on the other engineer's branch being merged
            from app.workspace.contracts import WorkspaceProvider  # noqa: F401
        except Exception:
            return None
        factory = getattr(_runtime_provider_factory, "build", None)
        if factory is None:  # pragma: no cover
            return None
        provider = factory()
        return cls(provider) if provider is not None else None

    def check_access(self, workspace_id: str, scope: Any) -> WorkspaceAccess:
        try:
            result = self._provider.check_workspace_access(workspace_id, scope)
        except Exception as exc:
            raise CanvasError(
                CanvasErrorCode.WORKSPACE_UNAVAILABLE,
                "workspace access check failed",
                retryable=True,
                context={"reason": exc.__class__.__name__},
            ) from exc
        level = getattr(result, "level", "none")
        level = getattr(level, "value", level)
        return WorkspaceAccess(
            workspace_id=getattr(result, "workspace_id", workspace_id),
            level=str(level),
            reason=str(getattr(result, "reason", "") or ""),
            provider_mode=self.mode,
            enforced=True,
            exists=str(level) != "none",
        )

    def register_reference(
        self, workspace_id: str, scope: Any, *, artifact_id: str, kind: str = "artifact",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            reference = self._provider.register_artifact_reference(
                workspace_id, scope,
                {"artifact_id": artifact_id, "kind": kind, "metadata": dict(metadata or {})},
            )
        except Exception as exc:
            raise CanvasError(
                CanvasErrorCode.WORKSPACE_UNAVAILABLE,
                "workspace artifact reference registration failed",
                retryable=True,
                context={"reason": exc.__class__.__name__},
            ) from exc
        if hasattr(reference, "model_dump"):
            payload = reference.model_dump()
        elif isinstance(reference, dict):
            payload = dict(reference)
        else:  # pragma: no cover
            payload = {"artifact_id": artifact_id}
        payload.setdefault("provider_mode", self.mode)
        payload.setdefault("enforced", True)
        return payload


class _RuntimeProviderFactory:
    """Indirection so the Workspace owner can register its provider without Canvas importing it."""

    build = None


_runtime_provider_factory = _RuntimeProviderFactory()


def register_runtime_provider_factory(factory) -> None:
    """Integration hook: call with ``lambda: WorkspaceService(...)`` once the branch is merged."""
    _runtime_provider_factory.build = factory


def build_workspace_gateway(settings) -> WorkspaceGateway:
    mode = (settings.workspace_provider or "unavailable").lower()
    if mode == "workspace":
        provider = RuntimeWorkspaceProvider.try_build()
        if provider is not None:
            return provider
        return UnavailableWorkspaceProvider(
            "workspace provider requested but app.workspace is not available on this branch"
        )
    if mode == "dev":
        if settings.production or not settings.dev_preview or not settings.dev_adapters_enabled:
            return UnavailableWorkspaceProvider(
                "development workspace adapter is disabled (set AGCORE_CANVAS_DEV_PREVIEW=true)"
            )
        return LocalDevWorkspaceProvider(production=settings.production)
    return UnavailableWorkspaceProvider()


def default_dev_provider(data_dir: Optional[Path] = None) -> LocalDevWorkspaceProvider:
    """Convenience for tests/tools that need the dev provider explicitly."""
    return LocalDevWorkspaceProvider(production=False)
