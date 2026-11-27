"""Independent R2-W preview entrypoint.

Run:  python -m uvicorn app.workspace.preview_app:app --host 127.0.0.1 --port 8210

This app intentionally does not import the R1.3 runtime, AGPolicy or ActionValidator: the
Workspace preview is isolated from the operating runtime until the integration step listed in
`docs/r2/WORKSPACE_CONTRACT.md` §8.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.workspace.mock import build_mock_router

app = FastAPI(
    title="HD2Lab AGCore R2-W Workspace Preview",
    version="0.1.0",
    description="Workspace & RAG Foundation Preview (independent of the R1.3 runtime)",
)

_origins = [o.strip() for o in os.environ.get("AGCORE_WORKSPACE_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=("*" not in _origins),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(build_mock_router())

try:  # pragma: no cover - present once CP-W01..W04 land
    from app.workspace.api import router as workspace_router

    app.include_router(workspace_router)
except Exception:  # pragma: no cover
    pass


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok", "component": "agcore-r2-workspace-preview", "contract": "r2-w/v1"}
