"""Independent R2-W preview entrypoint.

Run:  python -m uvicorn app.workspace.preview_app:app --host 127.0.0.1 --port 8210

This app intentionally does not import the R1.3 runtime, AGPolicy or ActionValidator: the
Workspace preview is isolated from the operating runtime until the integration step listed in
`docs/r2/WORKSPACE_CONTRACT.md` §8.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.workspace.api import router as workspace_router
from app.workspace.mock import build_mock_router, is_production_env
from app.workspace.runtime import WorkspaceRuntime, set_runtime


def _assert_preview_allowed() -> None:
    """R2-INT-FIX-03-BE: Block standalone workspace preview app in production."""
    if is_production_env():
        raise RuntimeError(
            "Workspace preview app is restricted to development/test environments (AGCORE_ENV=production)."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_preview_allowed()
    runtime = WorkspaceRuntime.build(start_worker=True)
    set_runtime(runtime)
    app.state.workspace_runtime = runtime
    try:
        yield
    finally:
        runtime.shutdown()


app = FastAPI(
    title="HD2Lab AGCore R2-W Workspace Preview",
    version="0.1.0",
    description="Workspace & RAG Foundation Preview (independent of the R1.3 runtime)",
    lifespan=lifespan,
)


@app.middleware("http")
async def _block_preview_in_production(request: Request, call_next):
    if is_production_env():
        return JSONResponse(
            status_code=403,
            content={
                "detail": {
                    "code": "preview_disabled_in_production",
                    "message": "Workspace preview app is disabled when AGCORE_ENV=production.",
                    "retryable": False,
                }
            },
        )
    return await call_next(request)

_origins = [o.strip() for o in os.environ.get("AGCORE_WORKSPACE_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=("*" not in _origins),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(workspace_router)
app.include_router(build_mock_router())


@app.get("/health", include_in_schema=False)
def health() -> dict:
    from app.workspace.contracts import WORKSPACE_CONTRACT_AMENDMENT, WORKSPACE_CONTRACT_REVISION

    return {
        "status": "ok",
        "component": "agcore-r2-workspace-preview",
        "contract": WORKSPACE_CONTRACT_REVISION,
        "amendment": WORKSPACE_CONTRACT_AMENDMENT,
    }
