"""Tests for AI Gateway <-> AGCore contract-synced token budgets."""
import json
import time
import pytest

from app.core import runtime_config as rc
from app.runtime.context import ContextManager
from app.runtime.document_analysis import AnalysisStore, DocumentAnalysisManager
from app.runtime.public_grounding import public_grounded_messages
from app.runtime.state import AgentState
from app.runtime.tasks.composition import AnswerComposer, CompositionVerifier, GROUNDING, VERIFY
from app.runtime.tasks.domain import BudgetLedger, Draft, Plan, Section, Task
from app.runtime.tasks.ports import JsonTaskRepository
from app.runtime.tasks.services import AnswerVerifier, AnswerWriter, ModelInvoker, RequestPlanner
from app.schemas.gateway import GatewayMessage, ModelReply


def test_contract_sync_preserves_custom_when_unchanged_and_resets_when_changed(tmp_path, monkeypatch):
    cfg_path = tmp_path / "runtime_config.json"
    monkeypatch.setattr(rc, "CONFIG_PATH_ENV", str(cfg_path))
    monkeypatch.setattr(rc, "_ACTIVE_CONFIG", rc.AgentRuntimeConfig())

    # 1. Initial sync from Gateway contract
    entry = rc.sync_route_contract("hd2ai-admin", {
        "context_tokens": 131072,
        "max_output_tokens": 32768,
        "reasoning_effort": "on",
        "revision": 5,
        "version": 2,
    })
    assert entry.is_customized is False
    assert entry.active.output_ceiling_tokens == 32768
    assert entry.active.reasoning_start_tokens == 16384
    assert entry.active.input_budget_tokens == 131072 - 32768 - 2048

    # 2. Customize AGCore budget while Gateway contract stays identical
    custom = rc.customize_route_budget("hd2ai-admin", {"write_max_tokens": 6000, "compose_max_tokens": 12000})
    assert custom.is_customized is True
    assert custom.active.write_max_tokens == 6000
    assert custom.active.compose_max_tokens == 12000

    # 3. Re-sync with SAME Gateway contract -> custom overrides must be preserved
    same = rc.sync_route_contract("hd2ai-admin", {
        "context_tokens": 131072,
        "max_output_tokens": 32768,
        "reasoning_effort": "on",
        "revision": 6,
        "version": 2,
    })
    assert same.is_customized is True
    assert same.active.write_max_tokens == 6000
    assert same.active.compose_max_tokens == 12000

    # 4. Re-sync with CHANGED Gateway contract -> reset to new recommended defaults
    changed = rc.sync_route_contract("hd2ai-admin", {
        "context_tokens": 32768,
        "max_output_tokens": 16384,
        "reasoning_effort": "on",
        "revision": 7,
        "version": 3,
    })
    assert changed.is_customized is False
    assert changed.active == changed.recommended
    assert changed.active.output_ceiling_tokens == 16384
    assert changed.active.reasoning_start_tokens == 8192


