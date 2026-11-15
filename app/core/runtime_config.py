"""Dynamic agent runtime configuration (Max Tool Calls, Steps, Timeout, Contract Token Budgets).
Persists to durable JSON and supports hot reload without restarts.
"""
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

DEFAULT_MAX_TOOL_CALLS = 16
DEFAULT_MAX_AGENT_STEPS = 20
DEFAULT_RUN_TIMEOUT_SECONDS = 180.0
CONTRACT_SYNC_TTL_SECONDS = 30.0

CONFIG_PATH_ENV = os.environ.get("AGCORE_RUNTIME_CONFIG_PATH", "/var/lib/hd2lab-agcore/runtime_config.json")
LOCAL_FALLBACK_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "runtime_config.json"


class GatewayContractSnapshot(BaseModel):
    context_tokens: int = Field(ge=1024)
    max_output_tokens: int = Field(ge=128)
    reasoning_effort: str = "off"
    revision: int = 0
    version: int = 0


class TokenBudgetValues(BaseModel):
    input_budget_tokens: int = Field(ge=1024, le=1048576)
    output_ceiling_tokens: int = Field(ge=128, le=262144)
    plan_max_tokens: int = Field(ge=256, le=32768)
    verify_max_tokens: int = Field(ge=256, le=32768)
    write_max_tokens: int = Field(ge=512, le=131072)
    compose_max_tokens: int = Field(ge=512, le=131072)
    reasoning_start_tokens: int = Field(ge=512, le=131072)
    batch_prompt_bytes: int = Field(ge=4000, le=120000)


class RouteTokenBudget(BaseModel):
    gateway_contract: Optional[GatewayContractSnapshot] = None
    recommended: TokenBudgetValues
    active: TokenBudgetValues
    is_customized: bool = False
    synced_at: float = 0.0


