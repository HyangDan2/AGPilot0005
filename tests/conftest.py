"""Fixtures for the R2 Canvas test modules (CP-01 … CP-06).

The Canvas engine reads its paths and limits from ``AGCORE_CANVAS_*`` environment variables at
service-construction time, so each test gets a throw-away data directory and a fresh singleton.
"""
from __future__ import annotations

import pytest

from tests.r2_canvas_support import configure


@pytest.fixture()
def canvas(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path)
    from app.canvas.service import get_canvas_service

    service = get_canvas_service()
    yield service
    from app.canvas.service import reset_canvas_service

    reset_canvas_service()


@pytest.fixture()
def configured(tmp_path, monkeypatch):
    def _apply(**env):
        configure(monkeypatch, tmp_path, **env)
        from app.canvas.service import get_canvas_service

        return get_canvas_service()

    yield _apply
    from app.canvas.service import reset_canvas_service

    reset_canvas_service()
