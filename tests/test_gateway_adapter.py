import pytest
from app.gateway.streaming import ToolCallAccumulator, SSEStreamParser

def test_tool_call_accumulator():
    acc = ToolCallAccumulator()
    
    # Delta 1: initial function name
    acc.feed_delta([
        {
            "index": 0,
            "id": "call_123",
            "type": "function",
            "function": {"name": "math.", "arguments": ""},
        }
    ])
    # Delta 2: complete function name and partial argument
    acc.feed_delta([
        {
            "index": 0,
            "function": {"name": "calculate", "arguments": '{"expres'},
        }
    ])
    # Delta 3: complete argument
    acc.feed_delta([
        {
            "index": 0,
            "function": {"arguments": 'sion": "25 * 4"}'},
        }
    ])
    
    calls = acc.get_tool_calls()
    assert len(calls) == 1
    call = calls[0]
    assert call.id == "call_123"
    assert call.function.name == "math.calculate"
    assert call.function.arguments == '{"expression": "25 * 4"}'

def test_sse_stream_parser():
    line1 = "data: {\"choices\": [{\"delta\": {\"content\": \"안녕\"}}]}"
    parsed1 = SSEStreamParser.parse_line(line1)
    assert parsed1 is not None
    assert parsed1[0] == "data"
    assert parsed1[1]["choices"][0]["delta"]["content"] == "안녕"

    line2 = "data: [DONE]"
    parsed2 = SSEStreamParser.parse_line(line2)
    assert parsed2 == ("[DONE]", None)
