"""R2 Canvas CP-03 — AI artifact generation (WO-C06), partial editing (WO-C07), tools (WO-C08)."""
from __future__ import annotations

import json

import pytest

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.events import ArtifactStreamSession, GenerationRequest
from app.canvas.models import PatchRequest
from app.canvas.offsets import Selection
from app.canvas.tools import CANVAS_TOOL_MANIFEST, install_canvas_tools, manifest
from app.core.auth import current_execution_scope
from app.schemas.gateway import FunctionCall, ToolCall
from app.tools.dispatcher import dispatcher
from app.tools.registry import registry
from tests.r2_canvas_support import BASE, client, headers, scope


# --------------------------------------------------------------------------- SSE contract


@pytest.mark.asyncio
async def test_generation_stream_creates_one_persisted_revision(canvas):
    async with client() as c:
        body = {
            "title": "AI 보고서",
            "artifact_type": "markdown",
            "content": "# 초안\n\n첫 문단입니다.\n둘째 문단입니다.",
            "chunk_size": 10,
            "idempotency_key": "gen-001",
        }
        r = await c.post(f"{BASE}/generations", headers=headers(), json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["committed"] is True
        assert payload["duplicate"] is False
        assert payload["revision"] == 1

        events = payload["events"]
        assert events[0]["event"] == "artifact.created"
        assert events[0]["data"]["provisional"] is True
        assert events[0]["data"]["persisted"] is False
        deltas = [event for event in events if event["event"] == "artifact.delta"]
        assert len(deltas) >= 2
        assert [event["data"]["seq"] for event in deltas] == list(range(1, len(deltas) + 1))
        assert all(event["data"]["provisional"] for event in deltas)
        assert events[-1]["event"] == "artifact.updated"
        assert events[-1]["data"]["persisted"] is True
        assert events[-1]["data"]["artifact_id"] == payload["artifact_id"]

        # exactly one artifact and one revision exist, built from the concatenated deltas
        r = await c.get(f"{BASE}/artifacts/{payload['artifact_id']}", headers=headers())
        assert r.json()["content"] == body["content"]
        assert r.json()["artifact"]["current_revision"] == 1
        assert r.json()["artifact"]["creation_source"] == "ai"
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 1


@pytest.mark.asyncio
async def test_aborted_or_failed_stream_is_never_persisted(canvas):
    async with client() as c:
        body = {
            "title": "중단된 스트림",
            "artifact_type": "markdown",
            "content": "일부만 생성된 내용",
            "simulate": "abort",
            "idempotency_key": "gen-abort",
        }
        r = await c.post(f"{BASE}/generations", headers=headers(), json=body)
        assert r.status_code == 200
        payload = r.json()
        assert payload["committed"] is False
        assert payload["artifact_id"] is None
        assert payload["events"][-1]["event"] == "artifact.error"
        assert payload["events"][-1]["data"]["code"] == "stream_aborted"
        assert payload["events"][-1]["data"]["persisted"] is False
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 0

        # a repeated delivery of the same aborted key stays unpersisted (no artifact is invented)
        r = await c.post(f"{BASE}/generations", headers=headers(), json=body)
        assert r.json()["committed"] is False
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_duplicate_generation_is_idempotent(canvas):
    async with client() as c:
        body = {
            "title": "중복 스트림",
            "artifact_type": "plain_text",
            "content": "내용",
            "idempotency_key": "gen-duplicate",
        }
        first = (await c.post(f"{BASE}/generations", headers=headers(), json=body)).json()
        second = (await c.post(f"{BASE}/generations", headers=headers(), json=body)).json()
        assert first["artifact_id"] == second["artifact_id"]
        assert second["duplicate"] is True
        assert second["revision"] == 1
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 1
        aid = first["artifact_id"]
        revisions = (await c.get(f"{BASE}/artifacts/{aid}/revisions", headers=headers())).json()
        assert revisions["total"] == 1


def test_duplicate_delta_frames_and_sequence_gaps(canvas):
    session = ArtifactStreamSession(canvas.manager, scope(), key="session-1")
    session.start(title="t", artifact_type="markdown", language=None, conversation_id=None, workspace_id=None)
    frame, duplicate = session.delta("가", seq=1)
    assert duplicate is False
    again, duplicate = session.delta("가", seq=1)
    assert duplicate is True
    assert again.event_id == frame.event_id
    assert session.delta("나")[0].data["seq"] == 2
    with pytest.raises(CanvasError) as exc:
        session.delta("다", seq=9)
    assert exc.value.code is CanvasErrorCode.INVALID_REQUEST
    assert "gap" in exc.value.message
    session.abort()
    assert session.status == "aborted"


@pytest.mark.asyncio
async def test_generation_sse_frames_use_existing_envelope(canvas):
    async with client() as c:
        body = {
            "title": "SSE 문서",
            "artifact_type": "markdown",
            "content": "본문 내용",
            "chunk_size": 2,
            "idempotency_key": "gen-sse",
        }
        r = await c.post(f"{BASE}/generations", headers=headers(), params={"stream": "true"}, json=body)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        text = r.text
        assert "event: artifact.created" in text
        assert "event: artifact.delta" in text
        assert "event: artifact.updated" in text
        data_lines = [line[len("data: "):] for line in text.splitlines() if line.startswith("data: ")]
        first = json.loads(data_lines[0])
        assert {"run_id", "event_id", "timestamp", "status", "artifact_id"} <= set(first)


@pytest.mark.asyncio
async def test_generation_rejects_unregistered_type_before_emitting(canvas):
    async with client() as c:
        r = await c.post(
            f"{BASE}/generations",
            headers=headers(),
            json={"title": "t", "artifact_type": "chart", "content": "x"},
        )
        assert r.status_code == 415
        assert r.json()["detail"]["code"] == "unsupported_artifact_type"
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 0

        # source_code without a language is refused the same way, still with nothing persisted
        r = await c.post(
            f"{BASE}/generations",
            headers=headers(),
            json={"title": "t", "artifact_type": "source_code", "content": "print(1)"},
        )
        assert r.status_code == 422
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_generation_fails_closed_when_content_exceeds_limit(configured):
    configured(AGCORE_CANVAS_MAX_CONTENT_BYTES=1024)
    async with client() as c:
        r = await c.post(
            f"{BASE}/generations",
            headers=headers(),
            json={"title": "t", "artifact_type": "plain_text", "content": "가" * 400, "simulate": "complete"},
        )
        assert r.status_code == 413
        assert r.json()["detail"]["code"] == "content_too_large"
        assert r.json()["detail"]["context"]["persisted"] is False
        r = await c.get(f"{BASE}/artifacts", headers=headers())
        assert r.json()["total"] == 0


# --------------------------------------------------------------------------- AI partial editing


async def _doc(c, content="첫 문장입니다. 둘째 문장입니다. 셋째 문장입니다."):
    r = await c.post(
        f"{BASE}/artifacts",
        headers=headers(),
        json={"title": "편집 문서", "artifact_type": "plain_text", "content": content},
    )
    return r.json()["artifact"]["artifact_id"], content


@pytest.mark.asyncio
async def test_edit_proposal_apply_and_reject(canvas):
    async with client() as c:
        aid, content = await _doc(c)
        body = {
            "instruction": "첫 문장을 더 짧게",
            "selection": {"start": 0, "end": 8, "offset_unit": "utf16"},
            "scripted_replacement": "첫 문장.",
            "change_reason": "길이 조절",
        }
        r = await c.post(f"{BASE}/artifacts/{aid}/edit-proposals", headers=headers(), json=body)
        assert r.status_code == 201, r.text
        proposal = r.json()["proposal"]
        assert proposal["status"] == "proposed"
        assert proposal["operation"] == "replace_selection"
        assert proposal["expected_original_text"] == content[0:8]
        assert proposal["ai_integration_verified"] is False
        assert proposal["adapter"] == "dev"
        assert r.json()["event"]["event"] == "artifact.edit.proposed"
        assert proposal["preview"]["would_change"] if "would_change" in proposal["preview"] else True
        # the artifact is untouched until the user applies
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.json()["content"] == content
        assert r.json()["artifact"]["current_revision"] == 1

        r = await c.post(f"{BASE}/edit-proposals/{proposal['proposal_id']}/apply", headers=headers(), json={})
        assert r.status_code == 200, r.text
        assert r.json()["applied"] is True
        assert r.json()["revision"]["revision"] == 2
        assert r.json()["revision"]["actor"] == "ai"
        assert r.json()["proposal"]["status"] == "applied"
        assert r.json()["event"]["event"] == "artifact.edit.applied"
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.json()["content"] == "첫 문장." + content[8:]

        # applying again is a no-op, not a second revision
        r = await c.post(f"{BASE}/edit-proposals/{proposal['proposal_id']}/apply", headers=headers(), json={})
        assert r.status_code == 200
        assert r.json()["duplicate"] is True
        r = await c.get(f"{BASE}/artifacts/{aid}/revisions", headers=headers())
        assert r.json()["total"] == 2

        # reject path keeps the artifact unchanged
        r = await c.post(
            f"{BASE}/artifacts/{aid}/edit-proposals",
            headers=headers(),
            json={
                "instruction": "문체 변경",
                "selection": {"start": 0, "end": 4},
                "scripted_replacement": "다른 문장",
            },
        )
        second = r.json()["proposal"]["proposal_id"]
        r = await c.post(f"{BASE}/edit-proposals/{second}/reject", headers=headers(), json={"reason": "불필요"})
        assert r.status_code == 200
        assert r.json()["rejected"] is True
        assert r.json()["event"]["event"] == "artifact.edit.rejected"
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.json()["artifact"]["current_revision"] == 2
        r = await c.post(f"{BASE}/edit-proposals/{second}/apply", headers=headers(), json={})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_proposal_becomes_stale_instead_of_overwriting_user_edit(canvas):
    async with client() as c:
        aid, content = await _doc(c)
        proposal = (
            await c.post(
                f"{BASE}/artifacts/{aid}/edit-proposals",
                headers=headers(),
                json={
                    "instruction": "첫 문장 수정",
                    "selection": {"start": 0, "end": 8},
                    "scripted_replacement": "AI 문장.",
                },
            )
        ).json()["proposal"]

        # the user edits the same region first
        r = await c.post(
            f"{BASE}/artifacts/{aid}/patches",
            headers=headers(),
            json={
                "base_revision": 1,
                "operation": "replace_selection",
                "selection": {"start": 0, "end": 8},
                "expected_original_text": content[0:8],
                "replacement_text": "사용자 문장.",
            },
        )
        assert r.status_code == 200

        r = await c.post(f"{BASE}/edit-proposals/{proposal['proposal_id']}/apply", headers=headers(), json={})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "proposal_stale"
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.json()["content"].startswith("사용자 문장.")
        r = await c.get(f"{BASE}/edit-proposals/{proposal['proposal_id']}", headers=headers())
        assert r.json()["proposal"]["status"] == "stale"
        assert r.json()["is_stale"] is True


@pytest.mark.asyncio
async def test_edit_context_budget_never_sends_the_whole_document(canvas):
    large = ("가나다라마바사아자차" * 2000) + "|선택|" + ("카타파하" * 2000)
    async with client() as c:
        aid = (
            await c.post(
                f"{BASE}/artifacts", headers=headers(),
                json={"title": "대문서", "artifact_type": "plain_text", "content": large},
            )
        ).json()["artifact"]["artifact_id"]
        start = large.index("|선택|")
        body = {
            "instruction": "이 부분만 다듬어줘",
            "selection": {"start": start, "end": start + 4, "offset_unit": "codepoint"},
            "budget_tokens": 400,
            "scripted_replacement": "다듬은 선택",
        }
        r = await c.post(f"{BASE}/artifacts/{aid}/edit-proposals", headers=headers(), json=body)
        assert r.status_code == 201, r.text
        envelope = r.json()["envelope"]
        assert envelope["budget"]["full_document_sent"] is False
        assert envelope["budget"]["omitted_before_chars"] > 0
        assert envelope["budget"]["omitted_after_chars"] > 0
        assert envelope["budget"]["estimated_tokens"] <= 400 * 2  # conservative estimate stays bounded
        assert len(envelope["context_text"]) < len(large) / 4
        assert envelope["content_is_untrusted"] is True
        assert envelope["selected_text"] == "|선택|"
        assert envelope["context_text"].startswith("<<<UNTRUSTED_ARTIFACT_CONTENT>>>")

        # a budget too small for the selection is refused instead of silently truncating it
        r = await c.post(
            f"{BASE}/artifacts/{aid}/edit-proposals",
            headers=headers(),
            json={
                "instruction": "수정",
                "selection": {"start": 0, "end": 400, "offset_unit": "codepoint"},
                "budget_tokens": 128,
                "scripted_replacement": "x",
            },
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_request"


def test_scope_override_in_edit_request_is_refused(canvas):
    artifact, _r, _d, _m = canvas.manager.create(
        scope(), title="가드", artifact_type="plain_text", content="본문"
    )
    with pytest.raises(CanvasError) as exc:
        canvas.proposals.create(
            scope(),
            artifact.artifact_id,
            instruction="수정",
            scripted_replacement="x",
            selection=Selection(start=0, end=2),
            extra={"tenant_id": "other_tenant"},
        )
    assert exc.value.code is CanvasErrorCode.FORBIDDEN
    assert exc.value.http_status == 403


@pytest.mark.asyncio
async def test_dev_adapter_refuses_without_script(canvas):
    async with client() as c:
        aid, _content = await _doc(c)
        r = await c.post(
            f"{BASE}/artifacts/{aid}/edit-proposals",
            headers=headers(),
            json={"instruction": "알아서 고쳐줘", "selection": {"start": 0, "end": 4}},
        )
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "ai_adapter_unavailable"
        assert r.json()["detail"]["context"]["ai_integration_verified"] is False

        # a documented deterministic dev rule still produces a proposal
        r = await c.post(
            f"{BASE}/artifacts/{aid}/edit-proposals",
            headers=headers(),
            json={"instruction": "대문자로", "selection": {"start": 0, "end": 4}, "dev_rule": "trim"},
        )
        assert r.status_code == 201


# --------------------------------------------------------------------------- tools


def test_tool_manifest_matches_registry_and_opt_in_registration(canvas):
    assert len(CANVAS_TOOL_MANIFEST) == 8
    names = {tool["name"] for tool in CANVAS_TOOL_MANIFEST}
    assert names == {
        "canvas.create_artifact", "canvas.read_artifact", "canvas.list_artifacts",
        "canvas.propose_edit", "canvas.apply_edit", "canvas.list_revisions",
        "canvas.restore_revision", "canvas.export_artifact",
    }
    for tool in CANVAS_TOOL_MANIFEST:
        assert tool["parameters"]["additionalProperties"] is False
        assert "tenant_id" not in tool["parameters"]["properties"]
        assert "owner_id" not in tool["parameters"]["properties"]
        assert tool["output_schema"]["type"] == "object"

    # R1.3 default runtime surface: the process registry carries no canvas.* tool without the
    # AGCORE_CANVAS_TOOLS_ENABLED opt-in.
    assert names.isdisjoint({reg.name for reg in registry.list_tools()})

    # Explicit registration (the opt-in path) adds them and is idempotent.
    assert set(install_canvas_tools(registry)) == names
    assert names <= {reg.name for reg in registry.list_tools()}
    assert set(install_canvas_tools(registry)) == names

    shipped = manifest()
    assert shipped["integration"]["verification"].startswith("dev API")
    assert shipped["scope"]["client_supplied_identity"] is False

    # Existing service profiles do not expose Canvas tools yet: runtime behaviour is unchanged.
    for service_id in ("hd2ai-admin", "hd2ai-public"):
        exposed = {reg.name for reg in registry.list_tools(service=service_id)}
        assert not (exposed & names), service_id


def test_canvas_tools_are_registered_only_with_the_opt_in_flag(monkeypatch):
    from app.tools.registry import CANVAS_TOOLS_ENV, ToolRegistry, canvas_tools_enabled

    names = {tool["name"] for tool in CANVAS_TOOL_MANIFEST}
    for value in ("", "0", "false", "no", "off", "enabled", "2"):
        monkeypatch.setenv(CANVAS_TOOLS_ENV, value)
        assert canvas_tools_enabled() is False, value
        assert names.isdisjoint({reg.name for reg in ToolRegistry().list_tools()}), value
    for value in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv(CANVAS_TOOLS_ENV, value)
        assert canvas_tools_enabled() is True, value
        assert names <= {reg.name for reg in ToolRegistry().list_tools()}, value
    monkeypatch.delenv(CANVAS_TOOLS_ENV, raising=False)
    assert canvas_tools_enabled() is False


@pytest.mark.asyncio
async def test_tool_dispatch_executes_real_artifact_operations(canvas):
    install_canvas_tools(registry)
    token = current_execution_scope.set(scope())
    try:
        create = ToolCall(
            id="call-1",
            function=FunctionCall(
                name="canvas.create_artifact",
                arguments=json.dumps(
                    {"title": "툴 문서", "artifact_type": "markdown", "content": "툴로 만든 본문"},
                    ensure_ascii=False,
                ),
            ),
        )
        result = await dispatcher.execute_tool_call(create, service=None)
        assert result.is_error is False, result.content
        payload = json.loads(result.content)
        assert payload["artifact"]["creation_source"] == "tool"
        aid = payload["artifact_id"]

        read = ToolCall(
            id="call-2",
            function=FunctionCall(name="canvas.read_artifact", arguments=json.dumps({"artifact_id": aid})),
        )
        result = await dispatcher.execute_tool_call(read, service=None)
        assert json.loads(result.content)["content"] == "툴로 만든 본문"

        listed = ToolCall(
            id="call-3",
            function=FunctionCall(name="canvas.list_artifacts", arguments="{}"),
        )
        result = await dispatcher.execute_tool_call(listed, service=None)
        assert json.loads(result.content)["total"] == 1

        exported = ToolCall(
            id="call-4",
            function=FunctionCall(
                name="canvas.export_artifact", arguments=json.dumps({"artifact_id": aid, "format": "md"})
            ),
        )
        result = await dispatcher.execute_tool_call(exported, service=None)
        assert json.loads(result.content)["content"] == "툴로 만든 본문"

        restore_without_revision = ToolCall(
            id="call-5",
            function=FunctionCall(
                name="canvas.restore_revision", arguments=json.dumps({"artifact_id": aid, "revision": 1})
            ),
        )
        result = await dispatcher.execute_tool_call(restore_without_revision, service=None)
        assert result.is_error is True
        assert json.loads(result.content)["code"] == "invalid_request"
    finally:
        current_execution_scope.reset(token)


@pytest.mark.asyncio
async def test_tool_call_without_scope_or_with_injected_identity_fails(canvas):
    install_canvas_tools(registry)
    token = current_execution_scope.set(None)
    try:
        call = ToolCall(
            id="call-x",
            function=FunctionCall(
                name="canvas.create_artifact",
                arguments=json.dumps({"title": "t", "artifact_type": "markdown", "content": "c"}),
            ),
        )
        result = await dispatcher.execute_tool_call(call, service=None)
        assert result.is_error is True
        assert json.loads(result.content)["code"] == "unauthenticated"
    finally:
        current_execution_scope.reset(token)

    token = current_execution_scope.set(scope())
    try:
        injected = ToolCall(
            id="call-y",
            function=FunctionCall(
                name="canvas.create_artifact",
                arguments=json.dumps(
                    {
                        "title": "t",
                        "artifact_type": "markdown",
                        "content": "c",
                        "tenant_id": "other_tenant",
                    }
                ),
            ),
        )
        result = await dispatcher.execute_tool_call(injected, service=None)
        assert result.is_error is True
        assert json.loads(result.content)["code"] == "invalid_arguments"
        # the rejected call created nothing
        listed = ToolCall(
            id="call-z",
            function=FunctionCall(name="canvas.list_artifacts", arguments="{}"),
        )
        result = await dispatcher.execute_tool_call(listed, service=None)
        assert json.loads(result.content)["total"] == 0
    finally:
        current_execution_scope.reset(token)


@pytest.mark.asyncio
async def test_tool_propose_and_apply_edit(canvas):
    install_canvas_tools(registry)
    artifact, _r, _d, _m = canvas.manager.create(
        scope(), title="툴 편집", artifact_type="plain_text", content="가나다라"
    )
    token = current_execution_scope.set(scope())
    try:
        propose = ToolCall(
            id="p-1",
            function=FunctionCall(
                name="canvas.propose_edit",
                arguments=json.dumps(
                    {
                        "artifact_id": artifact.artifact_id,
                        "instruction": "앞부분 수정",
                        "selection_start": 0,
                        "selection_end": 2,
                        "scripted_replacement": "하하",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        result = await dispatcher.execute_tool_call(propose, service=None)
        assert result.is_error is False, result.content
        payload = json.loads(result.content)
        assert payload["ai_integration_verified"] is False

        apply_call = ToolCall(
            id="p-2",
            function=FunctionCall(
                name="canvas.apply_edit",
                arguments=json.dumps({"proposal_id": payload["proposal"]["proposal_id"]}),
            ),
        )
        result = await dispatcher.execute_tool_call(apply_call, service=None)
        assert result.is_error is False, result.content
        assert json.loads(result.content)["revision"]["revision"] == 2
        assert canvas.manager.get(scope(), artifact.artifact_id)["content"] == "하하다라"

        revisions = ToolCall(
            id="p-3",
            function=FunctionCall(
                name="canvas.list_revisions", arguments=json.dumps({"artifact_id": artifact.artifact_id})
            ),
        )
        result = await dispatcher.execute_tool_call(revisions, service=None)
        assert json.loads(result.content)["total"] == 2
    finally:
        current_execution_scope.reset(token)
