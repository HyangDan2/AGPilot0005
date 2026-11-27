"""WO-C18 — Omni Agent capability amendment (canvas.save_to_workspace / canvas.load_from_workspace).

Only the WO-C18 connection boundary is verified here. The frozen Canvas engine (artifact/revision/
patch/proposal/export/A01 workspace) keeps its own 90-test evidence and is not re-run in this file.

Covered:

* **Load (WO-C18-04)** from an existing workspace file: external-reference semantics (one artifact
  per workspace file, edit buffer only), path resolution, type inference, refresh on external
  change, refusal to discard unsaved edits, closed / read-only / local_browser / non-text refusals,
  and scope isolation;
* **Save (WO-C18-04)** with link state: ``workspace_file_id`` / ``relative_path`` /
  ``last_saved_content_hash``, default ``expected_content_hash`` blocking an external overwrite
  (``409 content_conflict``), Canvas content preserved on failure, read-only / closed refusals,
  ``local_browser`` → ``client_write_pending`` (completion contract unverified), detach, restart
  persistence;
* **Omni Agent tool/Adapter contract (WO-C18-02)**: manifest + action classes + gates, the second
  opt-in switch, end-to-end dispatch, the C-FIX-06 approval boundary and the untouched R1.3 surface;
* **Shared contract synchronization (WO-C18-05)**: SSoT sha, contract switch point in
  ``/capabilities``.
"""
from __future__ import annotations

import hashlib
import inspect
import json

import pytest

from app.canvas.errors import CanvasErrorCode
from app.canvas.omni import (
    CANVAS_OMNI_TOOL_CONTRACTS,
    CORE_TOOLS_ENV,
    OMNI_TOOL_MANIFEST,
    WORKSPACE_TOOL_NAMES,
    WORKSPACE_TOOLS_ENV,
    omni_exposed_tool_names,
    workspace_tools_enabled,
)
from app.canvas.storage import SCHEMA_VERSION, SqliteCanvasStorage
from app.canvas.tools import (
    AI_EXPOSED_TOOL_NAMES,
    CANVAS_TOOL_MANIFEST,
    install_canvas_tools,
    install_canvas_workspace_tools,
    manifest,
)
from app.canvas.workspace import (
    WORKSPACE_CONTRACT_SSOT_SHA,
    LocalDevWorkspaceProvider,
    WorkspaceStorageProvider,
)
from app.core.auth import current_execution_scope
from app.schemas.gateway import FunctionCall, ToolCall
from app.tools.dispatcher import dispatcher
from app.tools.registry import ToolRegistry, registry
from tests.r2_canvas_support import BASE, client, headers, scope

WS = "ws_c18omni0001"
CONTENT = "# 계획\n\n첫 번째 줄입니다.\n둘째 줄입니다.\n"
PATH = "notes/plan.md"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lab(canvas, *, level: str = "admin", **options) -> LocalDevWorkspaceProvider:
    assert isinstance(canvas.workspace, LocalDevWorkspaceProvider)
    canvas.workspace.add_workspace(WS, name="Omni lab", level=level, **options)
    return canvas.workspace


async def _load(c, **payload):
    return await c.post(
        f"{BASE}/artifacts/from-workspace", headers=headers(), json={"workspace_id": WS, **payload}
    )


async def _save(c, artifact_id, **payload):
    return await c.post(
        f"{BASE}/artifacts/{artifact_id}/save-to-workspace", headers=headers(), json=payload
    )


async def _create(c, *, title="저장 대상", body="본문입니다."):
    r = await c.post(
        f"{BASE}/artifacts",
        headers=headers(),
        json={"title": title, "artifact_type": "markdown", "content": body},
    )
    assert r.status_code == 201, r.text
    return r.json()["artifact"]["artifact_id"]


async def _link(c, artifact_id):
    r = await c.get(f"{BASE}/artifacts/{artifact_id}/workspace-link", headers=headers())
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- load


