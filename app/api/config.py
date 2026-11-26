"""Agent Runtime Configuration Endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from app.core.auth import require_admin_caller
from app.core.runtime_config import (
    AgentRuntimeConfig,
    RouteTokenBudget,
    get_runtime_config,
    save_runtime_config,
    customize_route_budget,
    reset_route_budget,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_AGENT_STEPS,
    DEFAULT_RUN_TIMEOUT_SECONDS,
)

router = APIRouter(prefix="/config/agent", tags=["Config"], dependencies=[Depends(require_admin_caller)])


class UpdateAgentRuntimeConfigRequest(BaseModel):
    max_tool_calls: Optional[int] = Field(default=None, ge=1, le=32)
    max_agent_steps: Optional[int] = Field(default=None, ge=1, le=40)
    run_timeout_seconds: Optional[float] = Field(default=None, ge=30.0, le=600.0)


class UpdateRouteTokenBudgetRequest(BaseModel):
    input_budget_tokens: Optional[int] = Field(default=None, ge=1024, le=1048576)
    output_ceiling_tokens: Optional[int] = Field(default=None, ge=128, le=262144)
    plan_max_tokens: Optional[int] = Field(default=None, ge=256, le=32768)
    verify_max_tokens: Optional[int] = Field(default=None, ge=256, le=32768)
    write_max_tokens: Optional[int] = Field(default=None, ge=512, le=131072)
    compose_max_tokens: Optional[int] = Field(default=None, ge=512, le=131072)
    reasoning_start_tokens: Optional[int] = Field(default=None, ge=512, le=131072)
    batch_prompt_bytes: Optional[int] = Field(default=None, ge=4000, le=120000)


@router.get("", response_model=AgentRuntimeConfig)
async def get_agent_config():
    """Returns currently active dynamic Agent runtime limits."""
    return get_runtime_config()


@router.patch("", response_model=AgentRuntimeConfig)
async def patch_agent_config(payload: UpdateAgentRuntimeConfigRequest):
    """Updates dynamic Agent runtime limits (hot reload without service restarts)."""
    current = get_runtime_config()
    updated = AgentRuntimeConfig(
        max_tool_calls=payload.max_tool_calls if payload.max_tool_calls is not None else current.max_tool_calls,
        max_agent_steps=payload.max_agent_steps if payload.max_agent_steps is not None else current.max_agent_steps,
        run_timeout_seconds=payload.run_timeout_seconds if payload.run_timeout_seconds is not None else current.run_timeout_seconds,
        token_budgets=current.token_budgets,
    )
    return save_runtime_config(updated)


@router.patch("/tokens/{route}", response_model=RouteTokenBudget)
async def patch_route_tokens(route: str, payload: UpdateRouteTokenBudgetRequest):
    """Customize token budgets for a route (preserved as long as Gateway contract stays unchanged)."""
    return customize_route_budget(route, payload.model_dump(exclude_none=True))


@router.post("/tokens/{route}/reset", response_model=RouteTokenBudget)
async def reset_route_tokens(route: str):
    """Reset route token budgets back to Gateway-contract recommended defaults."""
    return reset_route_budget(route)


@router.post("/reset", response_model=AgentRuntimeConfig)
async def reset_agent_config():
    """Resets Agent runtime limits to default policy (16 tools, 20 steps, 180s timeout)."""
    current = get_runtime_config()
    default_config = AgentRuntimeConfig(
        max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
        max_agent_steps=DEFAULT_MAX_AGENT_STEPS,
        run_timeout_seconds=DEFAULT_RUN_TIMEOUT_SECONDS,
        token_budgets=current.token_budgets,
    )
    return save_runtime_config(default_config)