@pytest.mark.asyncio
async def test_document_analysis_note_and_reduce_scale_with_contract(tmp_path, monkeypatch):
    cfg_path = tmp_path / "runtime_config.json"
    monkeypatch.setattr(rc, "CONFIG_PATH_ENV", str(cfg_path))
    monkeypatch.setattr(rc, "_ACTIVE_CONFIG", rc.AgentRuntimeConfig())
    rc.sync_route_contract("hd2ai-beta", {
        "context_tokens": 65536,
        "max_output_tokens": 16384,
        "reasoning_effort": "on",
        "revision": 1,
        "version": 1,
    })

    runner = DocumentAnalysisManager(AnalysisStore(tmp_path / "analysis"))
    job = {
        "job_id": "ana_" + "d" * 32,
        "service_id": "hd2ai-beta",
        "question": "분석",
        "model_calls": 0,
        "reasoning_effort": "off",
        "notes": [],
    }

    limits = await runner._budget_limits(job)
    # Contract input_budget=47104 -> batch_prompt_bytes=47104, note_max_bytes scales far above 3500
    assert limits["max_prompt_bytes"] == 47104
    assert limits["note_max_bytes"] > 3500
    assert limits["reduce_group_bytes"] > 7000

    # A 4500-byte Korean note (previously rejected by hardcoded 3500 bytes) now passes without retry
    korean_4500b = ("가" * 1500)  # 4500 UTF-8 bytes
    calls = []

    class FakeGateway:
        route = "hd2ai-beta"

        async def execute(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return korean_4500b, [], "stop"
            # On oversized second test, verify compression receives the oversized note, not the raw input
            return "압축된 노트", [], "stop"

    import app.runtime.document_analysis as da_mod
    monkeypatch.setattr(da_mod, "gateway_client", FakeGateway())

    res1 = await runner._note(job, "병합 지침", "원본 긴 입력")
    assert res1 == korean_4500b
    assert len(calls) == 1

    # When a note exceeds even the dynamic note_max_bytes, retry compresses the oversized note itself
    oversized = "나" * ((limits["note_max_bytes"] // 3) + 200)
    calls.clear()

    class OversizedFirstGateway:
        route = "hd2ai-beta"

        async def execute(self, **kwargs):
            calls.append(kwargs["messages"][0].content)
            if len(calls) == 1:
                return oversized, [], "stop"
            return "압축 완료 [doc/chk_1]", [], "stop"

    monkeypatch.setattr(da_mod, "gateway_client", OversizedFirstGateway())
    res2 = await runner._note(job, "병합 지침", "원본 입력 텍스트")
    assert res2 == "압축 완료 [doc/chk_1]"
    assert len(calls) == 2
    assert oversized[:30] in calls[1]


@pytest.mark.asyncio
async def test_task_services_use_contract_budgets_for_plan_write_and_verify(tmp_path, monkeypatch):
    cfg_path = tmp_path / "runtime_config.json"
    monkeypatch.setattr(rc, "CONFIG_PATH_ENV", str(cfg_path))
    monkeypatch.setattr(rc, "_ACTIVE_CONFIG", rc.AgentRuntimeConfig())
    rc.sync_route_contract("hd2ai-admin", {
        "context_tokens": 65536,
        "max_output_tokens": 16384,
        "reasoning_effort": "on",
        "revision": 1,
        "version": 1,
    })
    rc.customize_route_budget("hd2ai-admin", {
        "plan_max_tokens": 1800,
        "write_max_tokens": 5200,
        "verify_max_tokens": 1900,
    })

    repo = JsonTaskRepository(AnalysisStore(tmp_path / "tasks"), time.time)
    now = time.time()
    task = Task(
        job_id="ana_" + "a" * 32,
        owner_id="u1",
        service_id="hd2ai-admin",
        question="요약해줘",
        document_ids=["doc"],
        created_at=now,
        updated_at=now,
        budget=BudgetLedger(deadline=now + 120),
    )
    repo.save(task)

    seen_max_tokens = []

    class RecordingModel:
        route = "hd2ai-admin"

        async def complete(self, **kwargs):
            seen_max_tokens.append(kwargs["max_tokens"])
            body = json.loads(kwargs["messages"][0].content)
            if "documents" in body:
                return ModelReply(content=json.dumps({
                    "objective": "요약",
                    "intent": "summary",
                    "scope": "all",
                    "entity_type": "general",
                    "required_outputs": ["요약"],
                    "search_terms": [],
                    "completion_criteria": ["요약"],
                    "steps": ["read", "write", "verify"],
                    "revision": 1,
                }), finish_reason="stop")
            if "sources" in body and "answer" not in body:
                return ModelReply(content=json.dumps({
                    "sections": [{"heading": "요약", "body": "내용", "refs": ["doc/chk_1"], "labels": []}]
                }), finish_reason="stop")
            return ModelReply(content=json.dumps({
                "satisfied": True, "missing_requirements": [], "unsupported_claims": []
            }), finish_reason="stop")

    invoker = ModelInvoker(RecordingModel(), repo, time.time)
    plan = await RequestPlanner(invoker).plan(task, [])
    assert isinstance(plan, Plan)
    draft = await AnswerWriter(invoker).write(task, [{"key": "doc/chk_1", "content": "원문", "source": {}}])
    assert isinstance(draft, Draft)
    ver = await AnswerVerifier(invoker).verify(task, draft, [{"key": "doc/chk_1", "content": "원문", "source": {}}])
    assert ver.satisfied is True
    assert seen_max_tokens == [1800, 5200, 1900]


def test_context_manager_and_public_grounding_accept_dynamic_budgets():
    cm = ContextManager(budget_tokens=100)
    msgs = [
        GatewayMessage(role="system", content="sys"),
        GatewayMessage(role="user", content="a" * 200),
        GatewayMessage(role="assistant", content="b" * 200),
        GatewayMessage(role="user", content="c" * 200),
    ]
    _, trimmed_default = cm.prune_and_compress(msgs)
    _, trimmed_dynamic = cm.prune_and_compress(msgs, budget_tokens=4000)
    assert trimmed_default is True
    assert trimmed_dynamic is False

    state = AgentState(
        run_id="run-1",
        service_id="hd2ai-public",
        messages=[
            GatewayMessage(role="tool", name="document.read_chunk", content="x" * 10000),
            GatewayMessage(role="tool", name="document.read_chunk", content="y" * 10000),
            GatewayMessage(role="user", content="질문"),
        ],
    )
    compact = public_grounded_messages(state, max_body_chars=12000)
    expanded = public_grounded_messages(state, max_body_chars=24000)
    assert "제외했습니다" in compact[0].content
    assert "제외했습니다" not in expanded[0].content


@pytest.mark.asyncio
async def test_lookup_uses_single_final_verify_and_capped_reasoning_and_grounding_allows_absence(tmp_path):
    assert "부재나 한계를 명시한 문장은" in GROUNDING
    assert "부재나 한계를 명시한 문장은" in VERIFY

    repo = JsonTaskRepository(AnalysisStore(tmp_path / "lookup"), time.time)
    now = time.time()
    task = Task(
        job_id="ana_" + "b" * 32,
        owner_id="u1",
        service_id="hd2ai-admin",
        question="GAN 아티팩트 처리 방법은?",
        document_ids=["doc_1"],
        reasoning_effort="high",
        created_at=now,
        updated_at=now,
        budget=BudgetLedger(deadline=now + 120),
        plan=Plan(
            intent="lookup",
            scope="relevant",
            objective="GAN 아티팩트 처리 방법 설명",
            required_outputs=["핵심 기법"],
            completion_criteria=["근거 기반 설명"],
        ),
    )
    task.drafts = {
        "g0": Draft(sections=[Section(heading="아티팩트 처리", body="sinc 필터와 U-Net SN 판별자 사용. 아블레이션 수치는 확인 불가.", refs=["doc_1/chk_1"])]),
    }
    repo.save(task)

    efforts = []
    verify_calls = []

    class LookupModel:
        async def complete(self, **kwargs):
            body = json.loads(kwargs["messages"][0].content)
            if "answer" in body:
                verify_calls.append(body.get("verification_mode"))
                return ModelReply(content=json.dumps({"satisfied": True, "missing_requirements": [], "unsupported_claims": []}), finish_reason="stop")
            efforts.append(kwargs.get("reasoning_effort"))
            return ModelReply(
                content=json.dumps({"sections": [{"heading": "아티팩트 처리", "body": "sinc 필터와 U-Net SN 판별자 적용. 아블레이션 수치는 없음.", "refs": ["doc_1/chk_1"], "labels": []}]}),
                finish_reason="stop",
            )

    invoker = ModelInvoker(LookupModel(), repo, time.time)
    await AnswerComposer(invoker, repo).compose(task)
    assert efforts == ["low"]
    # Lookup uses single unified VERIFY call instead of bounded N+M+1 split calls
    assert verify_calls == [None]
