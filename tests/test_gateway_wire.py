from app.gateway.wire import ToolNameCodec
from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall, ToolDefinition, ToolFunctionDefinition


def test_gateway_tool_names_round_trip():
    tool = ToolDefinition(type="function", function=ToolFunctionDefinition(
        name="document.get_outline", description="Outline", parameters={"type": "object"},
    ))
    codec = ToolNameCodec([tool])
    assert codec.tools([tool])[0]["function"]["name"] == "document_get_outline"
    previous = ToolCall(id="call-1", function=FunctionCall(name="document.get_outline", arguments='{"doc_id":"doc_1"}'))
    messages = [GatewayMessage(role="assistant", content=None, tool_calls=[previous])]
    assert codec.messages(messages)[0]["tool_calls"][0]["function"]["name"] == "document_get_outline"
    assert previous.function.name == "document.get_outline"
    returned = ToolCall(id="call-2", function=FunctionCall(name="document_get_outline", arguments="{}"))
    assert codec.decode_calls([returned])[0].function.name == "document.get_outline"
