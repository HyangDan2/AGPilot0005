"""R2 Canvas CP-01 — Artifact Manager (WO-C02) and Artifact Persistence (WO-C03)."""
from __future__ import annotations

import sqlite3

import pytest

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.types import ArtifactTypeSpec
from tests.r2_canvas_support import BASE, client, headers, scope


async def _create(c, *, owner="u1", title="문서", artifact_type="markdown", content="본문", **extra):
    payload = {"title": title, "artifact_type": artifact_type, "content": content, **extra}
    r = await c.post(f"{BASE}/artifacts", headers=headers(owner), json=payload)
    return r


@pytest.mark.asyncio
async def test_artifact_crud_and_metadata(canvas):
    async with client() as c:
        r = await _create(c, title="R2 설계 문서", content="# 제목\n\n한글 본문")
        assert r.status_code == 201, r.text
        body = r.json()
        artifact = body["artifact"]
        assert artifact["artifact_id"].startswith("art_")
        assert artifact["current_revision"] == 1
        assert artifact["content_hash"].startswith("sha256:")
        assert artifact["creation_source"] == "user"
        assert body["revision"]["actor"] == "user"
        assert body["content_metrics"]["utf8_bytes"] > 0
        assert set(artifact) >= {
            "artifact_id", "workspace_id", "conversation_id", "tenant_id", "owner_id",
            "service_id", "title", "artifact_type", "language", "current_revision",
            "content_hash", "created_at", "updated_at", "creation_source",
        }

        aid = artifact["artifact_id"]
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.status_code == 200
        assert r.json()["content"] == "# 제목\n\n한글 본문"
        assert r.json()["is_current"] is True

        r = await c.patch(f"{BASE}/artifacts/{aid}", headers=headers(), json={"title": "새 제목"})
        assert r.status_code == 200
        assert r.json()["artifact"]["title"] == "새 제목"
        # a rename is metadata only: no content revision is created
        assert r.json()["artifact"]["current_revision"] == 1

        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.status_code == 200
        assert r.json()["total"] == 1

        r = await c.delete(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.status_code == 200
        assert r.json() == {"artifact_id": aid, "deleted": True, "purged": False}
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "artifact_not_found"
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_multiple_artifacts_per_conversation_and_message_independence(canvas):
    async with client() as c:
        first = (await _create(c, title="문서 A", conversation_id="conv_001")).json()["artifact"]
        second = (await _create(c, title="문서 B", conversation_id="conv_001")).json()["artifact"]
        other = (await _create(c, title="문서 C", conversation_id="conv_002")).json()["artifact"]

        r = await c.get(f"{BASE}/artifacts", headers=headers(), params={"conversation_id": "conv_001"})
        ids = {item["artifact_id"] for item in r.json()["items"]}
        assert ids == {first["artifact_id"], second["artifact_id"]}

        # Deleting one artifact never touches its siblings or the conversation link of others.
        await c.delete(f"{BASE}/artifacts/{first['artifact_id']}", headers=headers())
        r = await c.get(f"{BASE}/artifacts/{second['artifact_id']}", headers=headers())
        assert r.status_code == 200
        assert r.json()["artifact"]["conversation_id"] == "conv_001"

        # Detaching the conversation keeps content and revisions (chat lifecycle is separate).
        r = await c.delete(f"{BASE}/artifacts/{second['artifact_id']}/links/conversation", headers=headers())
        assert r.status_code == 200
        assert r.json()["artifact"]["conversation_id"] is None
        r = await c.get(f"{BASE}/artifacts/{second['artifact_id']}", headers=headers())
        assert r.status_code == 200 and r.json()["content"] == "본문"
        r = await c.get(f"{BASE}/artifacts/{other['artifact_id']}", headers=headers())
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_artifact_type_registry_and_extension(canvas):
    async with client() as c:
        for artifact_type, extra in (
            ("markdown", {}),
            ("plain_text", {}),
            ("source_code", {"language": "python"}),
            ("html", {}),
            ("structured_json", {}),
        ):
            r = await _create(c, title=f"{artifact_type}", artifact_type=artifact_type, **extra)
            assert r.status_code == 201, (artifact_type, r.text)

        # source_code requires an explicit language
        r = await _create(c, title="코드", artifact_type="source_code")
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_request"

        # reserved (not yet enabled) and unknown types are refused
        r = await _create(c, title="차트", artifact_type="chart")
        assert r.status_code == 415
        assert r.json()["detail"]["code"] == "unsupported_artifact_type"
        r = await _create(c, title="???", artifact_type="nope")
        assert r.status_code == 415

        r = await c.get(f"{BASE}/types", headers=headers())
        catalogue = r.json()["artifact_types"]
        enabled = {item["artifact_type"] for item in catalogue if item["enabled"]}
        reserved = {item["artifact_type"] for item in catalogue if not item["enabled"]}
        assert enabled == {"markdown", "plain_text", "source_code", "html", "structured_json"}
        assert {"chart", "table", "spreadsheet", "office_document"} <= reserved
        assert r.json()["default_offset_unit"] == "utf16"

        # A new type can be registered at runtime without a schema migration.
        canvas.types.register(
            ArtifactTypeSpec(
                key="diagram",
                label="Diagram",
                media_type="text/plain",
                default_extension=".dgm",
                export_formats=("txt",),
            )
        )
        r = await _create(c, title="다이어그램", artifact_type="diagram")
        assert r.status_code == 201


@pytest.mark.asyncio
async def test_structured_json_reports_validation_without_losing_content(canvas):
    async with client() as c:
        r = await _create(c, title="json", artifact_type="structured_json", content='{"a": 1}')
        assert r.status_code == 201
        assert r.json()["content_validation"] == {"valid": True, "error": None}
        aid = r.json()["artifact"]["artifact_id"]
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.json()["content_validation"]["valid"] is True

        r = await _create(c, title="broken", artifact_type="structured_json", content='{"a": ]')
        assert r.status_code == 201
        aid = r.json()["artifact"]["artifact_id"]
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.json()["content_validation"]["valid"] is False
        assert r.json()["content"] == '{"a": ]'


@pytest.mark.asyncio
async def test_restart_restores_artifacts_and_revisions(configured):
    service = configured()
    artifact, revision, _dup, _meta = service.manager.create(
        scope(), title="재시작 문서", artifact_type="markdown", content="첫 내용"
    )
    service.patches.apply(
        scope(),
        __import__("app.canvas.models", fromlist=["PatchRequest"]).PatchRequest(
            artifact_id=artifact.artifact_id,
            base_revision=1,
            operation="append_content",
            replacement_text="\n둘째 줄",
        ),
    )

    from app.canvas.service import get_canvas_service, reset_canvas_service

    reset_canvas_service()
    restarted = get_canvas_service()
    loaded = restarted.manager.get(scope(), artifact.artifact_id)
    assert loaded["content"] == "첫 내용\n둘째 줄"
    assert loaded["revision"]["revision"] == 2
    history = restarted.revisions.history(scope(), artifact.artifact_id)
    assert [item.revision for item in history.items] == [2, 1]
    assert restarted.revisions.get(scope(), artifact.artifact_id, 1)["content"] == "첫 내용"


@pytest.mark.asyncio
async def test_workspace_link_uses_dev_adapter_and_denies_unknown_workspace(canvas):
    async with client() as c:
        aid = (await _create(c, title="ws 문서")).json()["artifact"]["artifact_id"]
        r = await c.post(
            f"{BASE}/artifacts/{aid}/links",
            headers=headers(),
            json={"workspace_id": "ws_dev00000001"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["artifact"]["workspace_id"] == "ws_dev00000001"
        assert body["workspace_sync"]["status"] == "synced"
        assert body["workspace_sync"]["provider_mode"] == "dev"
        assert body["workspace_sync"]["enforced"] is False

        r = await c.post(
            f"{BASE}/artifacts/{aid}/links", headers=headers(), json={"workspace_id": "ws_missing"}
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "workspace_access_denied"

        r = await c.get(f"{BASE}/artifacts", headers=headers(), params={"workspace_id": "ws_dev00000001"})
        assert {item["artifact_id"] for item in r.json()["items"]} == {aid}


@pytest.mark.asyncio
async def test_limits_content_size_and_revision_retention(configured):
    service = configured(AGCORE_CANVAS_MAX_CONTENT_BYTES=2048, AGCORE_CANVAS_MAX_REVISIONS=3)
    with pytest.raises(CanvasError) as exc:
        service.manager.create(scope(), title="큰 문서", artifact_type="plain_text", content="가" * 2000)
    assert exc.value.code is CanvasErrorCode.CONTENT_TOO_LARGE
    assert exc.value.http_status == 413

    from app.canvas.models import PatchRequest

    artifact, _revision, _dup, _meta = service.manager.create(
        scope(), title="작은 문서", artifact_type="plain_text", content="a"
    )
    for index in range(2):
        service.patches.apply(
            scope(),
            PatchRequest(
                artifact_id=artifact.artifact_id,
                base_revision=index + 1,
                operation="append_content",
                replacement_text="b",
            ),
        )
    assert service.storage.count_revisions(artifact.artifact_id) == 3
    with pytest.raises(CanvasError) as exc:
        service.patches.apply(
            scope(),
            PatchRequest(
                artifact_id=artifact.artifact_id,
                base_revision=3,
                operation="append_content",
                replacement_text="c",
            ),
        )
    assert exc.value.code is CanvasErrorCode.REVISION_LIMIT_REACHED
    # the refused patch left the artifact untouched
    assert service.manager.get(scope(), artifact.artifact_id)["content"] == "abb"


@pytest.mark.asyncio
async def test_storage_failure_never_returns_success(canvas, monkeypatch):
    async with client() as c:
        def boom(content):
            raise CanvasError(CanvasErrorCode.STORAGE_FAILURE, "disk unavailable")

        monkeypatch.setattr(canvas.storage, "_write_blob", boom)
        r = await _create(c, title="실패", content="본문")
        assert r.status_code == 500
        assert r.json()["detail"]["code"] == "storage_failure"
        monkeypatch.undo()
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_idempotent_create_with_same_key(canvas):
    async with client() as c:
        payload = {
            "title": "중복 생성",
            "artifact_type": "markdown",
            "content": "본문",
            "idempotency_key": "gen-dup-1",
        }
        first = await c.post(f"{BASE}/artifacts", headers=headers(), json=payload)
        second = await c.post(f"{BASE}/artifacts", headers=headers(), json=payload)
        assert first.status_code == 201 and second.status_code == 201
        assert first.json()["artifact"]["artifact_id"] == second.json()["artifact"]["artifact_id"]
        assert second.json()["duplicate"] is True
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 1

        # a different scope using the same key gets its own artifact
        third = await c.post(f"{BASE}/artifacts", headers=headers(owner="u2"), json=payload)
        assert third.status_code == 201
        assert third.json()["duplicate"] is False
        assert third.json()["artifact"]["artifact_id"] != first.json()["artifact"]["artifact_id"]


@pytest.mark.asyncio
async def test_scope_isolation_on_every_read_path(canvas):
    async with client() as c:
        aid = (await _create(c, owner="u1", title="비공개")).json()["artifact"]["artifact_id"]

        for method, path, kwargs in (
            ("get", f"{BASE}/artifacts/{aid}", {}),
            ("get", f"{BASE}/artifacts/{aid}/preview", {}),
            ("get", f"{BASE}/artifacts/{aid}/export", {}),
            ("get", f"{BASE}/artifacts/{aid}/revisions", {}),
        ):
            response = await getattr(c, method)(path, headers=headers(owner="u2"), **kwargs)
            assert response.status_code == 404, (path, response.text)
            assert response.json()["detail"]["code"] == "artifact_not_found"

        r = await c.patch(f"{BASE}/artifacts/{aid}", headers=headers(owner="u2"), json={"title": "탈취"})
        assert r.status_code == 404
        r = await c.delete(f"{BASE}/artifacts/{aid}", headers=headers(owner="u2"))
        assert r.status_code == 404
        r = await c.get(f"{BASE}/artifacts", headers=headers(owner="u2"))
        assert r.json()["total"] == 0

        # another service in the same tenant/owner is also isolated
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers(owner="u1", service="hd2ai-public"))
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_purge_removes_rows(canvas):
    async with client() as c:
        aid = (await _create(c)).json()["artifact"]["artifact_id"]
        r = await c.delete(f"{BASE}/artifacts/{aid}", headers=headers(), params={"purge": "true"})
        assert r.status_code == 200 and r.json()["purged"] is True
        with sqlite3.connect(str(canvas.settings.db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM canvas_artifacts").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM canvas_revisions").fetchone()[0] == 0
