"""R2 Canvas CP-02 — Revision Manager (WO-C04) and Patch & Diff Engine (WO-C05)."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.models import PatchRequest
from app.canvas.offsets import Selection
from tests.r2_canvas_support import BASE, client, headers, scope


def _patch(aid: str, base: int, operation: str, **kwargs) -> PatchRequest:
    return PatchRequest(artifact_id=aid, base_revision=base, operation=operation, **kwargs)


@pytest.mark.asyncio
async def test_revision_history_diff_and_non_destructive_restore(canvas):
    artifact, _revision, _dup, _meta = canvas.manager.create(
        scope(), title="이력 문서", artifact_type="markdown", content="첫 줄\n둘째 줄\n셋째 줄"
    )
    aid = artifact.artifact_id

    first = canvas.patches.apply(
        scope(),
        _patch(aid, 1, "replace_selection", selection=Selection(start=0, end=3), expected_original_text="첫 줄",
               replacement_text="1번째 줄", change_reason="표기 변경"),
    )
    assert first.revision.revision == 2
    assert first.revision.actor == "user"
    assert first.revision.change_reason == "표기 변경"
    assert first.revision.operation == "replace_selection"
    assert first.revision.base_revision == 1

    ai = canvas.patches.apply(
        scope(),
        PatchRequest(
            artifact_id=aid, base_revision=2, operation="append_content", replacement_text="\n넷째 줄",
            actor="ai", change_reason="AI 제안 반영",
        ),
    )
    assert ai.revision.revision == 3
    assert ai.revision.actor == "ai"

    imported, imported_revision, _dup, _meta = canvas.manager.create(
        scope(), title="가져온 문서", artifact_type="plain_text", content="x", creation_source="import"
    )
    assert imported.creation_source == "import"
    assert imported_revision.actor == "import"

    history = canvas.revisions.history(scope(), aid)
    assert [item.revision for item in history.items] == [3, 2, 1]
    assert history.total == 3

    old = canvas.revisions.get(scope(), aid, 1)
    assert old["content"] == "첫 줄\n둘째 줄\n셋째 줄"
    assert old["is_current"] is False

    diff = canvas.revisions.diff(scope(), aid, from_revision=1, to_revision=3)
    assert diff["identical"] is False
    assert diff["stats"]["hunks"] >= 1
    assert "1번째 줄" in diff["unified"]
    assert diff["from_revision"] == 1 and diff["to_revision"] == 3
    assert diff["emphasis_unit"] == "codepoint"

    restored = canvas.revisions.restore(scope(), aid, 1, change_reason="1판으로 복원")
    assert restored.revision.revision == 4
    assert restored.revision.operation == "replace_content"
    assert restored.revision.change_reason == "1판으로 복원"
    assert restored.artifact.current_revision == 4
    assert canvas.revisions.get(scope(), aid, 4)["content"] == "첫 줄\n둘째 줄\n셋째 줄"
    # history is preserved: nothing was deleted or overwritten
    history = canvas.revisions.history(scope(), aid)
    assert [item.revision for item in history.items] == [4, 3, 2, 1]

    with pytest.raises(CanvasError) as exc:
        canvas.revisions.restore(scope(), aid, 4)
    assert exc.value.code is CanvasErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_all_six_patch_operations(canvas):
    artifact, _r, _d, _m = canvas.manager.create(
        scope(), title="연산 문서", artifact_type="plain_text", content="HEAD\nBODY\nTAIL"
    )
    aid = artifact.artifact_id

    out = canvas.patches.apply(
        scope(), _patch(aid, 1, "replace_selection", selection=Selection(start=5, end=9),
                        expected_original_text="BODY", replacement_text="본문")
    )
    assert out.changed and canvas.manager.get(scope(), aid)["content"] == "HEAD\n본문\nTAIL"

    out = canvas.patches.apply(
        scope(), _patch(aid, 2, "insert_before", selection=Selection(start=0, end=0),
                        expected_original_text="", replacement_text="[앞]\n")
    )
    assert canvas.manager.get(scope(), aid)["content"].startswith("[앞]\nHEAD")

    out = canvas.patches.apply(
        scope(), _patch(aid, 3, "insert_after", selection=Selection(start=8, end=8),
                        expected_original_text="", replacement_text="!")
    )
    assert canvas.manager.get(scope(), aid)["content"].startswith("[앞]\nHEAD!")

    out = canvas.patches.apply(
        scope(), _patch(aid, 4, "append_content", replacement_text="\n[끝]")
    )
    assert canvas.manager.get(scope(), aid)["content"].endswith("[끝]")

    before_delete = canvas.manager.get(scope(), aid)["content"]
    canvas.patches.apply(
        scope(), _patch(aid, 5, "delete_selection", selection=Selection(start=0, end=4),
                        expected_original_text=before_delete[0:4])
    )
    assert canvas.manager.get(scope(), aid)["content"] == before_delete[4:]

    replaced = canvas.patches.apply(
        scope(), _patch(aid, 6, "replace_content", replacement_text="완전히 새 문서",
                        expected_original_text=canvas.manager.get(scope(), aid)["content"])
    )
    assert replaced.artifact.current_revision == 7
    assert canvas.manager.get(scope(), aid)["content"] == "완전히 새 문서"

    # replace_content without confirmation text is refused only when it cannot be verified;
    # an empty replacement is allowed (clearing the document).
    cleared = canvas.patches.apply(
        scope(), _patch(aid, 7, "replace_content", replacement_text="")
    )
    assert cleared.changed and canvas.manager.get(scope(), aid)["content"] == ""


@pytest.mark.asyncio
async def test_korean_and_unicode_offset_alignment(canvas):
    artifact, _r, _d, _m = canvas.manager.create(
        scope(), title="유니코드", artifact_type="plain_text", content="가😀나다"
    )
    aid = artifact.artifact_id

    info = canvas.manager.get(scope(), aid)
    assert info["content_metrics"] == {"codepoints": 4, "utf16_units": 5, "utf8_bytes": 13}

    # utf16 offsets, as a browser textarea reports them: select the emoji (units 1..3)
    out = canvas.patches.apply(
        scope(),
        _patch(aid, 1, "replace_selection", selection=Selection(start=1, end=3, offset_unit="utf16"),
               expected_original_text="😀", replacement_text="🙂"),
    )
    assert canvas.manager.get(scope(), aid)["content"] == "가🙂나다"
    assert out.applied_range_utf16 == {"start": 1, "end": 3}

    # same range expressed in code points gives the same result
    canvas.patches.apply(
        scope(),
        _patch(aid, 2, "replace_selection", selection=Selection(start=1, end=2, offset_unit="codepoint"),
               expected_original_text="🙂", replacement_text="😀"),
    )
    assert canvas.manager.get(scope(), aid)["content"] == "가😀나다"

    # an offset that would split a surrogate pair cannot come from a real selection
    with pytest.raises(CanvasError) as exc:
        canvas.patches.apply(
            scope(),
            _patch(aid, 3, "replace_selection", selection=Selection(start=2, end=3, offset_unit="utf16"),
                   expected_original_text="", replacement_text="x"),
        )
    assert exc.value.code is CanvasErrorCode.INVALID_SELECTION

    # Korean text: codepoint and utf16 agree
    canvas.patches.apply(
        scope(),
        _patch(aid, 3, "replace_selection", selection=Selection(start=2, end=3, offset_unit="codepoint"),
               expected_original_text="나", replacement_text="너"),
    )
    assert canvas.manager.get(scope(), aid)["content"] == "가😀너다"


@pytest.mark.asyncio
async def test_conflict_detection_rejects_stale_and_mismatched_patches(canvas):
    artifact, _r, _d, _m = canvas.manager.create(
        scope(), title="충돌", artifact_type="markdown", content="원본 문장"
    )
    aid = artifact.artifact_id
    canvas.patches.apply(scope(), _patch(aid, 1, "append_content", replacement_text=" 추가"))

    # stale base revision
    with pytest.raises(CanvasError) as exc:
        canvas.patches.apply(
            scope(), _patch(aid, 1, "replace_selection", selection=Selection(start=0, end=2),
                            expected_original_text="원본", replacement_text="덮어쓰기")
        )
    assert exc.value.code is CanvasErrorCode.REVISION_CONFLICT
    assert exc.value.http_status == 409
    assert exc.value.context["current_revision"] == 2

    # current revision but stale text expectation (e.g. another client edited earlier)
    with pytest.raises(CanvasError) as exc:
        canvas.patches.apply(
            scope(), _patch(aid, 2, "replace_selection", selection=Selection(start=0, end=2),
                            expected_original_text="다른", replacement_text="덮어쓰기")
        )
    assert exc.value.code is CanvasErrorCode.EXPECTED_TEXT_MISMATCH
    assert exc.value.http_status == 409
    assert canvas.manager.get(scope(), aid)["content"] == "원본 문장 추가"

    # omitted expectation is not a free pass: it is refused as an unverifiable patch
    with pytest.raises(CanvasError) as exc:
        canvas.patches.apply(
            scope(), _patch(aid, 2, "replace_selection", selection=Selection(start=0, end=2),
                            replacement_text="덮어쓰기")
        )
    assert exc.value.code is CanvasErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_patch_preview_does_not_write(canvas):
    artifact, _r, _d, _m = canvas.manager.create(
        scope(), title="미리보기", artifact_type="markdown", content="가나다"
    )
    aid = artifact.artifact_id
    preview = canvas.patches.preview(
        scope(),
        _patch(aid, 1, "replace_selection", selection=Selection(start=0, end=1),
               expected_original_text="가", replacement_text="하"),
    )
    assert preview["would_change"] is True
    assert preview["from_revision"] == 1
    assert preview["applied_range_utf16"] == {"start": 0, "end": 1}
    assert canvas.manager.get(scope(), aid)["revision"]["revision"] == 1
    assert canvas.manager.get(scope(), aid)["content"] == "가나다"

    noop = canvas.patches.apply(
        scope(), _patch(aid, 1, "replace_selection", selection=Selection(start=0, end=1),
                        expected_original_text="가", replacement_text="가")
    )
    assert noop.changed is False
    assert noop.artifact.current_revision == 1
    assert canvas.storage.count_revisions(aid) == 1

    with pytest.raises(CanvasError) as exc:
        canvas.patches.preview(
            scope(), _patch(aid, 9, "append_content", replacement_text="x")
        )
    assert exc.value.code is CanvasErrorCode.REVISION_CONFLICT


@pytest.mark.asyncio
async def test_concurrent_writes_lose_no_changes(canvas):
    artifact, _r, _d, _m = canvas.manager.create(
        scope(), title="동시 저장", artifact_type="plain_text", content="기준"
    )
    aid = artifact.artifact_id
    barrier = threading.Barrier(2)
    results: list = []

    def writer(label: str):
        barrier.wait(timeout=10)
        try:
            outcome = canvas.patches.apply(
                scope(),
                _patch(aid, 1, "append_content", replacement_text=f"-{label}"),
            )
            results.append(("ok", label, outcome.revision.revision))
        except CanvasError as exc:
            results.append(("conflict", label, exc.code.value))

    with ThreadPoolExecutor(max_workers=2) as pool:
        for label in ("A", "B"):
            pool.submit(writer, label)

    assert sorted(status for status, *_ in results) == ["conflict", "ok"]
    winner = next(label for status, label, *_ in results if status == "ok")
    loser = next(label for status, label, *_ in results if status == "conflict")
    content = canvas.manager.get(scope(), aid)["content"]
    assert content == f"기준-{winner}"
    assert f"-{loser}" not in content
    assert canvas.storage.count_revisions(aid) == 2
    assert [entry[2] for entry in results if entry[0] == "conflict"] == ["revision_conflict"]


@pytest.mark.asyncio
async def test_patch_api_contract(canvas):
    async with client() as c:
        r = await c.post(
            f"{BASE}/artifacts",
            headers=headers(),
            json={"title": "API 패치", "artifact_type": "markdown", "content": "가나다"},
        )
        aid = r.json()["artifact"]["artifact_id"]
        body = {
            "base_revision": 1,
            "operation": "replace_selection",
            "selection": {"start": 1, "end": 2, "offset_unit": "utf16"},
            "expected_original_text": "나",
            "replacement_text": "너",
            "change_reason": "선택 영역 수정",
        }
        r = await c.post(f"{BASE}/artifacts/{aid}/patches", headers=headers(), json=body)
        assert r.status_code == 200, r.text
        assert r.json()["revision"]["revision"] == 2
        assert r.json()["content_metrics"]["utf16_units"] == 3

        r = await c.post(f"{BASE}/artifacts/{aid}/patches", headers=headers(), json=body)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "revision_conflict"

        r = await c.get(f"{BASE}/artifacts/{aid}/revisions", headers=headers())
        assert [item["revision"] for item in r.json()["items"]] == [2, 1]

        r = await c.get(f"{BASE}/artifacts/{aid}/revisions/1", headers=headers())
        assert r.json()["content"] == "가나다"

        r = await c.get(f"{BASE}/artifacts/{aid}/diff", headers=headers(), params={"from_revision": 1, "to_revision": 2})
        assert r.status_code == 200
        assert r.json()["stats"]["added_lines"] >= 1

        r = await c.post(f"{BASE}/artifacts/{aid}/revisions/1/restore", headers=headers(), json={})
        assert r.status_code == 200
        assert r.json()["revision"]["revision"] == 3
        r = await c.get(f"{BASE}/artifacts/{aid}", headers=headers())
        assert r.json()["content"] == "가나다"

        r = await c.get(f"{BASE}/artifacts/{aid}/revisions/99", headers=headers())
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "revision_not_found"
