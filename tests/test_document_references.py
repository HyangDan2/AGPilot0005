"""Document references must reach the model and constrain document tools."""
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.runner import DocumentReferenceError, runner
from app.schemas.agent import AgentRunRequest
from app.schemas.gateway import FunctionCall, ToolCall
from app.tools.dispatcher import dispatcher
from app.runtime.graph import model_node, tool_node
from app.runtime.state import AgentState
from app.schemas.gateway import GatewayMessage


@pytest.mark.asyncio
async def test_completed_reference_is_in_agent_context():
    with patch("app.runtime.runner.document_adapter.get_outline", new_callable=AsyncMock) as outline:
        outline.return_value = {"doc_id": "doc_abc123", "status": "completed", "outline": []}
        session = await runner.create_run(
            AgentRunRequest(message="이 문서를 요약해줘", context_refs=["doc_abc123"]),
            owner_id="alice",
        )
    assert session.state.owner_id == "alice"
    assert session.state.context_refs == ["doc_abc123"]
    assert "doc_abc123" in session.state.messages[0].content
    assert session.state.messages[-1].content == "이 문서를 요약해줘"


@pytest.mark.asyncio
async def test_incomplete_reference_rejected_before_model_call():
    with patch("app.runtime.runner.document_adapter.get_outline", new_callable=AsyncMock) as outline:
        outline.return_value = {"doc_id": "doc_abc123", "status": "processing", "outline": []}
        with pytest.raises(DocumentReferenceError):
            await runner.create_run(AgentRunRequest(message="요약", context_refs=["doc_abc123"]))


@pytest.mark.asyncio
async def test_unreferenced_document_tool_is_denied():
    call = ToolCall(id="call-1", function=FunctionCall(name="document.get_outline", arguments='{"doc_id":"doc_other"}'))
    result = await dispatcher.execute_tool_call(call, allowed_document_ids=["doc_allowed"])
    assert result.is_error
    assert result.trace.error_code == "document_not_referenced"


@pytest.mark.asyncio
async def test_first_document_step_requires_outline_tool():
    client = AsyncMock()
    client.execute.return_value = ("done", [], "stop")
    state = AgentState(run_id="r1", service_id="hd2ai-beta", context_refs=["doc_abc123"],
                       messages=[GatewayMessage(role="user", content="문서 요약")], custom_client=client)
    await model_node(state)
    kwargs = client.execute.await_args.kwargs
    assert kwargs["tool_choice"] == "required"
    assert [tool.function.name for tool in kwargs["tools"]] == ["document.get_outline"]


@pytest.mark.asyncio
async def test_first_outline_uses_verified_reference_id():
    call = ToolCall(id="call-1", function=FunctionCall(name="document.get_outline", arguments='{"doc_id":"made_up"}'))
    state = AgentState(run_id="r1", service_id="hd2ai-beta", context_refs=["doc_abc123"],
                       messages=[GatewayMessage(role="assistant", content=None, tool_calls=[call])],
                       pending_tool_calls=[call])
    with patch("app.runtime.graph.dispatcher.execute_batch", new_callable=AsyncMock) as execute:
        execute.return_value = []
        await tool_node(state)
    sent = execute.await_args.kwargs["calls"][0]
    assert sent.function.arguments == '{"doc_id": "doc_abc123"}'


@pytest.mark.asyncio
async def test_public_route_reads_verified_document_before_local_model():
    state = AgentState(run_id="r-public", service_id="hd2ai-public", context_refs=["doc_abc123"],
                       messages=[GatewayMessage(role="user", content="문서의 표식은?")])
    first = await model_node(state)
    assert first["pending_tool_calls"][0].function.name == "document.get_outline"
    assert 'doc_abc123' in first["pending_tool_calls"][0].function.arguments

    state.tool_call_count = 1
    state.messages.append(GatewayMessage(role="tool", name="document.get_outline",
                                         content='{"doc_id":"doc_abc123","outline":[{"section_id":"s1"}]}',
                                         tool_call_id="public-outline-0"))
    section = await model_node(state)
    assert section["pending_tool_calls"][0].function.name == "document.read_section"
    assert '"section_id": "s1"' in section["pending_tool_calls"][0].function.arguments

    client = AsyncMock()
    client.execute.return_value = ("표식 답변", [], "stop")
    state.tool_call_count = 2
    state.custom_client = client
    state.messages.append(GatewayMessage(role="tool", name="document.read_section",
                                         content='{"content":"근거"}', tool_call_id="public-section-0"))
    second = await model_node(state)
    sent = client.execute.await_args.kwargs
    assert sent["tools"] is None
    assert "근거" in sent["messages"][0].content
    assert second["final_answer"] == "표식 답변"


@pytest.mark.asyncio
async def test_public_grounded_answer_retries_once_when_local_model_is_empty():
    client = AsyncMock()
    client.execute.side_effect = [(None, [], "stop"), ("근거 있는 답변", [], "stop")]
    state = AgentState(run_id="r-retry", service_id="hd2ai-public", context_refs=["doc_abc123"],
                       tool_call_count=2, custom_client=client,
                       messages=[GatewayMessage(role="user", content="질문"),
                                 GatewayMessage(role="tool", name="document.read_section",
                                                content='{"content":"근거"}', tool_call_id="section")])
    result = await model_node(state)
    assert client.execute.await_count == 2
    assert result["final_answer"] == "근거 있는 답변"


@pytest.mark.asyncio
async def test_public_search_hit_is_read_before_answer_and_uses_current_question():
    state=AgentState(run_id='r-body',service_id='hd2ai-public',context_refs=['doc_abc123'],tool_call_count=5,max_tool_calls=6,
        messages=[GatewayMessage(role='user',content='이전 질문'),GatewayMessage(role='assistant',content='이전 답변'),
                  GatewayMessage(role='user',content='현재 함수의 반환값은?'),
                  GatewayMessage(role='tool',name='document.search',tool_call_id='search',
                    content='{"doc_id":"doc_abc123","results":[{"chunk_id":"chk_6","snippet":"잘린 스니펫"}]}')])
    pending=await model_node(state)
    assert pending['pending_tool_calls'][0].function.name=='document.read_chunk'
    assert 'chk_6' in pending['pending_tool_calls'][0].function.arguments
    state.tool_call_count=6;state.messages.append(GatewayMessage(role='tool',name='document.read_chunk',tool_call_id='read',
        content='{"doc_id":"doc_abc123","chunk_id":"chk_6","content":"TRUE success, FALSE failure"}'))
    client=AsyncMock();client.execute.return_value=('성공 TRUE, 실패 FALSE',[],'stop');state.custom_client=client
    result=await model_node(state);sent=client.execute.await_args.kwargs['messages'][0].content
    assert result['final_answer']=='성공 TRUE, 실패 FALSE'
    assert '[현재 질문]\n현재 함수의 반환값은?' in sent and '이전 답변' in sent
    assert 'TRUE success, FALSE failure' in sent and '잘린 스니펫' not in sent
