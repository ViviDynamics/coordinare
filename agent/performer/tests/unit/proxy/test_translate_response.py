"""084 US1 — OpenAI ``/v1/chat/completions`` → Anthropic ``/v1/messages``
non-streaming response translation (T006).

TDD: written BEFORE ``proxy/translate/response.py`` exists and must FAIL first.
Covers the FR-002 / FR-005 non-streaming field map from
``contracts/response-translation.md``:

* ``id`` passthrough (or synthesized ``msg_…`` when absent)
* constant ``type: "message"`` / ``role: "assistant"``
* ``message.content`` string → one ``text`` content block
* ``message.tool_calls[]`` → ``tool_use`` content block(s) (FR-004): ``id`` /
  ``function.name`` → ``name`` / ``function.arguments`` (JSON string) → parsed
  ``input`` object; invalid JSON args → ``input: {}`` (documented degenerate
  case, never a silent drop)
* ``finish_reason`` → ``stop_reason`` via the shared FR-005 map
* constant ``stop_sequence: null``
* ``model`` passthrough
* ``usage.prompt_tokens`` → ``usage.input_tokens`` and
  ``usage.completion_tokens`` → ``usage.output_tokens`` rename

The translator is pure (no network, no disk, no body/token logging — FR-011),
so it is exercised directly against dict bodies with no shim or live upstream.
"""

from __future__ import annotations

from performer.proxy.translate.response import translate_response

from .fixtures import STUB_OPENAI_NONSTREAM_JSON, STUB_OPENAI_TOOLCALL_JSON

# --- non-streaming text completion (STUB_OPENAI_NONSTREAM_JSON) -------------- #


def test_type_is_constant_message():
    out = translate_response(STUB_OPENAI_NONSTREAM_JSON)
    assert out["type"] == "message"


def test_role_is_constant_assistant():
    out = translate_response(STUB_OPENAI_NONSTREAM_JSON)
    assert out["role"] == "assistant"


def test_id_passes_through():
    out = translate_response(STUB_OPENAI_NONSTREAM_JSON)
    assert out["id"] == "chatcmpl-stub-nonstream"


def test_synthesizes_msg_id_when_absent():
    body = {k: v for k, v in STUB_OPENAI_NONSTREAM_JSON.items() if k != "id"}
    out = translate_response(body)
    assert out["id"].startswith("msg_")


def test_model_passes_through():
    out = translate_response(STUB_OPENAI_NONSTREAM_JSON)
    assert out["model"] == "gpt-oss:120b"


def test_content_string_becomes_single_text_block():
    out = translate_response(STUB_OPENAI_NONSTREAM_JSON)
    assert out["content"] == [
        {"type": "text", "text": "The README documents the build steps."}
    ]


def test_stop_reason_maps_stop_to_end_turn():
    out = translate_response(STUB_OPENAI_NONSTREAM_JSON)
    assert out["stop_reason"] == "end_turn"


def test_stop_sequence_is_constant_null():
    out = translate_response(STUB_OPENAI_NONSTREAM_JSON)
    assert out["stop_sequence"] is None


def test_usage_token_fields_renamed():
    out = translate_response(STUB_OPENAI_NONSTREAM_JSON)
    assert out["usage"]["input_tokens"] == 27
    assert out["usage"]["output_tokens"] == 9
    # the OpenAI field names must not survive the rename
    assert "prompt_tokens" not in out["usage"]
    assert "completion_tokens" not in out["usage"]


# --- structured tool call (STUB_OPENAI_TOOLCALL_JSON) ----------------------- #


def test_tool_call_becomes_tool_use_block():
    out = translate_response(STUB_OPENAI_TOOLCALL_JSON)
    tool_blocks = [b for b in out["content"] if b["type"] == "tool_use"]
    assert tool_blocks == [
        {
            "type": "tool_use",
            "id": "call_stub_0",
            "name": "read_file",
            "input": {"path": "README.md"},
        }
    ]


def test_tool_call_arguments_parsed_into_input_object():
    out = translate_response(STUB_OPENAI_TOOLCALL_JSON)
    tool = [b for b in out["content"] if b["type"] == "tool_use"][0]
    # arguments arrive as a JSON *string* on the wire; output must be a dict
    assert isinstance(tool["input"], dict)
    assert tool["input"] == {"path": "README.md"}


def test_tool_call_finish_reason_maps_to_tool_use():
    out = translate_response(STUB_OPENAI_TOOLCALL_JSON)
    assert out["stop_reason"] == "tool_use"


def test_null_content_with_tool_calls_emits_no_text_block():
    """``content: None`` alongside a tool call must not produce an empty text
    block — only the ``tool_use`` block (contract: text omitted if empty and
    tool_calls present)."""
    out = translate_response(STUB_OPENAI_TOOLCALL_JSON)
    assert all(b["type"] != "text" for b in out["content"])


def test_invalid_tool_arguments_degrade_to_empty_input_not_dropped():
    """Per the documented degenerate case: unparsable ``function.arguments``
    yields ``input: {}`` (and a metadata-only record), never a silent drop of
    the tool_use block."""
    body = {
        "id": "chatcmpl-badargs",
        "model": "gpt-oss:120b",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": "{not valid json",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    out = translate_response(body)
    tool = [b for b in out["content"] if b["type"] == "tool_use"][0]
    assert tool["name"] == "read_file"
    assert tool["input"] == {}


# --- finish_reason map coverage (shared FR-005 table) ----------------------- #


def test_unknown_finish_reason_defaults_to_end_turn():
    body = {
        "id": "chatcmpl-x",
        "model": "gpt-oss:120b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "something_new",
            }
        ],
    }
    assert translate_response(body)["stop_reason"] == "end_turn"


def test_length_finish_reason_maps_to_max_tokens():
    body = {
        "id": "chatcmpl-x",
        "model": "gpt-oss:120b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "length",
            }
        ],
    }
    assert translate_response(body)["stop_reason"] == "max_tokens"


# --- purity ----------------------------------------------------------------- #


def test_translation_does_not_mutate_input():
    body = {
        "id": "chatcmpl-stub-nonstream",
        "model": "gpt-oss:120b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }
    before_usage = dict(body["usage"])
    translate_response(body)
    assert body["usage"] == before_usage
    assert "input_tokens" not in body["usage"]
    assert body["choices"][0]["message"]["content"] == "hello"