@pytest.mark.asyncio
async def test_c18_load_opens_an_external_reference_not_a_second_copy(canvas):
    workspace = _lab(canvas)
    seeded = workspace.seed_file(WS, PATH, CONTENT, mime_type="text/markdown")
    async with client() as c:
        r = await _load(c, path=PATH)
        assert r.status_code == 201, r.text
        body = r.json()
        artifact = body["artifact"]
        assert artifact["workspace_id"] == WS
        assert artifact["creation_source"] == "import"
        assert artifact["artifact_type"] == "markdown"
        assert artifact["title"] == "plan.md"
        assert body["content"] == CONTENT
        assert body["revision"]["revision"] == 1
        assert body["revision"]["operation"] == "create"
        assert body["reused"] is False and body["refreshed"] is False
        assert body["copy_semantics"] == "external_reference_with_edit_buffer"
        assert body["source"]["workspace_file_id"] == seeded["file_id"]
        assert body["source"]["relative_path"] == PATH
        assert body["source"]["content_hash"] == _sha(CONTENT)

        link = body["workspace_link"]
        assert link["workspace_file_id"] == seeded["file_id"]
        assert link["relative_path"] == PATH
        assert link["last_saved_content_hash"] == _sha(CONTENT)
        assert link["source_content_hash"] == _sha(CONTENT)
        assert link["save_state"] == "linked"
        assert link["last_saved_revision"] is None

        # the same workspace file is the same editing session: no second artifact, no new revision
        again = await _load(c, workspace_file_id=seeded["file_id"])
        assert again.status_code == 201, again.text
        assert again.json()["artifact"]["artifact_id"] == artifact["artifact_id"]
        assert again.json()["reused"] is True and again.json()["refreshed"] is False
        assert again.json()["revision"]["revision"] == 1

        listed = (await c.get(f"{BASE}/artifacts", headers=headers())).json()
        assert listed["total"] == 1
        link_route = await _link(c, artifact["artifact_id"])
        assert link_route["workspace_link"]["relative_path"] == PATH


@pytest.mark.asyncio
async def test_c18_load_infers_type_from_extension(canvas):
    workspace = _lab(canvas)
    workspace.seed_file(WS, "src/tool.py", "print('hello')\n", mime_type="text/x-python")
    workspace.seed_file(WS, "logs/run.log", "line one\n")
    async with client() as c:
        r = await _load(c, path="src/tool.py")
        assert r.status_code == 201, r.text
        assert r.json()["artifact"]["artifact_type"] == "source_code"
        assert r.json()["artifact"]["language"] == "python"
        r = await _load(c, path="logs/run.log")
        assert r.status_code == 201, r.text
        assert r.json()["artifact"]["artifact_type"] == "plain_text"
        assert r.json()["artifact"]["language"] is None


@pytest.mark.asyncio
async def test_c18_load_refreshes_the_buffer_after_an_external_change(canvas):
    workspace = _lab(canvas)
    workspace.seed_file(WS, PATH, CONTENT)
    async with client() as c:
        aid = (await _load(c, path=PATH)).json()["artifact"]["artifact_id"]
        assert (await c.post(f"{BASE}/artifacts/{aid}/patches", headers=headers(), json={
            "base_revision": 1, "operation": "append_content", "replacement_text": "저장한 문장."
        })).status_code == 200
        saved = await _save(c, aid)
        assert saved.status_code == 200, saved.text

        workspace.seed_file(WS, PATH, "다른 에이전트가 고친 내용\n")
        r = await _load(c, path=PATH)
        assert r.status_code == 201, r.text
        assert r.json()["reused"] is True and r.json()["refreshed"] is True
        assert r.json()["revision"]["revision"] == 3
        assert r.json()["content"] == "다른 에이전트가 고친 내용\n"
        assert r.json()["workspace_link"]["last_saved_content_hash"] == _sha("다른 에이전트가 고친 내용\n")


@pytest.mark.asyncio
async def test_c18_load_refuses_to_discard_unsaved_canvas_edits(canvas):
    workspace = _lab(canvas)
    workspace.seed_file(WS, PATH, CONTENT)
    async with client() as c:
        aid = (await _load(c, path=PATH)).json()["artifact"]["artifact_id"]
        assert (await c.post(f"{BASE}/artifacts/{aid}/patches", headers=headers(), json={
            "base_revision": 1, "operation": "append_content", "replacement_text": "아직 저장 안 함."
        })).status_code == 200
        workspace.seed_file(WS, PATH, "외부 수정 내용\n")

        r = await _load(c, path=PATH)
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "content_conflict"
        assert detail["context"]["artifact_id"] == aid
        assert detail["context"]["workspace_content_hash"] == _sha("외부 수정 내용\n")

        # the buffer was not touched by the refusal
        current = (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).json()
        assert current["revision"]["revision"] == 2
        assert current["content"].endswith("아직 저장 안 함.")

        forced = await _load(c, path=PATH, force=True)
        assert forced.status_code == 201, forced.text
        assert forced.json()["refreshed"] is True
        assert forced.json()["content"] == "외부 수정 내용\n"


