"""Agent Runtime Configuration Endpoints."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from app.core.runtime_config import (
    AgentRuntimeConfig,
    get_runtime_config,
    save_runtime_config,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_AGENT_STEPS,
    DEFAULT_RUN_TIMEOUT_SECONDS,
)

router = APIRouter(prefix="/config/agent", tags=["Config"])


class UpdateAgentRuntimeConfigRequest(BaseModel):
    max_tool_calls: Optional[int] = Field(default=None, ge=1, le=32)
    max_agent_steps: Optional[int] = Field(default=None, ge=1, le=40)
    run_timeout_seconds: Optional[float] = Field(default=None, ge=30.0, le=600.0)


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
    )
    return save_runtime_config(updated)


@router.post("/reset", response_model=AgentRuntimeConfig)
async def reset_agent_config():
    """Resets Agent runtime limits to default policy (16 tools, 20 steps, 180s timeout)."""
    default_config = AgentRuntimeConfig(
        max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
        max_agent_steps=DEFAULT_MAX_AGENT_STEPS,
        run_timeout_seconds=DEFAULT_RUN_TIMEOUT_SECONDS,
    )
    return save_runtime_config(default_config)
