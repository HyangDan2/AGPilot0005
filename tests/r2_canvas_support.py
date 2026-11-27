"""Shared helpers for the R2 Canvas test modules.

Not a ``conftest.py`` on purpose: these helpers are imported explicitly so the existing suite's
collection is untouched.

WO-C17 note: the feature flag defaults to off, so every Canvas test must switch the feature on
(``AGCORE_CANVAS_ENABLED``) and, when it needs the dev workspace provider / dev edit adapter /
simulated generation stream, the separate dev preview switch (``AGCORE_CANVAS_DEV_PREVIEW``).
``configure()`` does both; a test that exercises the disabled state overrides them explicitly.
"""
from __future__ import annotations

from typing import Optional

from httpx import ASGITransport, AsyncClient

from app.core.auth import ExecutionScope
from app.main import app

BASE = "/api/v1/canvas"


def headers(
    owner: str = "u1",
    service: str = "hd2ai-admin",
    tenant: str = "default_tenant",
    role: Optional[str] = None,
) -> dict:
    payload = {
        "X-HD2Lab-User": owner,
        "X-HD2Lab-Service": service,
        "X-HD2Lab-Tenant": tenant,
    }
    if role is not None:
        payload["X-HD2Lab-Role"] = role
    return payload


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def scope(
    owner: str = "u1",
    service: str = "hd2ai-admin",
    tenant: str = "default_tenant",
    role: str = "user",
) -> ExecutionScope:
    return ExecutionScope(
        tenant_id=tenant,
        owner_id=owner,
        service_id=service,
        role=role,
        authenticated=True,
        auth_source="dev_local",
        allowed_tenants=frozenset({tenant}),
        allowed_services=frozenset({service}),
    )


def configure(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("AGCORE_ENV", "development")
    monkeypatch.setenv("AGCORE_CANVAS_ENABLED", "true")
    monkeypatch.setenv("AGCORE_CANVAS_DEV_PREVIEW", "true")
    monkeypatch.setenv("AGCORE_CANVAS_DEV_ADAPTERS", "true")
    monkeypatch.setenv("AGCORE_CANVAS_DATA_DIR", str(tmp_path / "canvas"))
    monkeypatch.delenv("AGCORE_CANVAS_DB_PATH", raising=False)
    monkeypatch.setenv("AGCORE_CANVAS_WORKSPACE_PROVIDER", "dev")
    monkeypatch.delenv("AGCORE_CANVAS_MAX_CONTENT_BYTES", raising=False)
    monkeypatch.delenv("AGCORE_CANVAS_MAX_REVISIONS", raising=False)
    monkeypatch.delenv("AGCORE_CANVAS_RETENTION_MIN_REVISIONS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    from app.canvas.service import reset_canvas_service

    reset_canvas_service()