@pytest.mark.asyncio
async def test_c18_load_refuses_closed_read_only_and_unknown(canvas):
    workspace = _lab(canvas)
    workspace.seed_file(WS, PATH, CONTENT)
    async with client() as c:
        workspace.set_lifecycle_state(WS, "closed")
        r = await _load(c, path=PATH)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "workspace_closed"
        assert r.json()["detail"]["retryable"] is True

        workspace.set_lifecycle_state(WS, "open")
        workspace.set_read_only(WS, True)
        r = await _load(c, path=PATH)
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "read_only_workspace"
        workspace.set_read_only(WS, False)

        r = await _load(c, path="notes/missing.md")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "file_not_found"

        r = await _load(c, workspace_file_id="wf_devmiss000000")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "file_not_found"

        # a read-only *grant* is not enough to open an editing session (it is opened to save back)
        canvas.workspace.set_level(WS, "read")
        r = await _load(c, path=PATH)
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "workspace_access_denied"
        assert r.json()["detail"]["context"]["required_level"] == "write"
        canvas.workspace.set_level(WS, "admin")

        # nothing was created by any refusal
        assert (await c.get(f"{BASE}/artifacts", headers=headers())).json()["total"] == 0


@pytest.mark.asyncio
async def test_c18_load_refuses_local_browser_and_non_text(canvas):
    workspace = _lab(canvas)
    workspace.set_storage_provider(
        WS, WorkspaceStorageProvider.LOCAL_BROWSER.value, client_handle_id="handle-9"
    )
    workspace.seed_file(WS, PATH, CONTENT)
    async with client() as c:
        r = await _load(c, path=PATH)
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "provider_mismatch"
        assert detail["context"]["client_read_required"] is True
        assert detail["context"]["client_handle_id"] == "handle-9"

    workspace.set_storage_provider(WS, WorkspaceStorageProvider.SERVER_MANAGED.value)
    workspace.seed_file(WS, "bin/data.bin", b"\xff\xfe\x00binary", mime_type="application/octet-stream")
    async with client() as c:
        r = await _load(c, path="bin/data.bin")
        assert r.status_code == 415, r.text
        assert r.json()["detail"]["code"] == "unsupported_artifact_type"
        assert r.json()["detail"]["context"]["encoding"] == "utf-8"
        assert (await c.get(f"{BASE}/artifacts", headers=headers())).json()["total"] == 0


@pytest.mark.asyncio
async def test_c18_load_requires_a_file_selector_and_a_safe_path(canvas):
    _lab(canvas)
    async with client() as c:
        r = await _load(c)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_request"
        for bad in ("../escape.md", "/etc/passwd", "a\\b.md", "C:evil.md"):
            r = await _load(c, path=bad)
            assert r.status_code == 400, (bad, r.text)
            assert r.json()["detail"]["code"] == "invalid_path"
        assert (await c.get(f"{BASE}/artifacts", headers=headers())).json()["total"] == 0


@pytest.mark.asyncio
async def test_c18_load_is_scoped_per_user(canvas):
    workspace = _lab(canvas)
    workspace.seed_file(WS, PATH, CONTENT)
    async with client() as c:
        first = await _load(c, path=PATH)
        aid = first.json()["artifact"]["artifact_id"]
        second = await c.post(
            f"{BASE}/artifacts/from-workspace",
            headers=headers(owner="u2"),
            json={"workspace_id": WS, "path": PATH},
        )
        assert second.status_code == 201, second.text
        assert second.json()["artifact"]["artifact_id"] != aid
        assert second.json()["reused"] is False
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers(owner="u2"))).status_code == 404


# --------------------------------------------------------------------------- save


