"""R2 Canvas WO-C17 — Backend correction (C-FIX-01 … C-FIX-06).

Each test exercises one corrected behaviour against the real FastAPI app and a throw-away SQLite
store. Nothing here re-runs the pre-existing 219-test baseline: these are the tests required for
the changed features only.
"""
from __future__ import annotations

import asyncio
import pytest

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.service import get_canvas_service
from app.canvas.workspace import LocalDevWorkspaceProvider, UnavailableWorkspaceProvider
from app.core.auth import current_execution_scope
from tests.r2_canvas_support import BASE, client, configure, headers, scope

CONTENT = "첫 문장입니다. 둘째 문장입니다."


async def _create(c, *, title="보정 문서", content=CONTENT, **extra):
    r = await c.post(
        f"{BASE}/artifacts",
        headers=headers(),
        json={"title": title, "artifact_type": "plain_text", "content": content, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()["artifact"]["artifact_id"]


async def _propose(c, aid, *, start=0, end=8, replacement="고친 문장.", base_revision=None):
    body = {
        "instruction": "첫 문장을 고쳐줘",
        "selection": {"start": start, "end": end, "offset_unit": "utf16"},
        "scripted_replacement": replacement,
    }
    if base_revision is not None:
        body["base_revision"] = base_revision
    r = await c.post(f"{BASE}/artifacts/{aid}/edit-proposals", headers=headers(), json=body)
    assert r.status_code == 201, r.text
    return r.json()["proposal"]


async def _apply(c, proposal_id, *, approved=True, role=None):
    return await c.post(
        f"{BASE}/edit-proposals/{proposal_id}/apply",
        headers=headers(role=role),
        json={"user_approved": approved},
    )


# --------------------------------------------------------------------------- C-FIX-01


@pytest.mark.asyncio
async def test_cfix01_workspace_creation_and_link_require_write(canvas):
    assert isinstance(canvas.workspace, LocalDevWorkspaceProvider)
    canvas.workspace.add_workspace("ws_readonly", name="읽기 전용", level="read")
    async with client() as c:
        # read-only grant: no artifact may be created into the workspace
        r = await c.post(
            f"{BASE}/artifacts",
            headers=headers(),
            json={
                "title": "읽기 전용 시도",
                "artifact_type": "plain_text",
                "content": "본문",
                "workspace_id": "ws_readonly",
            },
        )
        assert r.status_code == 403
        detail = r.json()["detail"]
        assert detail["code"] == "workspace_access_denied"
        assert detail["context"]["required_level"] == "write"
        assert detail["context"]["level"] == "read"
        assert (await c.get(f"{BASE}/artifacts", headers=headers())).json()["total"] == 0

        # write grant: creation succeeds
        aid = await _create(c, workspace_id="ws_dev00000001")

        # read-only grant: linking is refused and the artifact keeps no workspace link
        r = await c.post(
            f"{BASE}/artifacts/{aid}/links", headers=headers(), json={"workspace_id": "ws_readonly"}
        )
        assert r.status_code == 403
        assert r.json()["detail"]["context"]["required_level"] == "write"
        body = (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).json()
        assert body["artifact"]["workspace_id"] == "ws_dev00000001"

        # write grant: linking succeeds and registers the reference
        canvas.workspace.add_workspace("ws_write00001", name="쓰기 가능", level="write")
        r = await c.post(
            f"{BASE}/artifacts/{aid}/links", headers=headers(), json={"workspace_id": "ws_write00001"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["workspace_sync"]["status"] == "synced"
        assert r.json()["workspace_sync"]["access"]["level"] == "write"


@pytest.mark.asyncio
async def test_cfix01_read_grant_allows_reads_but_never_writes(canvas):
    async with client() as c:
        aid = await _create(c, workspace_id="ws_dev00000001")
        canvas.workspace.set_level("ws_dev00000001", "read")

        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.status_code == 200
        assert r.json()["content"] == CONTENT

        r = await c.post(
            f"{BASE}/artifacts/{aid}/patches",
            headers=headers(),
            json={"base_revision": 1, "operation": "append_content", "replacement_text": "추가"},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["context"]["required_level"] == "write"
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).json()["content"] == CONTENT


# --------------------------------------------------------------------------- C-FIX-02


@pytest.mark.asyncio
async def test_cfix02_revoked_workspace_blocks_every_read_and_write_path(canvas):
    async with client() as c:
        aid = await _create(c, workspace_id="ws_dev00000001")
        free = await _create(c, title="워크스페이스 없음")
        proposal = await _propose(c, aid)
        assert (await c.get(f"{BASE}/artifacts/{aid}/revisions", headers=headers())).status_code == 200

        canvas.workspace.set_level("ws_dev00000001", "none")

        for method, path, kwargs, payload in (
            ("get", f"{BASE}/artifacts/{aid}", {}, None),
            ("get", f"{BASE}/artifacts/{aid}/export", {}, None),
            ("get", f"{BASE}/artifacts/{aid}/preview", {}, None),
            ("get", f"{BASE}/artifacts/{aid}/revisions", {}, None),
            ("get", f"{BASE}/artifacts/{aid}/revisions/1", {}, None),
            ("get", f"{BASE}/artifacts/{aid}/diff", {"params": {"from_revision": 1, "to_revision": 1}}, None),
            ("get", f"{BASE}/artifacts/{aid}/edit-proposals", {}, None),
            (
                "post",
                f"{BASE}/artifacts/{aid}/patches",
                {},
                {"base_revision": 1, "operation": "append_content", "replacement_text": "탈취"},
            ),
            (
                "post",
                f"{BASE}/artifacts/{aid}/patches/preview",
                {},
                {"base_revision": 1, "operation": "append_content", "replacement_text": "미리보기"},
            ),
            ("patch", f"{BASE}/artifacts/{aid}", {}, {"title": "이름 변경"}),
            ("delete", f"{BASE}/artifacts/{aid}", {}, None),
            (
                "post",
                f"{BASE}/artifacts/{aid}/revisions/1/restore",
                {},
                {"change_reason": "복원"},
            ),
            (
                "post",
                f"{BASE}/artifacts/{aid}/edit-proposals",
                {},
                {
                    "instruction": "수정",
                    "selection": {"start": 0, "end": 4},
                    "scripted_replacement": "x",
                },
            ),
            ("post", f"{BASE}/edit-proposals/{proposal['proposal_id']}/reject", {}, {"reason": "x"}),
        ):
            response = await getattr(c, method)(path, headers=headers(), **({"json": payload} if payload else {}), **kwargs)
            assert response.status_code == 403, (path, response.status_code, response.text)
            assert response.json()["detail"]["code"] == "workspace_access_denied", path

        # export/ preview of another artifact in the same scope is unaffected
        r = await c.get(f"{BASE}/artifacts/{free}/export", headers=headers())
        assert r.status_code == 200

        # listing drops the revoked artifact and says how many were excluded
        page = (await c.get(f"{BASE}/artifacts", headers=headers())).json()
        assert [item["artifact_id"] for item in page["items"]] == [free]
        assert page["total"] == 1
        assert page["excluded_workspace_denied"] >= 1

        # an explicit filter on the revoked workspace is refused outright
        r = await c.get(f"{BASE}/artifacts", headers=headers(), params={"workspace_id": "ws_dev00000001"})
        assert r.status_code == 403

        # detaching is the documented way out and does not need workspace access
        r = await c.delete(f"{BASE}/artifacts/{aid}/links/workspace", headers=headers())
        assert r.status_code == 200
        assert r.json()["artifact"]["workspace_id"] is None
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).status_code == 200


@pytest.mark.asyncio
async def test_cfix02_unavailable_workspace_fails_closed(canvas):
    async with client() as c:
        aid = await _create(c, workspace_id="ws_dev00000001")
        canvas.workspace = UnavailableWorkspaceProvider("workspace engine is down")
        canvas.manager.workspace = canvas.workspace
        canvas.revisions.workspace = canvas.workspace
        canvas.patches.workspace = canvas.workspace
        canvas.proposals.workspace = canvas.workspace

        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "workspace_unavailable"

        page = (await c.get(f"{BASE}/artifacts", headers=headers())).json()
        assert page["items"] == []
        assert page["excluded_workspace_unavailable"] >= 1


@pytest.mark.asyncio
async def test_cfix02_foreign_scope_still_cannot_probe(canvas):
    async with client() as c:
        aid = await _create(c, workspace_id="ws_dev00000001")
        canvas.workspace.set_level("ws_dev00000001", "none")
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers(owner="u2"))
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "artifact_not_found"


# --------------------------------------------------------------------------- C-FIX-03


@pytest.mark.asyncio
async def test_cfix03_concurrent_apply_creates_exactly_one_revision(canvas):
    async with client() as c:
        aid = await _create(c)
        proposal = await _propose(c, aid)
    async with client() as first, client() as second:
        results = await asyncio.gather(
            _apply(first, proposal["proposal_id"]),
            _apply(second, proposal["proposal_id"]),
            return_exceptions=True,
        )
    statuses = sorted(getattr(result, "status_code", 599) for result in results)
    assert statuses == [200, 409], [getattr(r, "text", r) for r in results]
    failed = [r for r in results if r.status_code == 409][0]
    assert failed.json()["detail"]["code"] == "proposal_already_applied"
    async with client() as c:
        assert (await c.get(f"{BASE}/artifacts/{aid}/revisions", headers=headers())).json()["total"] == 2
        assert (await c.get(f"{BASE}/edit-proposals/{proposal['proposal_id']}", headers=headers())).json()[
            "proposal"
        ]["status"] == "applied"


@pytest.mark.asyncio
async def test_cfix03_stale_apply_commits_stale_status_and_adds_no_revision(canvas):
    async with client() as c:
        aid = await _create(c)
        proposal = await _propose(c, aid)
        r = await c.post(
            f"{BASE}/artifacts/{aid}/patches",
            headers=headers(),
            json={
                "base_revision": 1,
                "operation": "replace_selection",
                "selection": {"start": 0, "end": 8},
                "expected_original_text": CONTENT[0:8],
                "replacement_text": "사용자 문장.",
            },
        )
        assert r.status_code == 200
        r = await _apply(c, proposal["proposal_id"])
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "proposal_stale"
        assert (await c.get(f"{BASE}/artifacts/{aid}/revisions", headers=headers())).json()["total"] == 2
        assert (await c.get(f"{BASE}/edit-proposals/{proposal['proposal_id']}", headers=headers())).json()[
            "proposal"
        ]["status"] == "stale"


@pytest.mark.asyncio
async def test_cfix03_rejected_apply_is_an_explicit_conflict(canvas):
    async with client() as c:
        aid = await _create(c)
        proposal = await _propose(c, aid)
        r = await c.post(
            f"{BASE}/edit-proposals/{proposal['proposal_id']}/reject", headers=headers(), json={"reason": "불필요"}
        )
        assert r.status_code == 200
        r = await _apply(c, proposal["proposal_id"])
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "proposal_rejected"


@pytest.mark.asyncio
async def test_cfix03_failed_write_leaves_proposal_and_revisions_consistent(canvas, monkeypatch):
    async with client() as c:
        aid = await _create(c)
        proposal = await _propose(c, aid)

        original = canvas.storage._write_blob

        def boom(content):
            raise CanvasError(CanvasErrorCode.STORAGE_FAILURE, "disk unavailable")

        monkeypatch.setattr(canvas.storage, "_write_blob", boom)
        r = await _apply(c, proposal["proposal_id"])
        assert r.status_code == 500
        assert r.json()["detail"]["code"] == "storage_failure"
        canvas.storage._write_blob = original

        # the transaction rolled back: no revision, and the proposal is still applicable
        assert (await c.get(f"{BASE}/artifacts/{aid}/revisions", headers=headers())).json()["total"] == 1
        body = (await c.get(f"{BASE}/edit-proposals/{proposal['proposal_id']}", headers=headers())).json()
        assert body["proposal"]["status"] == "proposed"
        r = await _apply(c, proposal["proposal_id"])
        assert r.status_code == 200
        assert r.json()["revision"]["revision"] == 2


# --------------------------------------------------------------------------- C-FIX-04


@pytest.mark.asyncio
async def test_cfix04_autosave_cap_prunes_instead_of_blocking(configured):
    service = configured(AGCORE_CANVAS_MAX_REVISIONS=4, AGCORE_CANVAS_RETENTION_MIN_REVISIONS=2)
    artifact, _r, _d, _m = service.manager.create(
        scope(), title="자동저장", artifact_type="plain_text", content="0"
    )
    for index in range(12):
        service.patches.apply(
            scope(),
            __import__("app.canvas.models", fromlist=["PatchRequest"]).PatchRequest(
                artifact_id=artifact.artifact_id,
                base_revision=index + 1,
                operation="append_content",
                replacement_text=str(index + 1),
            ),
        )
    loaded = service.manager.get(scope(), artifact.artifact_id)
    assert loaded["artifact"]["current_revision"] == 13
    assert loaded["content"] == "0123456789101112"
    assert service.storage.count_revisions(artifact.artifact_id) == 4
    history = service.revisions.history(scope(), artifact.artifact_id)
    assert len(history.items) == 4
    assert history.items[0].revision == 13 and history.items[1].revision == 12
    # the baseline (revision 1) is pruned last, so the newest window plus the baseline survives
    assert service.storage.get_revision(artifact.artifact_id, 1) is not None
    pruned = service.storage.list_pruned_revisions(artifact.artifact_id)
    assert len(pruned) == 9
    assert all(row["reason"].startswith("retention_cap") for row in pruned)
    assert loaded["limits"]["revision_retention"]["editing_blocked_at_limit"] is False


@pytest.mark.asyncio
async def test_cfix04_proposal_referenced_revision_survives_pruning(configured):
    service = configured(AGCORE_CANVAS_MAX_REVISIONS=6, AGCORE_CANVAS_RETENTION_MIN_REVISIONS=2)
    from app.canvas.models import PatchRequest
    from app.canvas.offsets import Selection

    artifact, _r, _d, _m = service.manager.create(
        scope(), title="제안 보존", artifact_type="plain_text", content="가나다라마바사"
    )
    service.patches.apply(
        scope(),
        PatchRequest(
            artifact_id=artifact.artifact_id, base_revision=1,
            operation="append_content", replacement_text="아",
        ),
    )  # revision 2 is the proposal's base
    service.proposals.create(
        scope(),
        artifact.artifact_id,
        instruction="앞부분 수정",
        selection=Selection(start=0, end=2),
        scripted_replacement="하하",
    )
    for revision in range(2, 7):
        service.patches.apply(
            scope(),
            PatchRequest(
                artifact_id=artifact.artifact_id, base_revision=revision,
                operation="append_content", replacement_text="자",
            ),
        )
    assert service.storage.count_revisions(artifact.artifact_id) == 6
    # the proposal still points at revision 2, so pruning skipped it
    assert service.storage.get_revision(artifact.artifact_id, 2) is not None
    pruned = [row["revision"] for row in service.storage.list_pruned_revisions(artifact.artifact_id)]
    assert 2 not in pruned
    assert service.manager.get(scope(), artifact.artifact_id)["content"].startswith("가나다라마바사아")


# --------------------------------------------------------------------------- C-FIX-05


@pytest.mark.asyncio
async def test_cfix05_disabled_feature_blocks_api_and_service(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path, AGCORE_CANVAS_ENABLED="false")
    with pytest.raises(CanvasError) as exc:
        get_canvas_service()
    assert exc.value.code is CanvasErrorCode.CANVAS_DISABLED
    assert exc.value.http_status == 503

    async with client() as c:
        for path in (f"{BASE}/capabilities", f"{BASE}/tools/manifest", f"{BASE}/artifacts"):
            r = await c.get(path, headers=headers())
            assert r.status_code == 503, path
            assert r.json()["detail"]["code"] == "canvas_disabled"
        r = await c.post(
            f"{BASE}/artifacts",
            headers=headers(),
            json={"title": "t", "artifact_type": "markdown", "content": "c"},
        )
        assert r.status_code == 503
        # the existing chat surface keeps working while Canvas is off
        r = await c.get("/api/v1/health", headers=headers())
        assert r.status_code == 200


def test_cfix05_default_is_off_and_production_forces_dev_off(monkeypatch):
    monkeypatch.delenv("AGCORE_CANVAS_ENABLED", raising=False)
    monkeypatch.delenv("AGCORE_CANVAS_DEV_PREVIEW", raising=False)
    from app.canvas.config import load_canvas_settings

    settings = load_canvas_settings()
    assert settings.enabled is False
    assert settings.dev_preview is False
    assert settings.dev_adapters_enabled is False
    assert settings.activation()["auto_activation"] is False

    monkeypatch.setenv("AGCORE_ENV", "production")
    monkeypatch.setenv("AGCORE_CANVAS_ENABLED", "true")
    monkeypatch.setenv("AGCORE_CANVAS_DEV_PREVIEW", "true")
    monkeypatch.setenv("AGCORE_CANVAS_WORKSPACE_PROVIDER", "dev")
    monkeypatch.setenv("AGCORE_CANVAS_AI_ADAPTER", "dev")
    prod = load_canvas_settings()
    assert prod.enabled is True
    assert prod.dev_preview is False
    assert prod.dev_adapters_enabled is False
    assert prod.workspace_provider == "workspace"
    assert prod.ai_edit_adapter == "runtime"
    monkeypatch.setenv("AGCORE_ENV", "development")


@pytest.mark.asyncio
async def test_cfix05_dev_preview_is_a_separate_switch(configured):
    service = configured(AGCORE_CANVAS_DEV_PREVIEW="false")
    assert service.settings.enabled is True
    assert service.dev_preview is False
    assert getattr(service.workspace, "mode") == "unavailable"

    async with client() as c:
        caps = (await c.get(f"{BASE}/capabilities", headers=headers())).json()
        assert caps["enabled"] is True
        assert caps["feature_isolation"]["feature_enabled"] is True
        assert caps["feature_isolation"]["dev_preview"] is False
        assert caps["workspace"]["provider_mode"] == "unavailable"
        # plain artifacts still work; only the dev-only surfaces are gone
        aid = await _create(c)
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).status_code == 200
        r = await c.post(
            f"{BASE}/artifacts/{aid}/edit-proposals",
            headers=headers(),
            json={"instruction": "수정", "selection": {"start": 0, "end": 4}, "scripted_replacement": "x"},
        )
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "ai_adapter_unavailable"
        r = await c.post(
            f"{BASE}/generations",
            headers=headers(),
            json={"title": "t", "artifact_type": "markdown", "content": "c"},
        )
        assert r.status_code == 503
        assert r.json()["detail"]["context"]["dev_preview"] is False
        assert (await c.get(f"{BASE}/artifacts", headers=headers())).json()["total"] == 1


def test_cfix05_disabled_engine_does_not_register_tools(monkeypatch):
    from app.canvas.tools import AI_EXPOSED_TOOL_NAMES
    from app.tools.registry import ToolRegistry

    monkeypatch.delenv("AGCORE_CANVAS_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("AGCORE_CANVAS_ENABLED", raising=False)
    assert set(AI_EXPOSED_TOOL_NAMES).isdisjoint({reg.name for reg in ToolRegistry().list_tools()})


# --------------------------------------------------------------------------- C-FIX-06


@pytest.mark.asyncio
async def test_cfix06_apply_requires_an_explicit_user_approval(canvas):
    async with client() as c:
        aid = await _create(c)
        proposal = await _propose(c, aid)

        # no approval flag at all
        r = await c.post(f"{BASE}/edit-proposals/{proposal['proposal_id']}/apply", headers=headers(), json={})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "user_approval_required"
        assert r.json()["detail"]["context"]["tool_call_is_approval"] is False

        # a machine principal cannot approve on the user's behalf, even with the flag
        r = await _apply(c, proposal["proposal_id"], role="service")
        assert r.status_code == 403
        assert r.json()["detail"]["context"]["role"] == "service"
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).json()["content"] == CONTENT

        # the verified interactive approval applies it, and the approval is recorded
        r = await _apply(c, proposal["proposal_id"], role="user")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["approval"]["source"] == "user_action"
        assert body["approval"]["approved_by"] == "u1"
        assert body["event"]["data"]["approval"]["approval_id"] == body["approval"]["approval_id"]
        assert body["revision"]["actor"] == "ai"


@pytest.mark.asyncio
async def test_cfix06_tool_call_never_counts_as_approval(canvas):
    from app.canvas.tools import AI_EXPOSED_TOOL_NAMES, apply_edit, install_canvas_tools
    from app.tools.registry import ToolRegistry

    assert "canvas.apply_edit" not in AI_EXPOSED_TOOL_NAMES
    local = ToolRegistry()
    installed = install_canvas_tools(local)
    assert "canvas.apply_edit" not in installed
    assert "canvas.apply_edit" not in {reg.name for reg in local.list_tools()}
    # an admin surface that supplies a real approval may register it, and it stays gated
    install_canvas_tools(local, ai_exposed_only=False)
    assert "canvas.apply_edit" in {reg.name for reg in local.list_tools()}

    async with client() as c:
        aid = await _create(c)
        proposal = await _propose(c, aid)

    token = current_execution_scope.set(scope())
    try:
        payload = await apply_edit(proposal["proposal_id"])
        assert payload["error"] is True
        assert payload["code"] == "user_approval_required"
        assert payload["context"]["proposal_id"] == proposal["proposal_id"]
        assert payload["context"]["tool_call_is_approval"] is False
    finally:
        current_execution_scope.reset(token)

    assert canvas.manager.get(scope(), aid)["artifact"]["current_revision"] == 1


def test_cfix06_manifest_declares_the_approval_boundary(canvas):
    from app.canvas.tools import manifest

    shipped = manifest()
    boundary = shipped["approval_boundary"]
    assert boundary["apply_requires_user_approval"] is True
    assert boundary["tool_call_is_approval"] is False
    assert boundary["withheld_tools"] == ["canvas.apply_edit"]
    assert "canvas.apply_edit" not in boundary["ai_exposed_tools"]
    apply_spec = [tool for tool in shipped["tools"] if tool["name"] == "canvas.apply_edit"][0]
    assert apply_spec["ai_exposed"] is False
    assert apply_spec["requires_user_approval"] is True
    assert all(tool["ai_exposed"] for tool in shipped["tools"] if tool["name"] != "canvas.apply_edit")
