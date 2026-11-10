import pytest
from app.tools.builtin.system import echo, get_datetime
from app.tools.builtin.math import calculate
from app.tools.builtin.document import get_outline, read_section, search, read_chunk, get_figure_info
from app.tools.dispatcher import dispatcher
from app.schemas.gateway import ToolCall, FunctionCall

@pytest.mark.asyncio
async def test_echo_tool():
    res = await echo(message="Hello HD2Lab")
    assert res == {"message": "Hello HD2Lab"}

@pytest.mark.asyncio
async def test_datetime_tool():
    res = await get_datetime(timezone="Asia/Seoul")
    assert "date" in res
    assert "time" in res
    assert res["timezone"] == "Asia/Seoul"

@pytest.mark.asyncio
async def test_math_safe_calculation():
    # Basic arithmetic
    res = await calculate("25 * 40 + 12")
    assert res["result"] == 1012
    assert res["formatted"] == "1,012"

    # Math function
    res2 = await calculate("sqrt(144) + 10")
    assert res2["result"] == 22.0

@pytest.mark.asyncio
async def test_math_evil_code_blocked():
    # Disallow __import__, eval, exec, os
    res = await calculate("__import__('os').system('ls')")
    assert res.get("error") is True
    assert "error" in res

@pytest.mark.asyncio
async def test_document_tools():
    # 1. Outline
    outline = await get_outline(doc_id="hd2lab-guide")
    assert outline["doc_id"] == "hd2lab-guide"
    assert len(outline["outline"]) >= 3

    # 2. Read Section
    section = await read_section(doc_id="hd2lab-guide", section_id="sec-3")
    assert "AGCore" in section["content"]

    # 3. Search Chunks
    search_res = await search(doc_id="hd2lab-guide", query="Gateway", limit=3)
    assert search_res["doc_id"] == "hd2lab-guide"
    assert search_res["total_matches"] >= 1
    assert any("Gateway" in r["snippet"] for r in search_res["results"])

    # 4. Read Chunk
    chunk_res = await read_chunk(doc_id="hd2lab-guide", chunk_id="chk-2-1")
    assert chunk_res["chunk_id"] == "chk-2-1"
    assert chunk_res["token_count"] > 0
    assert "Gateway" in chunk_res["content"]

    # 5. Figure Info
    fig_res = await get_figure_info(doc_id="hd2lab-guide", figure_id="fig-agent-loop")
    assert fig_res["figure_id"] == "fig-agent-loop"
    assert "LangGraph" in fig_res["caption"]
    assert "asset_url" in fig_res

@pytest.mark.asyncio
async def test_dispatcher_execution():
    call = ToolCall(
        id="call_01",
        function=FunctionCall(name="system.echo", arguments='{"message": "Test Dispatcher"}'),
    )
    res = await dispatcher.execute_tool_call(call, step=1)
    assert res.is_error is False
    assert res.name == "system.echo"
    assert "Test Dispatcher" in res.content
    assert res.trace.status == "completed"

    # Test document.search via dispatcher
    call_doc = ToolCall(
        id="call_doc_01",
        function=FunctionCall(name="document.search", arguments='{"doc_id": "hd2lab-guide", "query": "LangGraph"}'),
    )
    res_doc = await dispatcher.execute_tool_call(call_doc, step=1)
    assert res_doc.is_error is False
    assert res_doc.name == "document.search"
    assert "AGCore" in res_doc.content
    assert res_doc.trace.status == "completed"
