import pytest
from app.runtime.context import ContextManager
from app.schemas.gateway import GatewayMessage

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
