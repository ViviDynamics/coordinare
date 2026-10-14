"""148: every event text is redacted before it is clipped, and `tool` is redacted like text.

A token that straddles the 200 character cut must not leave its prefix behind,
on any path that builds a backend event, and a secret in a tool name is treated
like one in any other free text field.
"""
from __future__ import annotations

from performer.backends.claude_code import ClaudeCodeBackend
from performer.backends.codex import CodexBackend
from performer.models import BackendEvent, BackendEventType

_TOKEN = "ghp_" + "AbCdE1234567890fGhIjKlMnOpQrStUvWx"
# 190 plain characters, a space, then the token: the 200 character cut lands inside it.
_STRADDLING = "x" * 190 + " " + _TOKEN


def _assert_no_prefix(text: str) -> None:
    assert "ghp_" not in text
    assert "AbCdE" not in text


def test_claude_assistant_text_is_redacted_before_it_is_clipped() -> None:
    adapter = ClaudeCodeBackend()
    adapter._handle_event({"type": "assistant", "message": {"content": [{"type": "text", "text": _STRADDLING}]}})
    (event,) = adapter.drain_events()
    _assert_no_prefix(event.text)


def test_claude_error_text_is_redacted_before_it_is_clipped() -> None:
    adapter = ClaudeCodeBackend()
    adapter._handle_event({"type": "system", "subtype": "api_retry", "error": _STRADDLING, "attempt": 1})
    events = adapter.drain_events()
    assert events, "the retry must surface as an error event"
    for event in events:
        _assert_no_prefix(event.text + event.detail)


def test_codex_error_text_is_redacted_before_it_is_clipped() -> None:
    adapter = CodexBackend()
    adapter._handle_notification("error", {"error": {"message": _STRADDLING}, "willRetry": True})
    (event,) = adapter.drain_events()
    _assert_no_prefix(event.text)


def test_a_secret_in_a_tool_name_is_redacted() -> None:
    event = BackendEvent(type=BackendEventType.tool_use, text="Bash", tool="GITHUB_TOKEN=abc123short")
    assert "abc123short" not in event.tool
