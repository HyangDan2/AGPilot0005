"""R2-W workspace configuration (independent from the R1.3 runtime settings)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_ENV_PREFIX = "AGCORE_WORKSPACE_"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(_ENV_PREFIX + name, default)


def _env_int(name: str, default: int) -> int:
    raw = _env(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_state_dir() -> Path:
    for candidate in (Path("/var/lib/hd2lab-agcore"), _DEFAULT_DATA_DIR):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            if os.access(candidate, os.W_OK):
                return candidate
        except OSError:
            continue
    return _DEFAULT_DATA_DIR


@dataclass
class WorkspaceSettings:
    """Workspace engine settings. All values are env-overridable (`AGCORE_WORKSPACE_*`)."""

    enabled: bool = field(default_factory=lambda: _env_bool("ENABLED", True))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", ""))
    storage_root: str = field(default_factory=lambda: _env("STORAGE_ROOT", ""))

    # Limits
    max_file_bytes: int = field(default_factory=lambda: _env_int("MAX_FILE_BYTES", 50 * 1024 * 1024))
    max_workspace_bytes: int = field(default_factory=lambda: _env_int("MAX_WORKSPACE_BYTES", 512 * 1024 * 1024))
    max_files_per_workspace: int = field(default_factory=lambda: _env_int("MAX_FILES_PER_WORKSPACE", 5000))
    max_workspaces_per_owner: int = field(default_factory=lambda: _env_int("MAX_WORKSPACES_PER_OWNER", 50))
    max_folders_per_workspace: int = field(default_factory=lambda: _env_int("MAX_FOLDERS_PER_WORKSPACE", 1000))
    upload_chunk_bytes: int = field(default_factory=lambda: _env_int("UPLOAD_CHUNK_BYTES", 1024 * 1024))
    sqlite_busy_timeout_ms: int = field(default_factory=lambda: _env_int("SQLITE_BUSY_TIMEOUT_MS", 5000))

    # Isolation
    service_isolation: bool = field(default_factory=lambda: _env_bool("SERVICE_ISOLATION", True))

    # Blocked upload types (no archive auto-extraction / no execution in scope)
    blocked_extensions: tuple = field(
        default_factory=lambda: tuple(
            e.strip().lower()
            for e in _env(
                "BLOCKED_EXTENSIONS",
                ".exe,.dll,.so,.dylib,.bat,.cmd,.com,.msi,.scr,.ps1,.sh,.jar,.app,.apk,.class",
            ).split(",")
            if e.strip()
        )
    )

    # Chunking
    chunk_target_tokens: int = field(default_factory=lambda: _env_int("CHUNK_TARGET_TOKENS", 768))
    chunk_max_tokens: int = field(default_factory=lambda: _env_int("CHUNK_MAX_TOKENS", 1024))
    chunk_overlap_tokens: int = field(default_factory=lambda: _env_int("CHUNK_OVERLAP_TOKENS", 96))

    # Indexing queue
    index_max_pending: int = field(default_factory=lambda: _env_int("INDEX_MAX_PENDING", 200))
    index_max_concurrency: int = field(default_factory=lambda: _env_int("INDEX_MAX_CONCURRENCY", 2))
    index_max_attempts: int = field(default_factory=lambda: _env_int("INDEX_MAX_ATTEMPTS", 3))
    index_job_timeout_seconds: float = field(default_factory=lambda: _env_float("INDEX_JOB_TIMEOUT_SECONDS", 300.0))
    index_max_chunks_per_file: int = field(default_factory=lambda: _env_int("INDEX_MAX_CHUNKS_PER_FILE", 5000))

    # Document parser (AGTools / AGPilot0006) — reuse, never reimplement
    agtools_enabled: bool = field(
        default_factory=lambda: _env_bool("AGTOOLS_ENABLED", os.environ.get("AGCORE_AGTOOLS_ENABLED", "true").lower() in {"1", "true", "yes"})
    )
    agtools_url: str = field(
        default_factory=lambda: _env("AGTOOLS_URL", os.environ.get("AGCORE_AGTOOLS_URL", "http://127.0.0.1:8006/api/v1"))
    )
    agtools_token: str = field(
        default_factory=lambda: _env("AGTOOLS_TOKEN", os.environ.get("AGCORE_AGTOOLS_TOKEN", ""))
    )
    agtools_timeout_seconds: float = field(
        default_factory=lambda: _env_float("AGTOOLS_TIMEOUT_SECONDS", float(os.environ.get("AGCORE_AGTOOLS_TIMEOUT_SECONDS", "20") or 20))
    )
    parser_poll_interval_seconds: float = field(default_factory=lambda: _env_float("PARSER_POLL_INTERVAL_SECONDS", 0.5))
    parser_max_wait_seconds: float = field(default_factory=lambda: _env_float("PARSER_MAX_WAIT_SECONDS", 240.0))
    parser_chunk_fetch_concurrency: int = field(default_factory=lambda: _env_int("PARSER_CHUNK_FETCH_CONCURRENCY", 8))

    # Embeddings
    embedding_provider: str = field(default_factory=lambda: _env("EMBEDDING_PROVIDER", "auto"))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "local-hash-v1"))
    embedding_dimension: int = field(default_factory=lambda: _env_int("EMBEDDING_DIMENSION", 512))
    embedding_batch_size: int = field(default_factory=lambda: _env_int("EMBEDDING_BATCH_SIZE", 32))
    embedding_max_input_tokens: int = field(default_factory=lambda: _env_int("EMBEDDING_MAX_INPUT_TOKENS", 8000))
    embedding_timeout_seconds: float = field(default_factory=lambda: _env_float("EMBEDDING_TIMEOUT_SECONDS", 15.0))
    embedding_url: str = field(default_factory=lambda: _env("EMBEDDING_URL", ""))
    embedding_api_key: str = field(default_factory=lambda: _env("EMBEDDING_API_KEY", ""))
    embedding_remote_model: str = field(default_factory=lambda: _env("EMBEDDING_REMOTE_MODEL", "text-embedding-3-small"))
    embedding_auto_index: bool = field(default_factory=lambda: _env_bool("EMBEDDING_AUTO_INDEX", True))

    # Retrieval / context
    search_default_top_k: int = field(default_factory=lambda: _env_int("SEARCH_DEFAULT_TOP_K", 8))
    search_max_top_k: int = field(default_factory=lambda: _env_int("SEARCH_MAX_TOP_K", 50))
    search_rrf_k: int = field(default_factory=lambda: _env_int("SEARCH_RRF_K", 60))
    search_min_score: float = field(default_factory=lambda: _env_float("SEARCH_MIN_SCORE", 0.0))
    context_budget_tokens: int = field(
        default_factory=lambda: _env_int(
            "CONTEXT_BUDGET_TOKENS", int(os.environ.get("AGCORE_CONTEXT_BUDGET_TOKENS", "4096") or 4096)
        )
    )
    context_max_excerpt_tokens: int = field(default_factory=lambda: _env_int("CONTEXT_MAX_EXCERPT_TOKENS", 600))

    # Number of content revisions kept on disk per file (older blobs are pruned)
    revision_keep: int = field(default_factory=lambda: _env_int("REVISION_KEEP", 3))

    # Event log retention
    max_events_per_workspace: int = field(default_factory=lambda: _env_int("MAX_EVENTS_PER_WORKSPACE", 5000))

    def state_dir(self) -> Path:
        return _default_state_dir()

    def resolved_db_path(self) -> Path:
        if self.db_path:
            return Path(self.db_path).expanduser()
        return self.state_dir() / "workspace.db"

    def resolved_storage_root(self) -> Path:
        if self.storage_root:
            return Path(self.storage_root).expanduser()
        return self.state_dir() / "workspace-storage"

    @classmethod
    def for_testing(cls, tmp_path, **overrides) -> "WorkspaceSettings":
        base = tmp_path if isinstance(tmp_path, Path) else Path(tmp_path)
        values = dict(
            db_path=str(base / "workspace.db"),
            storage_root=str(base / "storage"),
            max_file_bytes=1024 * 1024,
            max_workspace_bytes=4 * 1024 * 1024,
            max_files_per_workspace=50,
            max_workspaces_per_owner=10,
            embedding_provider="local",
            agtools_enabled=False,
            parser_poll_interval_seconds=0.01,
            parser_max_wait_seconds=5.0,
        )
        values.update(overrides)
        return cls(**values)


_SETTINGS: Optional[WorkspaceSettings] = None


def get_settings() -> WorkspaceSettings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = WorkspaceSettings()
    return _SETTINGS


def reset_settings_cache() -> None:
    """Testing helper: drop the cached settings so env changes are visible."""

    global _SETTINGS
    _SETTINGS = None