@pytest.mark.asyncio
async def test_c18_save_links_workspace_file_id_path_and_hash(canvas):
    workspace = _lab(canvas)
    async with client() as c:
        aid = await _create(c, body="저장할 본문입니다.")
        r = await _save(c, aid, workspace_id=WS, path="reports/summary.md")
        assert r.status_code == 200, r.text
        body = r.json()
        written = workspace.list_files(WS)["reports/summary.md"]
        assert written["content"] == "저장할 본문입니다."
        assert body["save_state"] == "saved"
        assert body["write_mode"] == "server_write"
        link = body["workspace_link"]
        assert link["workspace_file_id"] == body["workspace_file"]["file_id"]
        assert link["relative_path"] == "reports/summary.md"
        assert link["last_saved_content_hash"] == written["content_hash"]
        assert link["last_saved_revision"] == 1
        assert link["save_state"] == "saved"
        assert link["last_saved_at"] is not None
        assert body["expected_content_hash_defaulted"] is False

        # the artifact is now workspace-linked, so the C-FIX-02 re-check applies to it
        artifact = (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).json()["artifact"]
        assert artifact["workspace_id"] == WS
        workspace.set_level(WS, "none")
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).status_code == 403
        workspace.set_level(WS, "admin")


@pytest.mark.asyncio
async def test_c18_save_default_hash_blocks_an_external_overwrite(canvas):
    workspace = _lab(canvas)
    workspace.seed_file(WS, PATH, CONTENT)
    async with client() as c:
        aid = (await _load(c, path=PATH)).json()["artifact"]["artifact_id"]
        assert (await c.post(f"{BASE}/artifacts/{aid}/patches", headers=headers(), json={
            "base_revision": 1, "operation": "append_content", "replacement_text": "Canvas 편집."
        })).status_code == 200
        saved = await _save(c, aid)
        assert saved.status_code == 200, saved.text
        saved_hash = saved.json()["workspace_link"]["last_saved_content_hash"]
        assert saved.json()["expected_content_hash_defaulted"] is True
        canvas_content = workspace.list_files(WS)[PATH]["content"]
        assert canvas_content.endswith("Canvas 편집.")

        workspace.seed_file(WS, PATH, "다른 에이전트가 덮어쓴 내용\n")
        r = await _save(c, aid)
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "content_conflict"
        assert detail["context"]["expected_content_hash"] == saved_hash
        assert detail["context"]["current_content_hash"] == _sha("다른 에이전트가 덮어쓴 내용\n")
        # the workspace file and the Canvas content both survived the refusal
        assert workspace.list_files(WS)[PATH]["content"] == "다른 에이전트가 덮어쓴 내용\n"
        current = (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).json()
        assert current["content"] == canvas_content
        assert (await _link(c, aid))["workspace_link"]["last_saved_content_hash"] == saved_hash

        # once the external content is back to what Canvas last wrote, the save goes through
        workspace.seed_file(WS, PATH, canvas_content)
        r = await _save(c, aid)
        assert r.status_code == 200, r.text
        assert r.json()["workspace_link"]["last_saved_content_hash"] == _sha(canvas_content)


@pytest.mark.asyncio
async def test_c18_two_artifacts_can_point_at_one_workspace_file(canvas):
    workspace = _lab(canvas)
    async with client() as c:
        first = await _create(c, title="원본", body="첫 번째 사본")
        second = await _create(c, title="사본", body="두 번째 사본")
        assert (await _save(c, first, workspace_id=WS, path="shared/doc.md")).status_code == 200
        r = await _save(c, second, workspace_id=WS, path="shared/doc.md")
        assert r.status_code == 200, r.text
        # each artifact keeps its own save state; the newer one is what a reload reuses
        assert (await _link(c, first))["workspace_link"]["artifact_id"] == first
        assert (await _link(c, second))["workspace_link"]["artifact_id"] == second
        reloaded = await _load(c, path="shared/doc.md")
        assert reloaded.json()["artifact"]["artifact_id"] == second
        assert workspace.list_files(WS)["shared/doc.md"]["content"] == "두 번째 사본"


@pytest.mark.asyncio
async def test_c18_save_reuses_the_link_workspace_and_path(canvas):
    _lab(canvas).seed_file(WS, PATH, CONTENT)
    async with client() as c:
        aid = (await _load(c, path=PATH)).json()["artifact"]["artifact_id"]
        r = await _save(c, aid)
        assert r.status_code == 200, r.text
        assert r.json()["path"] == PATH
        assert r.json()["workspace_id"] == WS
        # an explicit other path is a new target: no default hash is sent for it
        r = await _save(c, aid, path="copies/plan-copy.md")
        assert r.status_code == 200, r.text
        assert r.json()["expected_content_hash_defaulted"] is False
        assert r.json()["path"] == "copies/plan-copy.md"


