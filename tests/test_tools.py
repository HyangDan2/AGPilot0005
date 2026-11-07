import pytest
from app.tools.builtin.system import echo, get_datetime
from app.tools.builtin.math import calculate
from app.tools.builtin.document import get_outline, read_section
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
    outline = await get_outline(doc_id="hd2lab-guide")
    assert outline["doc_id"] == "hd2lab-guide"
    assert len(outline["outline"]) >= 3

    section = await read_section(doc_id="hd2lab-guide", section_id="sec-3")
    assert "AGCore" in section["content"]

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
