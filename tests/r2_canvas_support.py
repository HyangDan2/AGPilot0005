"""Shared helpers for the R2 Canvas test modules.

Not a ``conftest.py`` on purpose: these helpers are imported explicitly so the existing suite's
collection is untouched.
"""
from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.core.auth import ExecutionScope
from app.main import app

BASE = "/api/v1/canvas"


def headers(owner: str = "u1", service: str = "hd2ai-admin", tenant: str = "default_tenant") -> dict:
    return {
        "X-HD2Lab-User": owner,
        "X-HD2Lab-Service": service,
        "X-HD2Lab-Tenant": tenant,
    }


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def scope(owner: str = "u1", service: str = "hd2ai-admin", tenant: str = "default_tenant") -> ExecutionScope:
    return ExecutionScope(
        tenant_id=tenant,
        owner_id=owner,
        service_id=service,
        role="user",
        authenticated=True,
        auth_source="dev_local",
        allowed_tenants=frozenset({tenant}),
        allowed_services=frozenset({service}),
    )


def configure(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("AGCORE_ENV", "development")
    monkeypatch.setenv("AGCORE_CANVAS_DATA_DIR", str(tmp_path / "canvas"))
    monkeypatch.delenv("AGCORE_CANVAS_DB_PATH", raising=False)
    monkeypatch.setenv("AGCORE_CANVAS_WORKSPACE_PROVIDER", "dev")
    monkeypatch.delenv("AGCORE_CANVAS_MAX_CONTENT_BYTES", raising=False)
    monkeypatch.delenv("AGCORE_CANVAS_MAX_REVISIONS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    from app.canvas.service import reset_canvas_service

    reset_canvas_service()


