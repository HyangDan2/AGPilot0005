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


@pytest.mark.asyncio
async def test_direct_client_wire_tool_name_translation(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from app.gateway.direct import DirectLLMClient, _to_wire_name
    from app.schemas.gateway import GatewayMessage, ToolDefinition, ToolFunctionDefinition

    assert _to_wire_name("system.echo") == "system_echo"
    assert _to_wire_name("document.get_outline") == "document_get_outline"

    client = DirectLLMClient(base_url="https://api.example.com/v1", api_key="test-key", model="test-model")
    tools = [
        ToolDefinition(
            type="function",
            function=ToolFunctionDefinition(
                name="system.echo",
                description="Echo",
                parameters={"type": "object", "properties": {}},
            ),
        )
    ]

    captured_body = {}

    async def fake_post(url, json, headers):
        captured_body.update(json)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "system_echo", "arguments": '{"message": "hi"}'},
                            }
                        ],
                    },
                }
            ]
        }
        return resp

    mock_http = AsyncMock()
    mock_http.__aenter__.return_value = mock_http
    mock_http.post = fake_post
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: mock_http)

    content, tool_calls, finish_reason = await client.execute(
        messages=[GatewayMessage(role="user", content="echo hi")],
        tools=tools,
        stream=False,
    )

    assert captured_body["tools"][0]["function"]["name"] == "system_echo"
    assert len(tool_calls) == 1
    assert tool_calls[0].function.name == "system.echo"
    assert finish_reason == "tool_calls"
