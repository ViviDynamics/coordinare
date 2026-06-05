"""080 — canonical LLMRequest/LLMResponse representation (T015)."""

from __future__ import annotations

from performer.proxy.llm_turn import LLMRequest, LLMResponse, Message, ToolCall, ToolSchema


def test_without_tools_hides_tools_only():
    req = LLMRequest(
        messages=(Message.user("hi"),),
        tools=(ToolSchema(name="read", parameters={"type": "object"}),),
    )
    stripped = req.without_tools()
    assert stripped.tools == ()
    assert stripped.messages == req.messages  # conversation untouched
    assert req.tools  # original unchanged (frozen)


def test_with_system_prepended_inserts_first():
    req = LLMRequest(messages=(Message.user("task"),))
    out = req.with_system_prepended("PLAN")
    assert out.messages[0].role == "system"
    assert out.messages[0].content == "PLAN"
    assert out.messages[1].role == "user"
    # original conversation order preserved after the injected system message
    assert len(out.messages) == 2


def test_last_message_and_helpers():
    req = LLMRequest(messages=(Message.system("s"), Message.user("u")))
    assert req.last_message.role == "user"
    assert LLMRequest(messages=()).last_message is None


def test_response_has_tool_calls():
    assert LLMResponse(tool_calls=(ToolCall("1", "x"),)).has_tool_calls is True
    assert LLMResponse(content="hi").has_tool_calls is False


def test_message_constructors():
    assert Message.system("a").role == "system"
    assert Message.user("b").role == "user"
    asst = Message.assistant("c", tool_calls=(ToolCall("1", "t"),))
    assert asst.role == "assistant" and asst.tool_calls[0].name == "t"


def test_tool_result_error_flag_default_false():
    assert Message(role="tool", content="ok", tool_call_id="c1").is_error_tool_result is False