@pytest.mark.asyncio
async def test_c18_save_refuses_read_only_closed_and_missing_workspace(canvas):
    workspace = _lab(canvas)
    async with client() as c:
        aid = await _create(c)
        r = await _save(c, aid)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_request"

        workspace.set_read_only(WS, True)
        r = await _save(c, aid, workspace_id=WS, path="a.md")
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "read_only_workspace"
        workspace.set_read_only(WS, False)

        workspace.set_lifecycle_state(WS, "closed")
        r = await _save(c, aid, workspace_id=WS, path="a.md")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "workspace_closed"
        workspace.set_lifecycle_state(WS, "open")

        r = await _save(c, aid, workspace_id=WS, path="../escape.md")
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "invalid_path"

        # every refusal left the artifact and the workspace untouched
        assert workspace.list_files(WS) == {}
        link = await _link(c, aid)
        assert link["workspace_link"] is None
        artifact = (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).json()["artifact"]
        assert artifact["workspace_id"] is None
        assert artifact["current_revision"] == 1


@pytest.mark.asyncio
async def test_c18_save_to_local_browser_returns_client_write_pending(canvas):
    workspace = _lab(canvas)
    workspace.set_storage_provider(
        WS, WorkspaceStorageProvider.LOCAL_BROWSER.value, client_handle_id="handle-11"
    )
    async with client() as c:
        aid = await _create(c, body="브라우저 폴더로 저장")
        r = await _save(c, aid, workspace_id=WS, path="노트/메모.md")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["write_mode"] == "client_write"
        assert body["client_write"]["client_handle_id"] == "handle-11"
        assert body["client_write"]["relative_path"] == "노트/메모.md"
        assert body["save_state"] == "client_write_pending"
        assert body["completion_contract"].startswith("unverified")
        # the server did not write, so no hash is claimed as saved
        assert body["workspace_link"]["last_saved_content_hash"] is None
        assert body["workspace_link"]["save_state"] == "client_write_pending"
        assert workspace.list_files(WS) == {}


@pytest.mark.asyncio
async def test_c18_detach_drops_the_save_state_and_link_survives_a_restart(canvas):
    _lab(canvas).seed_file(WS, PATH, CONTENT)
    async with client() as c:
        aid = (await _load(c, path=PATH)).json()["artifact"]["artifact_id"]
        stored = SqliteCanvasStorage(canvas.settings.db_path, canvas.settings.blob_root)
        link = stored.get_workspace_link(aid)
        assert link is not None and link.relative_path == PATH
        assert stored.find_workspace_link(
            tenant_id=link.tenant_id, owner_id=link.owner_id, service_id=link.service_id,
            workspace_id=WS, relative_path=PATH,
        ) is not None

        r = await c.delete(f"{BASE}/artifacts/{aid}/links/workspace", headers=headers())
        assert r.status_code == 200, r.text
        assert r.json()["artifact"]["workspace_id"] is None
        assert (await _link(c, aid))["workspace_link"] is None
        assert stored.get_workspace_link(aid) is None
        # the artifact itself is untouched (it is still an editable artifact)
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).json()["content"] == CONTENT


# --------------------------------------------------------------------------- Omni tool contract


def test_c18_manifest_declares_the_workspace_surface_separately(canvas):
    shipped = manifest()
    # the frozen eight stay exactly eight; the workspace pair is reported on its own
    assert len(shipped["tools"]) == 8 == len(CANVAS_TOOL_MANIFEST)
    assert [tool["name"] for tool in shipped["workspace_tools"]] == list(WORKSPACE_TOOL_NAMES)
    assert set(shipped["tool_names"]) == set(AI_EXPOSED_TOOL_NAMES) | {"canvas.apply_edit"} | set(WORKSPACE_TOOL_NAMES)

    for tool in shipped["workspace_tools"]:
        schema = tool["parameters"]
        assert schema["additionalProperties"] is False
        assert not ({"tenant_id", "owner_id", "service_id", "role"} & set(schema["properties"]))
        assert tool["ai_exposed"] is True
        assert "WORKSPACE_TOOLS_ENABLED" in tool["gate"]

    boundary = shipped["approval_boundary"]
    assert boundary["withheld_tools"] == ["canvas.apply_edit"]

    omni = shipped["omni"]
    assert omni["contract"]["work_orders"] == ["WO-C18-02", "WO-C18-04", "WO-C18-05"]
    assert omni["gates"]["workspace_tools_require_both"] is True
    assert omni["action_validator"]["changed"] is False
    assert len(omni["adapters"]) >= 5 and len(omni["integration_patches"]) >= 6
    assert omni["workspace_contract"]["switch_point"]["ssot"]["sha"] == WORKSPACE_CONTRACT_SSOT_SHA == "907482c"
    assert "canvas.apply_edit" in omni["withheld_tools"]
    assert {c["name"] for c in CANVAS_OMNI_TOOL_CONTRACTS} == set(shipped["tool_names"])
    assert {c["action_class"] for c in CANVAS_OMNI_TOOL_CONTRACTS} <= {
        "canvas_read", "canvas_write", "canvas_write_user_approved", "workspace_read", "workspace_write",
    }
    assert len(OMNI_TOOL_MANIFEST) == 2


