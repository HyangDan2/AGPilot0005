import pytest
from app.runtime.context import ContextManager, is_context_overflow_error
from app.runtime.graph import model_node
from app.runtime.state import AgentState
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall

def test_context_budget_and_pruning():
    cm = ContextManager(budget_tokens=100)
    
    # Create messages within budget
    msgs = [
        GatewayMessage(role="system", content="시스템 지침"),
        GatewayMessage(role="user", content="안녕하세요"),
    ]
    pruned, trimmed = cm.prune_and_compress(msgs)
    assert not trimmed
    assert len(pruned) == 2

    # Add a huge tool message exceeding 100 tokens
    huge_msgs = [
        GatewayMessage(role="system", content="시스템 지침"),
        GatewayMessage(role="user", content="문서 요약해줘"),
        GatewayMessage(role="tool", content="A" * 500, tool_call_id="c1", name="doc"),
        GatewayMessage(role="user", content="후속 질문"),
    ]
    pruned2, trimmed2 = cm.prune_and_compress(huge_msgs)
    assert trimmed2 is True
    # Verify that the tool message was compressed
    assert any("[이전 도구 실행 결과 요약됨]" in (m.content or "") for m in pruned2)

def test_context_cache():
    cm = ContextManager()
    cm.cache_result("system.echo", {"message": "hello"}, "cached_hello")
    res = cm.get_cached_result("system.echo", {"message": "hello"})
    assert res == "cached_hello"
    assert cm.get_cached_result("system.echo", {"message": "other"}) is None


def test_dynamic_allocation_elastic_borrowing_and_ema_calibration():
    cm = ContextManager(budget_tokens=1000)
    msgs = [GatewayMessage(role="system", content="sys"), GatewayMessage(role="user", content="hi")]

    alloc_no_ev = cm.allocate_budget(msgs, budget_tokens=2000, has_evidence=False, has_history_summary=False)
    alloc_with_ev = cm.allocate_budget(msgs, budget_tokens=2000, has_evidence=True, has_history_summary=False)
    # Unused pools roll over dynamically
    assert alloc_no_ev.evidence_budget == 0
    assert alloc_no_ev.recent_budget > alloc_with_ev.recent_budget
    assert alloc_with_ev.evidence_budget > alloc_with_ev.recent_budget

    # EMA calibration adjusts token estimation dynamically
    base_est = cm.estimate_tokens("가나다라마바사" * 10, route="hd2ai-public")
    factor = cm.record_observed_usage("hd2ai-public", estimated_tokens=100, actual_prompt_tokens=150)
    assert factor > 1.0
    cal_est = cm.estimate_tokens("가나다라마바사" * 10, route="hd2ai-public")
    assert cal_est > base_est


def test_structured_tool_stub_preserves_ids_and_atomic_turn_groups():
    cm = ContextManager(budget_tokens=180)
    tc = ToolCall(id="call_1", function=FunctionCall(name="document.read_section", arguments='{"doc_id":"d1","section_id":"sec_003"}'))
    tool_json = '{"doc_id":"d1","section_id":"sec_003","figure_ids":["fig_d_002"],"content":"' + ("가" * 900) + '"}'
    msgs = [
        GatewayMessage(role="system", content="system"),
        GatewayMessage(role="user", content="1번 질문"),
        GatewayMessage(role="assistant", content=None, tool_calls=[tc]),
        GatewayMessage(role="tool", content=tool_json, tool_call_id="call_1", name="document.read_section"),
        GatewayMessage(role="assistant", content="1번 답변 " + ("내용 " * 40)),
        GatewayMessage(role="user", content="최신 질문"),
    ]
    compacted, report = cm.compact_with_report(msgs, budget_tokens=180, route="hd2ai-admin")
    assert report.trimmed is True
    assert report.after_tokens <= 180
    # Never leave orphan tool messages without their matching assistant tool_call
    for idx, m in enumerate(compacted):
        if m.role == "tool":
            assert idx > 0 and compacted[idx - 1].role == "assistant" and compacted[idx - 1].tool_calls


def test_oversized_single_user_prompt_guarded():
    cm = ContextManager(budget_tokens=120)
    msgs = [
        GatewayMessage(role="system", content="sys"),
        GatewayMessage(role="user", content="HEAD_" + ("X" * 2000) + "_TAIL"),
    ]
    compacted, report = cm.compact_with_report(msgs, budget_tokens=120)
    assert report.trimmed is True
    assert report.stage_reached == 3
    assert "HEAD_" in (compacted[-1].content or "")
    assert "_TAIL" in (compacted[-1].content or "")
    assert report.after_tokens <= 120


@pytest.mark.asyncio
async def test_reactive_overflow_recovery_in_model_node():
    attempts = []

    class FakeClient:
        async def execute(self, messages, **kwargs):
            attempts.append(len(messages))
            if len(attempts) == 1:
                raise RuntimeError("HTTP 400: maximum context length exceeded")
            return "복구된 정상 답변", [], "stop"

    state = AgentState(
        run_id="r1",
        service_id="hd2ai-admin",
        owner_id="hd2service",
        messages=[
            GatewayMessage(role="system", content="sys"),
            GatewayMessage(role="user", content="이전 질문 " * 80),
            GatewayMessage(role="assistant", content="이전 답변 " * 80),
            GatewayMessage(role="user", content="현재 질문"),
        ],
        custom_client=FakeClient(),
    )
    updates = await model_node(state)
    assert len(attempts) == 2
    assert updates["status"] == "completed"
    assert updates["final_answer"] == "복구된 정상 답변"
    assert is_context_overflow_error(RuntimeError("context_length_exceeded")) is True