def compute_recommended_budget(context_tokens: int, max_output_tokens: int) -> TokenBudgetValues:
    """Compute recommended AGCore token budgets from AI Gateway contract limits."""
    ctx = max(2048, int(context_tokens))
    out_cap = max(256, min(int(max_output_tokens), ctx - 1024))
    reserve = min(2048, max(512, ctx // 16))
    input_budget = max(1024, ctx - out_cap - reserve)
    return TokenBudgetValues(
        input_budget_tokens=input_budget,
        output_ceiling_tokens=out_cap,
        plan_max_tokens=min(out_cap, 2048),
        verify_max_tokens=min(out_cap, 2048),
        write_max_tokens=min(out_cap, max(4096, out_cap // 4)),
        compose_max_tokens=min(out_cap, max(4096, out_cap // 2)),
        reasoning_start_tokens=min(out_cap, max(8192, out_cap // 2)),
        batch_prompt_bytes=min(48000, max(9000, input_budget)),
    )


def _fallback_budget() -> TokenBudgetValues:
    env_budget = int(os.environ.get("AGCORE_CONTEXT_BUDGET_TOKENS", "12000"))
    # Keep safe legacy fallback when gateway is unreachable.
    est_ctx = max(16384, env_budget + 8192)
    rec = compute_recommended_budget(est_ctx, min(32768, max(4096, env_budget // 2)))
    rec.input_budget_tokens = max(1024, env_budget)
    return rec


class AgentRuntimeConfig(BaseModel):
    max_tool_calls: int = Field(default=DEFAULT_MAX_TOOL_CALLS, ge=1, le=32, description="최대 허용 도구 호출 수")
    max_agent_steps: int = Field(default=DEFAULT_MAX_AGENT_STEPS, ge=1, le=40, description="최대 에이전트 단계 수")
    run_timeout_seconds: float = Field(default=DEFAULT_RUN_TIMEOUT_SECONDS, ge=30.0, le=600.0, description="실행 제한 시간(초)")
    token_budgets: Dict[str, RouteTokenBudget] = Field(default_factory=dict, description="AI Gateway 컨트랙트 연동 라우트별 토큰 예산")


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


def _clamp_to_contract(values: TokenBudgetValues, contract: Optional[GatewayContractSnapshot]) -> TokenBudgetValues:
    if not contract:
        return values
    max_in = max(1024, contract.context_tokens - 256)
    max_out = max(128, contract.max_output_tokens)
    return TokenBudgetValues(
        input_budget_tokens=min(values.input_budget_tokens, max_in),
        output_ceiling_tokens=min(values.output_ceiling_tokens, max_out),
        plan_max_tokens=min(values.plan_max_tokens, max_out),
        verify_max_tokens=min(values.verify_max_tokens, max_out),
        write_max_tokens=min(values.write_max_tokens, max_out),
        compose_max_tokens=min(values.compose_max_tokens, max_out),
        reasoning_start_tokens=min(values.reasoning_start_tokens, max_out),
        batch_prompt_bytes=values.batch_prompt_bytes,
    )


def sync_route_contract(route: str, gateway_limits: Dict[str, Any]) -> RouteTokenBudget:
    """Sync AGCore route token budget with AI Gateway contract.
    - If Gateway contract (context_tokens / max_output_tokens) changed: reset to new recommended defaults.
    - If Gateway contract is unchanged: preserve AGCore custom overrides (clamped to physical ceiling).
    """
    cfg = get_runtime_config()
    snapshot = GatewayContractSnapshot(
        context_tokens=int(gateway_limits["context_tokens"]),
        max_output_tokens=int(gateway_limits["max_output_tokens"]),
        reasoning_effort=str(gateway_limits.get("reasoning_effort") or "off"),
        revision=int(gateway_limits.get("revision") or 0),
        version=int(gateway_limits.get("version") or 0),
    )
    recommended = compute_recommended_budget(snapshot.context_tokens, snapshot.max_output_tokens)
    existing = cfg.token_budgets.get(route)

    contract_changed = (
        existing is None
        or existing.gateway_contract is None
        or existing.gateway_contract.context_tokens != snapshot.context_tokens
        or existing.gateway_contract.max_output_tokens != snapshot.max_output_tokens
    )

    if contract_changed:
        entry = RouteTokenBudget(
            gateway_contract=snapshot,
            recommended=recommended,
            active=recommended.model_copy(),
            is_customized=False,
            synced_at=time.time(),
        )
    else:
        active = _clamp_to_contract(existing.active, snapshot) if existing.is_customized else recommended.model_copy()
        entry = RouteTokenBudget(
            gateway_contract=snapshot,
            recommended=recommended,
            active=active,
            is_customized=existing.is_customized,
            synced_at=time.time(),
        )

    updated = cfg.model_copy(deep=True)
    updated.token_budgets[route] = entry
    save_runtime_config(updated)
    return entry


def customize_route_budget(route: str, overrides: Dict[str, Any]) -> RouteTokenBudget:
    cfg = get_runtime_config()
    existing = cfg.token_budgets.get(route)
    if existing is None:
        base = _fallback_budget()
        existing = RouteTokenBudget(gateway_contract=None, recommended=base, active=base.model_copy(), is_customized=False)
    merged = existing.active.model_copy(update={k: v for k, v in overrides.items() if v is not None})
    clamped = _clamp_to_contract(merged, existing.gateway_contract)
    entry = RouteTokenBudget(
        gateway_contract=existing.gateway_contract,
        recommended=existing.recommended,
        active=clamped,
        is_customized=True,
        synced_at=existing.synced_at,
    )
    updated = cfg.model_copy(deep=True)
    updated.token_budgets[route] = entry
    save_runtime_config(updated)
    return entry


def reset_route_budget(route: str) -> RouteTokenBudget:
    cfg = get_runtime_config()
    existing = cfg.token_budgets.get(route)
    rec = existing.recommended.model_copy() if existing else _fallback_budget()
    entry = RouteTokenBudget(
        gateway_contract=existing.gateway_contract if existing else None,
        recommended=rec,
        active=rec.model_copy(),
        is_customized=False,
        synced_at=existing.synced_at if existing else 0.0,
    )
    updated = cfg.model_copy(deep=True)
    updated.token_budgets[route] = entry
    save_runtime_config(updated)
    return entry


def resolve_route_budget(route: Optional[str] = None, default_route: str = "hd2ai-admin") -> TokenBudgetValues:
    cfg = get_runtime_config()
    if route and route in cfg.token_budgets:
        return cfg.token_budgets[route].active
    if default_route in cfg.token_budgets:
        return cfg.token_budgets[default_route].active
    return _fallback_budget()


async def ensure_route_budget(client: Any, route: Optional[str] = None) -> TokenBudgetValues:
    target = getattr(client, "route", None) or "hd2ai-admin"
    if route and route in get_runtime_config().token_budgets:
        target = route
    existing = get_runtime_config().token_budgets.get(target)
    if existing is None or (time.time() - existing.synced_at) >= CONTRACT_SYNC_TTL_SECONDS:
        getter = getattr(client, "get_route_limits", None)
        if callable(getter):
            limits = await getter(target)
            if limits:
                return sync_route_contract(target, limits).active
    return resolve_route_budget(target, default_route=getattr(client, "route", "hd2ai-admin"))


async def sync_contracts_from_gateway(client: Any, routes: Optional[list[str]] = None) -> None:
    targets = routes or [getattr(client, "route", None) or "hd2ai-admin"]
    getter = getattr(client, "get_route_limits", None)
    if not callable(getter):
        return
    for r in dict.fromkeys(targets):
        limits = await getter(r)
        if limits:
            sync_route_contract(r, limits)


# Load at import
load_runtime_config()
