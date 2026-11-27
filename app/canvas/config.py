"""Canvas settings.

Canvas keeps its own settings module (``AGCORE_CANVAS_*``) so R2 Canvas work never edits the
shared ``app/config.py`` that the R1.3 runtime and Workspace/RAG engineer also depend on.
Every value is read from the environment at call time, which also lets tests point the engine at
a throw-away directory without touching the repository defaults.

Feature isolation (WO-C17 / C-FIX-05)
-------------------------------------
* ``AGCORE_CANVAS_ENABLED`` defaults to **false**. The Canvas API answers ``503 canvas_disabled``
  and the tool layer refuses with the same code until an operator turns it on explicitly, so
  merging this branch into an operating service never activates the feature by itself.
* ``AGCORE_CANVAS_DEV_PREVIEW`` is a **separate** switch for the development-only surface (dev
  workspace provider, deterministic dev edit adapter, simulated generation stream). It is off by
  default, can never be on in production, and is reported separately from ``enabled`` in
  ``/capabilities``.
* ``AGCORE_CANVAS_DEV_ADAPTERS`` stays as a second, independent gate on the same dev adapters so an
  older deployment that already pins it to ``false`` keeps its behaviour.

Storage defaults to a **development** location (``<repo>/data/canvas``). It is deliberately not
the production AGCore data directory; production activation is an operator decision that is out
of scope for R2 Canvas (see ``docs/r2/CANVAS_BACKEND_HANDOVER.md``).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROD_DATA_ROOT = Path("/var/lib/hd2lab-agcore")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def environment() -> str:
    return (os.environ.get("AGCORE_ENV", "development") or "development").strip().lower()


def is_production() -> bool:
    return environment() == "production"


def _default_data_dir() -> Path:
    explicit = (os.environ.get("AGCORE_CANVAS_DATA_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    if os.access(str(_PROD_DATA_ROOT), os.W_OK):
        return _PROD_DATA_ROOT / "canvas"
    return _REPO_ROOT / "data" / "canvas"


class CanvasSettings(BaseModel):
    """Effective Canvas configuration for one engine instance."""

    # Feature isolation (WO-C17 / C-FIX-05)
    enabled: bool = False
    dev_preview: bool = False
    data_dir: Path
    db_path: Path
    blob_root: Path

    # Resource limits (WO-C03 / WO-C14)
    max_content_bytes: int = Field(default=512 * 1024, ge=1024)
    max_revisions: int = Field(default=100, ge=2)
    retention_min_revisions: int = Field(default=20, ge=2)
    max_title_chars: int = Field(default=200, ge=1)
    max_list_limit: int = Field(default=100, ge=1)
    default_page_limit: int = Field(default=25, ge=1)
    max_diff_lines: int = Field(default=2000, ge=20)
    max_context_tokens: int = Field(default=3000, ge=128)
    max_list_scan: int = Field(default=1000, ge=1)

    # Adapters (WO-C07 / WO-C08 / Shared contract)
    workspace_provider: str = "dev"  # dev | workspace | unavailable
    ai_edit_adapter: str = "dev"  # dev | runtime
    dev_adapters_enabled: bool = False
    environment: str = "development"

    @property
    def production(self) -> bool:
        return self.environment == "production"

    @property
    def effective_min_retained_revisions(self) -> int:
        """The retention window is never the whole cap: at least one revision can be pruned."""
        return max(1, min(int(self.retention_min_revisions), int(self.max_revisions) - 1))

    def activation(self) -> dict:
        """Machine-readable activation state for ``/capabilities`` and the handover document."""
        return {
            "feature_enabled": self.enabled,
            "feature_flag": "AGCORE_CANVAS_ENABLED",
            "feature_flag_default": False,
            "dev_preview": self.dev_preview,
            "dev_preview_flag": "AGCORE_CANVAS_DEV_PREVIEW",
            "dev_preview_allowed": self.dev_preview and not self.production,
            "dev_adapters_enabled": self.dev_adapters_enabled,
            "api_gate": "503 canvas_disabled when the feature flag is off",
            "tool_gate": "AGCORE_CANVAS_TOOLS_ENABLED (separate, default off)",
            "auto_activation": False,
        }

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def load_canvas_settings(data_dir: Optional[Path] = None, db_path: Optional[Path] = None) -> CanvasSettings:
    """Resolve settings from the environment (tests may pass explicit paths)."""
    env = environment()
    prod = env == "production"
    root = Path(data_dir) if data_dir is not None else _default_data_dir()
    db = Path(db_path) if db_path is not None else Path(
        (os.environ.get("AGCORE_CANVAS_DB_PATH") or "").strip() or str(root / "canvas.db")
    )

    workspace_provider = (os.environ.get("AGCORE_CANVAS_WORKSPACE_PROVIDER") or "dev").strip().lower()
    if workspace_provider not in ("dev", "workspace", "unavailable"):
        workspace_provider = "unavailable"
    ai_adapter = (os.environ.get("AGCORE_CANVAS_AI_ADAPTER") or "dev").strip().lower()
    if ai_adapter not in ("dev", "runtime"):
        ai_adapter = "runtime"

    # WO-C17 / C-FIX-05: the dev preview gate is separate from the feature flag, and the legacy
    # dev-adapter flag is honoured as a second switch that can only make the surface smaller.
    dev_preview = _env_bool("AGCORE_CANVAS_DEV_PREVIEW", False)
    dev_adapters = dev_preview and _env_bool("AGCORE_CANVAS_DEV_ADAPTERS", True)

    max_revisions = max(2, _env_int("AGCORE_CANVAS_MAX_REVISIONS", 100))

    if prod:
        # WO-C02 / WO-C14: a development adapter must never be the authorization path in production.
        dev_preview = False
        dev_adapters = False
        if workspace_provider == "dev":
            workspace_provider = "workspace"
        if ai_adapter == "dev":
            ai_adapter = "runtime"

    return CanvasSettings(
        enabled=_env_bool("AGCORE_CANVAS_ENABLED", False),
        dev_preview=dev_preview,
        data_dir=root,
        db_path=db,
        blob_root=root / "blobs",
        max_content_bytes=_env_int("AGCORE_CANVAS_MAX_CONTENT_BYTES", 512 * 1024),
        max_revisions=max_revisions,
        retention_min_revisions=max(2, _env_int("AGCORE_CANVAS_RETENTION_MIN_REVISIONS", 20)),
        max_title_chars=_env_int("AGCORE_CANVAS_MAX_TITLE_CHARS", 200),
        max_list_limit=_env_int("AGCORE_CANVAS_MAX_LIST_LIMIT", 100),
        default_page_limit=_env_int("AGCORE_CANVAS_DEFAULT_PAGE_LIMIT", 25),
        max_diff_lines=_env_int("AGCORE_CANVAS_MAX_DIFF_LINES", 2000),
        max_context_tokens=_env_int("AGCORE_CANVAS_CONTEXT_TOKENS", 3000),
        max_list_scan=_env_int("AGCORE_CANVAS_MAX_LIST_SCAN", 1000),
        workspace_provider=workspace_provider,
        ai_edit_adapter=ai_adapter,
        dev_adapters_enabled=dev_adapters,
        environment=env,
    )