@pytest.mark.asyncio
async def test_c18_workspace_tools_need_their_own_opt_in_gate(canvas, monkeypatch):
    monkeypatch.delenv(WORKSPACE_TOOLS_ENV, raising=False)
    monkeypatch.delenv(CORE_TOOLS_ENV, raising=False)
    assert workspace_tools_enabled() is False
    for value in ("", "0", "false", "no", "off", "enabled"):
        monkeypatch.setenv(WORKSPACE_TOOLS_ENV, value)
        monkeypatch.setenv(CORE_TOOLS_ENV, "true")
        assert workspace_tools_enabled() is False, value
        assert WORKSPACE_TOOL_NAMES[0] not in {reg.name for reg in ToolRegistry().list_tools()}, value

    # the core gate alone must not expose the workspace pair
    monkeypatch.setenv(CORE_TOOLS_ENV, "true")
    monkeypatch.delenv(WORKSPACE_TOOLS_ENV, raising=False)
    registered = install_canvas_tools(registry)
    assert set(registered) == set(AI_EXPOSED_TOOL_NAMES)
    assert WORKSPACE_TOOL_NAMES[0] not in {reg.name for reg in registry.list_tools()}
    assert set(omni_exposed_tool_names()) == set(AI_EXPOSED_TOOL_NAMES)

    # both gates on: the pair is registered and reported
    monkeypatch.setenv(WORKSPACE_TOOLS_ENV, "on")
    install_canvas_workspace_tools(registry)
    names = {reg.name for reg in registry.list_tools()}
    assert set(WORKSPACE_TOOL_NAMES) <= names
    assert set(omni_exposed_tool_names()) == set(AI_EXPOSED_TOOL_NAMES) | set(WORKSPACE_TOOL_NAMES)


@pytest.mark.asyncio
async def test_c18_tool_dispatch_loads_and_saves_end_to_end(canvas, monkeypatch):
    workspace = _lab(canvas)
    workspace.seed_file(WS, PATH, CONTENT)
    monkeypatch.setenv(CORE_TOOLS_ENV, "true")
    monkeypatch.setenv(WORKSPACE_TOOLS_ENV, "true")
    install_canvas_workspace_tools(registry)
    token = current_execution_scope.set(scope())
    try:
        load = ToolCall(
            id="c18-1",
            function=FunctionCall(
                name="canvas.load_from_workspace",
                arguments=json.dumps({"workspace_id": WS, "path": PATH}, ensure_ascii=False),
            ),
        )
        result = await dispatcher.execute_tool_call(load, service=None)
        assert result.is_error is False, result.content
        payload = json.loads(result.content)
        aid = payload["artifact"]["artifact_id"]
        assert payload["content"] == CONTENT

        save = ToolCall(
            id="c18-2",
            function=FunctionCall(
                name="canvas.save_to_workspace",
                arguments=json.dumps({"artifact_id": aid}, ensure_ascii=False),
            ),
        )
        result = await dispatcher.execute_tool_call(save, service=None)
        assert result.is_error is False, result.content
        saved = json.loads(result.content)
        assert saved["save_state"] == "saved"
        assert saved["workspace_link"]["relative_path"] == PATH

        # a refusal reaches the model as an error envelope, not a fake success
        workspace.set_lifecycle_state(WS, "closed")
        closed = ToolCall(
            id="c18-3",
            function=FunctionCall(
                name="canvas.save_to_workspace",
                arguments=json.dumps({"artifact_id": aid}, ensure_ascii=False),
            ),
        )
        result = await dispatcher.execute_tool_call(closed, service=None)
        assert result.is_error is True
        assert json.loads(result.content)["code"] == "workspace_closed"
        workspace.set_lifecycle_state(WS, "open")

        # scope is never taken from arguments
        token2 = current_execution_scope.set(None)
        try:
            anon = ToolCall(
                id="c18-4",
                function=FunctionCall(
                    name="canvas.load_from_workspace",
                    arguments=json.dumps({"workspace_id": WS, "path": PATH}),
                ),
            )
            result = await dispatcher.execute_tool_call(anon, service=None)
            assert result.is_error is True
            assert json.loads(result.content)["code"] == "unauthenticated"
        finally:
            current_execution_scope.reset(token2)

        # a scope field is rejected by the closed schema before the handler runs
        injected = ToolCall(
            id="c18-5",
            function=FunctionCall(
                name="canvas.save_to_workspace",
                arguments=json.dumps({"artifact_id": aid, "owner_id": "u2"}),
            ),
        )
        result = await dispatcher.execute_tool_call(injected, service=None)
        assert result.is_error is True
        assert "owner_id" in result.content
    finally:
        current_execution_scope.reset(token)


