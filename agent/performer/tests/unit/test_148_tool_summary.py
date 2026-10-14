"""148: the primary argument and outcome of a tool call, as each backend reports it.

The activity feed shows what a tool was asked to do. These tests pin the one
argument the performer keeps per tool, the cap on it, and that the rest of the
tool input (file contents, replacement text) never reaches the event.
"""
from __future__ import annotations

import pytest

from performer.backends._tool_summary import MAX_TOOL_ARG, primary_argument
from performer.backends.claude_code import ClaudeCodeBackend
from performer.backends.codex import CodexBackend
from performer.backends.pi import PiBackend
from performer.backends.prime_agent import PrimeAgentBackend
from performer.models import BackendEvent, BackendEventType


@pytest.mark.parametrize(
    ("tool", "tool_input", "expected"),
    [
        ("Bash", {"command": "uv run pytest -q"}, "uv run pytest -q"),
        ("Bash", {"command": "make\n  test"}, "make test"),
        ("Read", {"file_path": "src/foo.py", "offset": 10}, "src/foo.py"),
        ("Edit", {"file_path": "src/foo.py", "old_string": "a", "new_string": "b"}, "src/foo.py"),
        ("Write", {"file_path": "docs/x.md", "content": "secret body"}, "docs/x.md"),
        ("NotebookEdit", {"notebook_path": "nb.ipynb"}, "nb.ipynb"),
        ("Grep", {"pattern": "TODO", "path": "src"}, "TODO in src"),
        ("Grep", {"pattern": "TODO"}, "TODO"),
        ("WebFetch", {"url": "https://example.com/docs?token=abc#top"}, "https://example.com/docs"),
        ("WebFetch", {"url": "https://user:pw@example.com/a"}, "https://example.com/a"),
        ("Glob", {"pattern": "**/*.py"}, ""),
        ("Task", {"description": "investigate"}, ""),
        ("Bash", {}, ""),
        ("Read", {"file_path": "   "}, ""),
        ("Bash", "not a mapping", ""),
    ],
)
def test_primary_argument_per_tool(tool: str, tool_input: object, expected: str) -> None:
    assert primary_argument(tool, tool_input) == expected


def test_primary_argument_is_capped() -> None:
    long_command = "echo " + "x" * 500
    summary = primary_argument("Bash", {"command": long_command})
    assert len(summary) == MAX_TOOL_ARG == 160


def test_claude_tool_use_carries_the_argument_and_not_the_content() -> None:
    adapter = ClaudeCodeBackend()
    adapter._handle_event({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "name": "Write",
            "input": {"file_path": "notes.txt", "content": "api body that must not travel"},
        }]},
    })
    (event,) = adapter.drain_events()
    assert event.type is BackendEventType.tool_use
    assert event.tool == "Write"
    assert event.arg == "notes.txt"
    assert "must not travel" not in event.model_dump_json()


def test_claude_bash_tool_use_carries_the_command() -> None:
    adapter = ClaudeCodeBackend()
    adapter._handle_event({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]},
    })
    (event,) = adapter.drain_events()
    assert (event.text, event.tool, event.arg) == ("Bash", "Bash", "git status")


def test_claude_tool_result_reports_ok_or_error() -> None:
    adapter = ClaudeCodeBackend()
    adapter._handle_event({"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "boom"})
    adapter._handle_event({"type": "tool_result", "tool_use_id": "t2", "content": "fine"})
    failed, ok = adapter.drain_events()
    assert failed.status == "error" and failed.detail == "t1"
    assert ok.status == "ok" and ok.detail == "t2"


def test_codex_command_carries_command_and_exit_status() -> None:
    adapter = CodexBackend()
    adapter._handle_notification("item/completed", {"item": {
        "type": "commandExecution", "command": "uv run pytest -q", "exitCode": 0,
    }})
    adapter._handle_notification("item/completed", {"item": {
        "type": "commandExecution", "command": "uv run pytest -q", "exitCode": 2,
    }})
    passed, failed = adapter.drain_events()
    assert (passed.tool, passed.arg, passed.status) == ("shell", "uv run pytest -q", "ok")
    assert failed.status == "error"


def test_codex_file_change_carries_the_path() -> None:
    adapter = CodexBackend()
    adapter._handle_notification("item/completed", {"item": {"type": "fileChange", "path": "src/a.py"}})
    (event,) = adapter.drain_events()
    assert (event.tool, event.arg) == ("Edit", "src/a.py")


def test_pi_tool_execution_carries_its_argument() -> None:
    adapter = PiBackend()
    adapter._handle_event({"type": "tool_execution_start", "toolName": "Read",
                           "args": {"file_path": "README.md"}})
    (event,) = adapter.drain_events()
    assert (event.tool, event.arg) == ("Read", "README.md")


def test_prime_agent_tool_execution_carries_its_argument() -> None:
    adapter = PrimeAgentBackend()
    adapter._handle_event({"type": "tool_execution_start", "toolName": "Bash",
                           "args": {"command": "ls"}})
    (event,) = adapter.drain_events()
    assert (event.tool, event.arg) == ("Bash", "ls")


def test_event_argument_is_redacted_by_the_model() -> None:
    event = BackendEvent(
        type=BackendEventType.tool_use, text="Bash", tool="Bash",
        arg="curl -H 'Authorization: Bearer " + "a" * 40 + "' https://x",
    )
    assert "a" * 40 not in event.arg
    assert "[REDACTED]" in event.arg


def test_wire_dump_carries_the_new_fields_with_empty_defaults() -> None:
    dumped = BackendEvent(type=BackendEventType.progress, text="hello").model_dump()
    assert dumped["tool"] == "" and dumped["arg"] == "" and dumped["status"] == ""
