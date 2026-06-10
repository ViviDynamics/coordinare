"""084 US1 — Anthropic ``/v1/messages`` → OpenAI ``/v1/chat/completions`` request
translation (T005).

TDD: written BEFORE ``proxy/translate/request.py`` exists and must FAIL first.
Covers the FR-001 top-level field map and message-turn mapping from
``contracts/request-translation.md``:

* ``model`` passthrough
* ``system`` (string + content-block array) → a leading ``system`` message
* user/assistant message turns (string + text-block content)
* ``max_tokens`` (required) / ``temperature`` / ``top_p`` / ``stream`` passthrough
* ``stop_sequences`` → ``stop`` (rename)
* ``metadata`` and ``thinking`` dropped — asserted explicitly, never silent

``tool_use`` / ``tool_result`` / ``tools`` / ``tool_choice`` translation is US2;
this file covers only the top-level + text-turn surface.

The translator is pure (no network, no disk, no body logging — FR-011), so it is
exercised directly against dict bodies with no shim or live upstream.
"""

from __future__ import annotations

from performer.proxy.translate.request import translate_request


def _base(**overrides) -> dict:
    """A minimal valid Anthropic request body (``model`` + ``max_tokens`` +
    one user turn), with optional field overrides."""
    body = {
        "model": "gpt-oss:120b",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": "Summarize the README."}],
    }
    body.update(overrides)
    return body


# --- top-level field map ---------------------------------------------------- #


def test_model_passes_through():
    out = translate_request(_base())
    assert out["model"] == "gpt-oss:120b"


def test_max_tokens_passes_through():
    out = translate_request(_base(max_tokens=1024))
    assert out["max_tokens"] == 1024


def test_temperature_and_top_p_pass_through_when_present():
    out = translate_request(_base(temperature=0.2, top_p=0.9))
    assert out["temperature"] == 0.2
    assert out["top_p"] == 0.9


def test_temperature_and_top_p_omitted_when_absent():
    out = translate_request(_base())
    assert "temperature" not in out
    assert "top_p" not in out


def test_stop_sequences_renamed_to_stop():
    out = translate_request(_base(stop_sequences=["\n\n", "END"]))
    assert out["stop"] == ["\n\n", "END"]
    assert "stop_sequences" not in out


def test_stream_flag_passes_through():
    assert translate_request(_base(stream=True))["stream"] is True
    assert translate_request(_base(stream=False))["stream"] is False


def test_metadata_is_dropped_not_silently_carried():
    out = translate_request(_base(metadata={"user_id": "abc"}))
    assert "metadata" not in out


def test_thinking_is_dropped_not_silently_carried():
    out = translate_request(_base(thinking={"type": "enabled", "budget_tokens": 1024}))
    assert "thinking" not in out


# --- system prompt mapping -------------------------------------------------- #


def test_system_string_becomes_leading_system_message():
    out = translate_request(_base(system="You are a careful reviewer."))
    assert out["messages"][0] == {
        "role": "system",
        "content": "You are a careful reviewer.",
    }
    # the original user turn is preserved after the system message
    assert out["messages"][1]["role"] == "user"


def test_system_block_array_text_joined_into_leading_system_message():
    out = translate_request(
        _base(
            system=[
                {"type": "text", "text": "You are a reviewer."},
                {"type": "text", "text": "Be terse."},
            ]
        )
    )
    assert out["messages"][0] == {
        "role": "system",
        "content": "You are a reviewer.\nBe terse.",
    }


def test_no_system_message_when_system_absent():
    out = translate_request(_base())
    assert all(m["role"] != "system" for m in out["messages"])


# --- message-turn mapping --------------------------------------------------- #


def test_user_string_content_passes_through():
    out = translate_request(_base())
    user = [m for m in out["messages"] if m["role"] == "user"][0]
    assert user == {"role": "user", "content": "Summarize the README."}


def test_user_text_blocks_joined():
    out = translate_request(
        _base(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "First line."},
                        {"type": "text", "text": "Second line."},
                    ],
                }
            ]
        )
    )
    user = [m for m in out["messages"] if m["role"] == "user"][0]
    assert user["content"] == "First line.\nSecond line."


def test_assistant_text_turn_maps_to_assistant_message():
    out = translate_request(
        _base(
            messages=[
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello! How can I help?"},
            ]
        )
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"][0]
    assert assistant == {"role": "assistant", "content": "Hello! How can I help?"}


def test_assistant_text_blocks_joined():
    out = translate_request(
        _base(
            messages=[
                {"role": "user", "content": "Hi"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Part one."},
                        {"type": "text", "text": "Part two."},
                    ],
                },
            ]
        )
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"][0]
    assert assistant["content"] == "Part one.\nPart two."


def test_message_order_preserved():
    out = translate_request(
        _base(
            system="sys",
            messages=[
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ],
        )
    )
    assert [m["role"] for m in out["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert out["messages"][1]["content"] == "one"
    assert out["messages"][3]["content"] == "three"


def test_translation_does_not_mutate_input():
    body = _base(system="sys", metadata={"x": 1})
    before = {
        "model": body["model"],
        "n_messages": len(body["messages"]),
        "has_metadata": "metadata" in body,
    }
    translate_request(body)
    assert body["model"] == before["model"]
    assert len(body["messages"]) == before["n_messages"]
    assert ("metadata" in body) == before["has_metadata"]
