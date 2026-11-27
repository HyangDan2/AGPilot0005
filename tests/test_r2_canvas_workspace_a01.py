"""R2 Canvas — R2-W Amendment 01 (folder-based workspace, ``r2-w/v1+a01``) consumer tests.

Scope of this module: only the behaviour Amendment 01 adds to Canvas. The frozen v1 paths are
covered by the pre-existing suites (foundation / revision_patch / ai / export_security /
correction, 63 tests) and are not re-run here.

Covered:

* ``409 workspace_closed`` on read / write / export / export-to-workspace, with the C-FIX-02
  detach path still open to the owner and a reopen restoring the workspace;
* ``403 read_only_workspace`` for every write on a read-only root;
* ``POST /artifacts/{id}/export-to-workspace`` against the §A1.5 write contract (path, content,
  expected_content_hash, create_parents, overwrite) for ``server_mounted_folder`` and
  ``local_browser`` (``client_write``);
* the shared ``validate_relative_path`` gate (``400 invalid_path``);
* ``409 content_conflict``, ``409 file_exists`` and ``400 provider_mismatch``;
* the full contract-error → Canvas-error bridge (all eight §A1.10 codes, plus the v1 codes Canvas
  now surfaces);
* runtime provider consumption of ``WorkspaceIdentity.lifecycle_state`` / ``root``;
* the dev provider stays production-refusing and validates its Amendment 01 setup values.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.workspace import (
    WORKSPACE_CONTRACT_AMENDMENT,
    WORKSPACE_CONTRACT_REVISION,
    WORKSPACE_CONTRACT_VERSION,
    WORKSPACE_ERROR_CODES,
    LocalDevWorkspaceProvider,
    RuntimeWorkspaceProvider,
    WorkspaceLifecycleState,
    WorkspaceStorageProvider,
    apply_workspace_identity,
    build_write_request,
    translate_workspace_error,
    validate_relative_path,
)
from tests.r2_canvas_support import BASE, client, headers, scope

content = "첫 문장입니다. 둘째 문장입니다."
WS = "ws_a01lab00001"


async def _create(c, *, title="A01 문서", body=content, **extra):
    r = await c.post(
        f"{BASE}/artifacts",
        headers=headers(),
        json={"title": title, "artifact_type": "markdown", "content": body, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()["artifact"]["artifact_id"]


async def _export_to_workspace(c, artifact_id, **payload):
    body = {"workspace_id": WS, **payload}
    return await c.post(
        f"{BASE}/artifacts/{artifact_id}/export-to-workspace", headers=headers(), json=body
    )


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- lifecycle: closed


@pytest.mark.asyncio
async def test_a01_closed_workspace_blocks_read_write_export_then_reopen_restores(canvas):
    assert isinstance(canvas.workspace, LocalDevWorkspaceProvider)
    canvas.workspace.add_workspace(WS, name="A01 lab", level="admin")
    async with client() as c:
        aid = await _create(c, workspace_id=WS)
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).status_code == 200

        canvas.workspace.set_lifecycle_state(WS, WorkspaceLifecycleState.CLOSED.value, reason="user closed")

        checks = (
            ("get", f"{BASE}/artifacts/{aid}", {}, None),
            ("get", f"{BASE}/artifacts/{aid}/export", {}, None),
            ("get", f"{BASE}/artifacts/{aid}/preview", {}, None),
            ("get", f"{BASE}/artifacts/{aid}/revisions", {}, None),
            ("get", f"{BASE}/artifacts/{aid}/diff", {"params": {"from_revision": 1, "to_revision": 1}}, None),
        )
        for method, path, kwargs, body in checks:
            response = await getattr(c, method)(path, headers=headers(), **kwargs)
            assert response.status_code == 409, (path, response.text)
            assert response.json()["detail"]["code"] == "workspace_closed", path
            assert response.json()["detail"]["retryable"] is True, path

        # write paths
        r = await c.post(
            f"{BASE}/artifacts/{aid}/patches",
            headers=headers(),
            json={"base_revision": 1, "operation": "append_content", "replacement_text": "추가"},
        )
        assert r.status_code == 409 and r.json()["detail"]["code"] == "workspace_closed"
        assert (await c.patch(f"{BASE}/artifacts/{aid}", headers=headers(), json={"title": "새 제목"})).status_code == 409

        # export to another (open) workspace still reads the artifact -> the artifact's own closed
        # workspace is the blocker, so the export answers workspace_closed as well
        canvas.workspace.add_workspace("ws_a01open0001", name="open target", level="admin")
        r = await _export_to_workspace(c, aid, workspace_id="ws_a01open0001", path="out/doc.md")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "workspace_closed"
        assert canvas.workspace.list_files("ws_a01open0001") == {}

        # metadata is kept (contract §A1.2): the artifact and its revision are still there
        stored = canvas.storage.get_artifact(aid)
        assert stored is not None and stored.current_revision == 1

        # C-FIX-02: detaching the link stays allowed for the owner even while closed
        r = await c.delete(f"{BASE}/artifacts/{aid}/links/workspace", headers=headers())
        assert r.status_code == 200, r.text
        assert r.json()["artifact"]["workspace_id"] is None
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).status_code == 200

        # reopening restores every path on a workspace that is still linked
        canvas.workspace.add_workspace("ws_a01reopen01", name="reopen", level="admin")
        reopened = await _create(c, title="재개 문서", workspace_id="ws_a01reopen01")
        canvas.workspace.set_lifecycle_state("ws_a01reopen01", WorkspaceLifecycleState.CLOSED.value)
        assert (await c.get(f"{BASE}/artifacts/{reopened}", headers=headers())).status_code == 409
        canvas.workspace.set_lifecycle_state("ws_a01reopen01", WorkspaceLifecycleState.OPEN.value)
        assert (await c.get(f"{BASE}/artifacts/{reopened}", headers=headers())).status_code == 200


@pytest.mark.asyncio
async def test_a01_deleted_lifecycle_is_reported_as_workspace_not_found(canvas):
    canvas.workspace.add_workspace(WS, name="A01 lab", level="admin")
    async with client() as c:
        aid = await _create(c, workspace_id=WS)
        canvas.workspace.set_lifecycle_state(WS, WorkspaceLifecycleState.DELETED.value)

        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "workspace_not_found"

        # detach is still the owner's way out
        r = await c.delete(f"{BASE}/artifacts/{aid}/links/workspace", headers=headers())
        assert r.status_code == 200
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).status_code == 200


# --------------------------------------------------------------------------- lifecycle: read-only


@pytest.mark.asyncio
async def test_a01_read_only_root_refuses_every_write_but_allows_reads(canvas):
    canvas.workspace.add_workspace(WS, name="읽기 전용 루트", level="admin", read_only=True)
    async with client() as c:
        # creating *into* a read-only workspace is a write
        r = await c.post(
            f"{BASE}/artifacts",
            headers=headers(),
            json={"title": "읽기 전용 시도", "artifact_type": "markdown", "content": "본문", "workspace_id": WS},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "read_only_workspace"

        aid = await _create(c, title="대상 문서")
        canvas.workspace.set_read_only(WS, False)
        r = await c.post(f"{BASE}/artifacts/{aid}/links", headers=headers(), json={"workspace_id": WS})
        assert r.status_code == 200, r.text
        canvas.workspace.set_read_only(WS, True)

        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).status_code == 200

        r = await c.post(
            f"{BASE}/artifacts/{aid}/patches",
            headers=headers(),
            json={"base_revision": 1, "operation": "append_content", "replacement_text": "추가"},
        )
        assert r.status_code == 403 and r.json()["detail"]["code"] == "read_only_workspace"

        r = await _export_to_workspace(c, aid, path="reports/summary.md")
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "read_only_workspace"
        assert r.json()["detail"]["context"]["storage_provider"] == "server_managed"
        assert canvas.workspace.list_files(WS) == {}


# --------------------------------------------------------------------------- export to workspace


@pytest.mark.asyncio
async def test_a01_export_to_workspace_writes_the_contract_request(canvas):
    canvas.workspace.add_workspace(WS, name="lab-docs", level="admin")
    canvas.workspace.set_storage_provider(WS, WorkspaceStorageProvider.SERVER_MOUNTED_FOLDER.value)
    async with client() as c:
        aid = await _create(c, title="요약 보고서")

        r = await _export_to_workspace(c, aid, path="reports/summary.md", mime_type="text/markdown")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["workspace_id"] == WS
        assert body["path"] == "reports/summary.md"
        assert body["storage_provider"] == "server_mounted_folder"
        assert body["content_origin"] == "external"
        assert body["write_mode"] == "server_write"
        assert body["client_write"] is None
        assert body["server_side_path_access"] is True
        assert body["artifact_revision"] == 1
        assert body["format"] == "md"
        assert body["media_type"].startswith("text/markdown")
        assert body["content_hash"] == _sha(content)
        assert body["written_bytes"] == len(content.encode("utf-8"))
        assert body["workspace_file_revision"] == 1
        assert body["workspace_file"]["relative_path"] == "reports/summary.md"
        assert body["workspace_file"]["content_origin"] == "external"
        assert body["workspace_access"]["lifecycle_state"] == "open"
        assert body["contract_revision"] == WORKSPACE_CONTRACT_REVISION

        files = canvas.workspace.list_files(WS)
        assert files["reports/summary.md"]["content"] == content
        assert files["reports/summary.md"]["content_hash"] == _sha(content)

        # a later artifact revision is exportable on its own
        r2 = await c.patch(f"{BASE}/artifacts/{aid}", headers=headers(), json={"title": "요약 보고서 v2"})
        assert r2.status_code == 200
        r = await _export_to_workspace(c, aid, path="reports/summary.md")
        assert r.status_code == 200
        assert r.json()["workspace_file_revision"] == 2

        # no path given -> the export filename derived from the (sanitized) title
        r = await _export_to_workspace(c, aid)
        assert r.status_code == 200
        assert r.json()["path"] == "요약 보고서 v2.md"
        assert r.json()["workspace_file"]["name"] == "요약 보고서 v2.md"


@pytest.mark.asyncio
async def test_a01_local_browser_export_returns_client_write_and_no_server_path(canvas):
    canvas.workspace.add_workspace(WS, name="브라우저 폴더", level="admin")
    canvas.workspace.set_storage_provider(
        WS, WorkspaceStorageProvider.LOCAL_BROWSER.value,
        client_handle_id="handle-7", display_path="/Users/shim/Documents/노트",
    )
    async with client() as c:
        aid = await _create(c, title="브라우저 문서")
        r = await _export_to_workspace(c, aid, path="노트/메모.md")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["storage_provider"] == "local_browser"
        assert body["content_origin"] == "external"
        assert body["write_mode"] == "client_write"
        assert body["server_side_path_access"] is False
        # the server never claims a server-side path for a browser-owned root
        assert body["workspace_root_path"] is None
        assert body["client_write"] == {
            "required": True,
            "client_handle_id": "handle-7",
            "relative_path": "노트/메모.md",
            "reason": "local_browser_no_server_path_access",
        }
        assert body["workspace_file"] is None
        assert body["written_bytes"] == 0
        # nothing was written on the server side
        assert canvas.workspace.list_files(WS) == {}


@pytest.mark.asyncio
async def test_a01_export_to_workspace_rejects_paths_outside_the_root(canvas):
    canvas.workspace.add_workspace(WS, name="lab", level="admin")
    async with client() as c:
        aid = await _create(c, title="경로 문서")
        for bad in ("/etc/passwd", "../escape.md", "a/./b.md", "a//b.md", "C:evil.md", "a\\b.md", ".."):
            r = await _export_to_workspace(c, aid, path=bad)
            assert r.status_code == 400, (bad, r.text)
            assert r.json()["detail"]["code"] == "invalid_path", bad
            assert r.json()["detail"]["context"]["path"] == bad
        assert canvas.workspace.list_files(WS) == {}
        # the artifact and its revision are untouched by the rejected writes
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).json()["content"] == content


@pytest.mark.asyncio
async def test_a01_export_to_workspace_content_conflict_and_file_exists(canvas):
    canvas.workspace.add_workspace(WS, name="lab", level="admin")
    async with client() as c:
        aid = await _create(c, title="충돌 문서")
        canvas.workspace.seed_file(WS, "reports/summary.md", "다른 에이전트가 먼저 쓴 내용")

        r = await _export_to_workspace(c, aid, path="reports/summary.md", expected_content_hash="sha256:deadbeef")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "content_conflict"
        assert detail["context"]["expected_content_hash"] == "sha256:deadbeef"
        assert canvas.workspace.list_files(WS)["reports/summary.md"]["content"] == "다른 에이전트가 먼저 쓴 내용"

        r = await _export_to_workspace(c, aid, path="reports/summary.md", overwrite=False)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "file_exists"

        # matching hash + overwrite=true replaces the file and bumps its revision
        current = canvas.workspace.list_files(WS)["reports/summary.md"]["content_hash"]
        r = await _export_to_workspace(c, aid, path="reports/summary.md", expected_content_hash=current)
        assert r.status_code == 200, r.text
        assert r.json()["workspace_file_revision"] == 2
        assert canvas.workspace.list_files(WS)["reports/summary.md"]["content"] == content


# --------------------------------------------------------------------------- provider bridge


class _V1OnlyProvider:
    """A provider that implements only the frozen v1 operations (no get_workspace/write_file)."""

    def __init__(self, level: str = "admin") -> None:
        self.level = level
        self.references: list = []

    def check_workspace_access(self, workspace_id, execution_scope):
        return SimpleNamespace(workspace_id=workspace_id, level=self.level, reason="owner")

    def register_artifact_reference(self, workspace_id, execution_scope, ref):
        self.references.append(ref)
        return {"artifact_ref_id": f"wref_dev{len(self.references)}", "artifact_id": ref["artifact_id"]}


class _A01Provider(_V1OnlyProvider):
    """A v1+Amendment 01 provider whose identity carries the root and lifecycle."""

    def __init__(self, *, lifecycle_state="open", root=None, level="admin", write_error=None):
        super().__init__(level=level)
        self.lifecycle_state = lifecycle_state
        self.root = root
        self.write_error = write_error
        self.writes: list = []

    def get_workspace(self, workspace_id, execution_scope):
        return SimpleNamespace(
            workspace_id=workspace_id,
            lifecycle_state=self.lifecycle_state,
            root=self.root,
        )

    def write_file(self, workspace_id, execution_scope, request):
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(request)
        return SimpleNamespace(
            workspace_id=workspace_id,
            path=getattr(request, "path"),
            file=None,
            revision=1,
            content_hash=None,
            written_bytes=len(content.encode("utf-8")),
            storage_provider="server_mounted_folder",
            content_origin="external",
            client_write=None,
        )


def _bind_runtime_provider(provider):
    """Rebuild the Canvas service singleton around a runtime provider (test-only wiring)."""

    from app.canvas import service as service_module
    from app.canvas.config import load_canvas_settings
    from app.canvas.service import CanvasService

    built = CanvasService(load_canvas_settings(), workspace=RuntimeWorkspaceProvider(provider))
    service_module._service = built
    return built


def _root(**overrides):
    payload = {
        "storage_provider": "server_mounted_folder",
        "root_id": "lab",
        "display_path": "/srv/hd2lab/workspaces/lab-docs",
        "allowed_root_id": "lab",
        "read_only": False,
        "zero_copy": True,
        "server_side_path_access": True,
        "client_handle_id": None,
        "opened_at": "2026-11-27T02:41:09.004Z",
        "status": "available",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


@pytest.mark.asyncio
async def test_a01_runtime_provider_consumes_identity_lifecycle_and_root(canvas):
    provider = _A01Provider(root=_root())
    _bind_runtime_provider(provider)
    async with client() as c:
        aid = await _create(c, workspace_id=WS)
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).status_code == 200

        provider.lifecycle_state = WorkspaceLifecycleState.CLOSED.value
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "workspace_closed"

        provider.lifecycle_state = WorkspaceLifecycleState.OPEN.value
        provider.root = _root(read_only=True)
        r = await c.post(
            f"{BASE}/artifacts/{aid}/patches",
            headers=headers(),
            json={"base_revision": 1, "operation": "append_content", "replacement_text": "추가"},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "read_only_workspace"

        provider.root = _root()
        r = await _export_to_workspace(c, aid, path="reports/summary.md")
        assert r.status_code == 200, r.text
        assert r.json()["storage_provider"] == "server_mounted_folder"
        assert r.json()["workspace_root_path"] == "/srv/hd2lab/workspaces/lab-docs"
        assert provider.writes and provider.writes[0].path == "reports/summary.md"
        assert provider.writes[0].content == content
        assert provider.writes[0].overwrite is True
        assert provider.writes[0].create_parents is True


@pytest.mark.asyncio
async def test_a01_v1_only_provider_keeps_working_but_cannot_write(canvas):
    provider = _V1OnlyProvider()
    _bind_runtime_provider(provider)
    async with client() as c:
        # v1 compatibility: access/link/reference stay exactly as before
        aid = await _create(c, workspace_id=WS)
        r = await c.post(f"{BASE}/artifacts/{aid}/links", headers=headers(), json={"workspace_id": WS})
        assert r.status_code == 200, r.text
        assert provider.references and provider.references[0]["artifact_id"] == aid
        assert (await c.get(f"{BASE}/artifacts/{aid}", headers=headers())).status_code == 200

        # export to workspace needs the Amendment 01 write contract -> explicit mismatch
        r = await _export_to_workspace(c, aid, path="reports/summary.md")
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "provider_mismatch"
        assert detail["context"]["required_operation"] == "write_file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contract_code,status",
    [
        ("workspace_closed", 409),
        ("read_only_workspace", 403),
        ("path_outside_allowed_root", 403),
        ("symlink_escape", 400),
        ("root_unavailable", 409),
        ("provider_mismatch", 400),
        ("content_conflict", 409),
        ("source_delete_not_permitted", 403),
        ("metadata_overlap", 400),
        ("watch_unavailable", 409),
        ("invalid_path", 400),
        ("file_exists", 409),
        ("workspace_not_found", 404),
    ],
)
async def test_a01_contract_error_codes_reach_the_canvas_envelope(canvas, contract_code, status):
    provider = _A01Provider(
        root=_root(),
        write_error=HTTPException(
            status_code=500, detail={"code": contract_code, "message": f"engine said {contract_code}", "retryable": False}
        ),
    )
    _bind_runtime_provider(provider)
    async with client() as c:
        aid = await _create(c, title="오류 문서")
        r = await _export_to_workspace(c, aid, path="reports/summary.md")
        assert r.status_code == status, (contract_code, r.text)
        detail = r.json()["detail"]
        assert detail["code"] == contract_code
        assert detail["context"]["provider_code"] == contract_code
        assert detail["context"]["workspace_id"] == WS


def test_a01_error_bridge_covers_the_whole_contract_table():
    from app.canvas.errors import ERROR_STATUS

    assert len(WORKSPACE_ERROR_CODES) == 22
    for contract_code, canvas_code in WORKSPACE_ERROR_CODES.items():
        exc = HTTPException(status_code=500, detail={"code": contract_code, "message": ""})
        mapped = translate_workspace_error(exc, workspace_id=WS, operation="write_file")
        assert mapped.code is canvas_code, contract_code
        assert mapped.http_status == ERROR_STATUS[canvas_code], contract_code
    # an unknown engine failure is never reported as a client error
    unknown = translate_workspace_error(RuntimeError("boom"), workspace_id=WS, operation="write_file")
    assert unknown.code is CanvasErrorCode.WORKSPACE_UNAVAILABLE
    assert unknown.http_status == 503 and unknown.retryable is True


def test_a01_contract_constants_and_path_gate():
    assert WORKSPACE_CONTRACT_VERSION == "r2-w/v1"
    assert WORKSPACE_CONTRACT_AMENDMENT == "A01"
    assert WORKSPACE_CONTRACT_REVISION == "r2-w/v1+a01"

    assert validate_relative_path("reports/summary.md") == "reports/summary.md"
    assert validate_relative_path("한글 폴더/문서.md") == "한글 폴더/문서.md"
    for bad in ("", " ", "/abs.md", "~/abs.md", "..", "a/../b", "a/./b", "C:evil", "a\\b", "a\x00b", "a" * 256):
        with pytest.raises(CanvasError) as exc:
            validate_relative_path(bad)
        assert exc.value.code is CanvasErrorCode.INVALID_PATH, bad
        assert exc.value.http_status == 400

    # binary content travels as content_base64 (contract §A1.5)
    request = build_write_request(path="bin/data.bin", content=b"\xff\xfe\x00binary")
    assert getattr(request, "content", None) is None
    assert getattr(request, "content_base64") == "//4AYmluYXJ5"


def test_a01_identity_never_echoes_a_client_path():
    from app.canvas.workspace import WorkspaceAccess

    access = WorkspaceAccess(workspace_id=WS, level="admin")
    local = apply_workspace_identity(
        access,
        SimpleNamespace(
            lifecycle_state="open",
            root=_root(
                storage_provider="local_browser",
                display_path="/Users/shim/Documents/노트",
                client_handle_id="handle-7",
                server_side_path_access=True,  # a misbehaving provider
            ),
        ),
    )
    assert local.storage_provider == "local_browser"
    assert local.server_side_path_access is False
    assert local.root_display_path == ""

    mounted = apply_workspace_identity(access, SimpleNamespace(lifecycle_state="open", root=_root()))
    assert mounted.storage_provider == "server_mounted_folder"
    assert mounted.root_display_path == "/srv/hd2lab/workspaces/lab-docs"


def test_a01_dev_provider_state_setup_and_production_refusal():
    with pytest.raises(RuntimeError):
        LocalDevWorkspaceProvider(production=True)

    provider = LocalDevWorkspaceProvider()
    provider.add_workspace(
        "ws_devsetup001",
        name="setup",
        level="write",
        lifecycle_state="open",
        storage_provider="server_mounted_folder",
        read_only=True,
    )
    access = provider.check_access("ws_devsetup001", scope())
    assert access.storage_provider == "server_mounted_folder"
    assert access.read_only is True
    assert access.lifecycle_state == "open"
    assert access.contract_revision == WORKSPACE_CONTRACT_REVISION

    provider.set_lifecycle_state("ws_devsetup001", "closed")
    assert provider.check_access("ws_devsetup001", scope()).lifecycle_state == "closed"
    provider.set_read_only("ws_devsetup001", False)
    assert provider.check_access("ws_devsetup001", scope()).read_only is False

    with pytest.raises(ValueError):
        provider.set_lifecycle_state("ws_devsetup001", "paused")
    with pytest.raises(ValueError):
        provider.set_storage_provider("ws_devsetup001", "network_drive")
    with pytest.raises(ValueError):
        provider.set_level("ws_devsetup001", "owner")

    # even a direct provider write refuses a closed / read-only root
    request = build_write_request(path="a.md", content=b"x")
    provider.set_lifecycle_state("ws_devsetup001", "closed")
    with pytest.raises(CanvasError) as closed:
        provider.write_file("ws_devsetup001", scope(), request)
    assert closed.value.code is CanvasErrorCode.WORKSPACE_CLOSED
    provider.set_lifecycle_state("ws_devsetup001", "open")
    provider.set_read_only("ws_devsetup001", True)
    with pytest.raises(CanvasError) as read_only:
        provider.write_file("ws_devsetup001", scope(), request)
    assert read_only.value.code is CanvasErrorCode.READ_ONLY_WORKSPACE


@pytest.mark.asyncio
async def test_a01_capabilities_report_the_consumed_revision(canvas):
    async with client() as c:
        body = (await c.get(f"{BASE}/capabilities", headers=headers())).json()
    workspace = body["workspace"]
    assert workspace["contract"]["version"] == "r2-w/v1"
    assert workspace["contract"]["amendment"] == "A01"
    assert workspace["contract"]["revision"] == "r2-w/v1+a01"
    assert "write_file" in workspace["contract"]["a01_operations"]
    assert workspace["lifecycle_enforcement"]["closed"].startswith("409 workspace_closed")
    assert workspace["lifecycle_enforcement"]["local_browser"].startswith("no server-side path access")
    assert "write_file (Amendment 01 / export to workspace)" in workspace["operations"]