@pytest.mark.asyncio
async def test_c18_apply_edit_boundary_and_r1_3_surface_are_unchanged(canvas, monkeypatch):
    monkeypatch.delenv(CORE_TOOLS_ENV, raising=False)
    monkeypatch.delenv(WORKSPACE_TOOLS_ENV, raising=False)
    default_names = {reg.name for reg in ToolRegistry().list_tools()}
    assert default_names == {
        "system.echo", "system.get_datetime", "math.calculate",
        "document.get_outline", "document.read_section", "document.search",
        "document.read_chunk", "document.get_figure_info",
    }

    install_canvas_tools(registry, ai_exposed_only=False)  # the admin surface, so apply_edit exists
    assert "canvas.apply_edit" not in AI_EXPOSED_TOOL_NAMES
    token = current_execution_scope.set(scope())
    try:
        refused = ToolCall(
            id="c18-apply",
            function=FunctionCall(
                name="canvas.apply_edit", arguments=json.dumps({"proposal_id": "prp_x"})
            ),
        )
        result = await dispatcher.execute_tool_call(refused, service=None)
        assert result.is_error is True
        payload = json.loads(result.content)
        assert payload["code"] == CanvasErrorCode.USER_APPROVAL_REQUIRED.value
        assert payload["context"]["tool_call_is_approval"] is False
    finally:
        current_execution_scope.reset(token)

    # Canvas never imports the R1.3 runtime policy/dispatcher internals
    from app.canvas import manager as manager_module
    from app.canvas import omni as omni_module
    from app.canvas import workspace as workspace_module

    for module in (manager_module, omni_module, workspace_module):
        source = inspect.getsource(module)
        assert "app.runtime" not in source, module.__name__


@pytest.mark.asyncio
async def test_c18_capabilities_report_the_switch_point_and_omni_contract(canvas):
    async with client() as c:
        caps = (await c.get(f"{BASE}/capabilities", headers=headers())).json()
    assert caps["schema_version"] == SCHEMA_VERSION == 3
    omni = caps["omni"]
    assert omni["contract"]["workspace_ssot_sha"] == "907482c"
    assert omni["switch_point"]["ssot"]["ref"] == "origin/feat/r2-workspace-rag"
    assert omni["switch_point"]["active_source"] in {"canvas_mirror", "app.workspace.contracts"}
    assert omni["switch_point"]["delegates_to_contract"] is False
    assert "validate_relative_path" in omni["switch_point"]["mirror_surface"]
    assert omni["gates"]["core_tools"] == CORE_TOOLS_ENV
    assert omni["gates"]["workspace_tools"] == WORKSPACE_TOOLS_ENV
    save = caps["workspace_save"]
    assert save["save"]["links"] == ["workspace_file_id", "relative_path", "last_saved_content_hash"]
    assert save["save"]["tool"] == "canvas.save_to_workspace"
    assert save["load"]["copy_semantics"] == "external_reference_with_edit_buffer"
    contract = caps["workspace"]["contract"]
    assert contract["revision"] == "r2-w/v1+a01"
    assert "read_file" in contract["read_operations"]
    assert contract["save_to_workspace"].endswith("/save-to-workspace")
    assert contract["load_from_workspace"].endswith("/from-workspace")
    text = str(caps).lower()
    for forbidden in ("authorization", "bearer", "password", "secret", "api_key", "x-hd2lab"):
        assert forbidden not in text
