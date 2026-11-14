"""Dynamic agent runtime configuration (Max Tool Calls, Steps, Timeout).
Persists to durable JSON and supports hot reload without restarts.
"""
import json
import os
from pathlib import Path
from pydantic import BaseModel, Field

DEFAULT_MAX_TOOL_CALLS = 16
DEFAULT_MAX_AGENT_STEPS = 20
DEFAULT_RUN_TIMEOUT_SECONDS = 180.0

CONFIG_PATH_ENV = os.environ.get("AGCORE_RUNTIME_CONFIG_PATH", "/var/lib/hd2lab-agcore/runtime_config.json")
LOCAL_FALLBACK_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "runtime_config.json"


class AgentRuntimeConfig(BaseModel):
    max_tool_calls: int = Field(default=DEFAULT_MAX_TOOL_CALLS, ge=1, le=32, description="최대 허용 도구 호출 수")
    max_agent_steps: int = Field(default=DEFAULT_MAX_AGENT_STEPS, ge=1, le=40, description="최대 에이전트 단계 수")
    run_timeout_seconds: float = Field(default=DEFAULT_RUN_TIMEOUT_SECONDS, ge=30.0, le=600.0, description="실행 제한 시간(초)")


def _resolve_config_path() -> Path:
    target = Path(CONFIG_PATH_ENV)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    except (PermissionError, OSError):
        LOCAL_FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        return LOCAL_FALLBACK_PATH


_ACTIVE_CONFIG = AgentRuntimeConfig()


def load_runtime_config() -> AgentRuntimeConfig:
    global _ACTIVE_CONFIG
    path = _resolve_config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _ACTIVE_CONFIG = AgentRuntimeConfig(**data)
        except Exception:
            pass
    return _ACTIVE_CONFIG


def save_runtime_config(new_config: AgentRuntimeConfig) -> AgentRuntimeConfig:
    global _ACTIVE_CONFIG
    path = _resolve_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(new_config.model_dump_json(indent=2), encoding="utf-8")
        temp.replace(path)
    except Exception as exc:
        print(f"[runtime_config] Warning: Could not persist config to {path}: {exc}")
    _ACTIVE_CONFIG = new_config
    return _ACTIVE_CONFIG


def get_runtime_config() -> AgentRuntimeConfig:
    return _ACTIVE_CONFIG


# Load at import
load_runtime_config()
