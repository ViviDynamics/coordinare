"""Spec 077 T011/T012 — PiBackend unit tests.

Covers the JSON event-stream parser (terminal contract) and the OpenAI-compatible
provider routing (PI_PROVIDER_* → LiteLLM, not Pi-hosted models). The parser is
tested deterministically against the documented pi.dev `--mode json` schema; the
subprocess is mocked (no real `pi` CLI needed).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from performer.backends.base import BackendAdapter
from performer.backends.pi import PiBackend, _extract_final_assistant_text
from performer.models import Score, Stand


# ---------------------------------------------------------------------------
# T011 — protocol + JSON event parser / terminal contract
# ---------------------------------------------------------------------------


def test_pi_satisfies_backend_adapter_protocol() -> None:
    assert isinstance(PiBackend(), BackendAdapter)


def test_text_delta_accumulates_and_reports_progress() -> None:
    b = PiBackend()
    b._handle_event({"type": "message_update",
                     "assistantMessageEvent": {"type": "text_delta", "delta": "Hello "}})
    b._handle_event({"type": "message_update",
                     "assistantMessageEvent": {"type": "text_delta", "delta": "world"}})
    assert b._status.state == "working"
    assert b._status.progress  # last delta surfaced as progress
    assert "".join(b._output_accumulator) == "Hello world"
    events = b.drain_events()
    assert all(e.is_delta for e in events)
    assert events[0].stream_id == events[1].stream_id
    assert "".join(e.text for e in events) == "Hello world"


def test_agent_end_sets_done_with_final_assistant_message() -> None:
    b = PiBackend()
    b._handle_event({
        "type": "agent_end",
        "messages": [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "PLAN COMPLETE"},
        ],
    })
    assert b._saw_terminal is True
    assert b._status.state == "done"
    assert b._status.output == "PLAN COMPLETE"


def test_agent_end_falls_back_to_accumulated_text() -> None:
    b = PiBackend()
    b._handle_event({"type": "message_update",
                     "assistantMessageEvent": {"type": "text_delta", "delta": "streamed answer"}})
    b._handle_event({"type": "agent_end", "messages": []})
    assert b._status.state == "done"
    assert b._status.output == "streamed answer"


def test_tool_execution_start_emits_tool_use_event() -> None:
    b = PiBackend()
    b._handle_event({"type": "tool_execution_start", "toolName": "bash"})
    events = b.drain_events()
    assert any(e.text == "bash" for e in events)


def test_error_event_sets_error_state_when_terminal() -> None:
    b = PiBackend()
    b._handle_event({"type": "error", "error": {"message": "boom"}, "willRetry": False})
    assert b._saw_terminal is True
    assert b._status.state == "error"
    assert b._status.error_reason == "boom"


def test_retryable_error_does_not_terminate() -> None:
    b = PiBackend()
    b._handle_event({"type": "error", "error": {"message": "rate limited"}, "willRetry": True})
    assert b._saw_terminal is False
    assert b._status.state == "working"


@pytest.mark.parametrize(
    "messages,expected",
    [
        ([{"role": "assistant", "content": "str form"}], "str form"),
        ([{"role": "assistant", "content": [{"type": "text", "text": "list form"}]}], "list form"),
        ([{"role": "user", "content": "only user"}], ""),
        ("not-a-list", ""),
    ],
)
def test_extract_final_assistant_text(messages, expected) -> None:
    assert _extract_final_assistant_text(messages) == expected


# ---------------------------------------------------------------------------
# T012 — PI_PROVIDER_* routing to LiteLLM (not Pi-hosted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_override_routes_to_litellm(tmp_path: Path, monkeypatch) -> None:
    """With PI_PROVIDER_BASE_URL set, start() writes an OpenAI-compatible provider
    config pointing at the proxy and launches pi with --provider/--model — never a
    Pi-hosted model (contracts C-2)."""
    monkeypatch.setenv("PI_PROVIDER_BASE_URL", "https://litellm.example/v1")
    monkeypatch.setenv("PI_PROVIDER_NAME", "litellm")
    monkeypatch.setenv("PI_PROVIDER_ENV_KEY", "LITELLM_MASTER_KEY")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "secret-key")
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate ~/.pi/agent/models.json

    captured: dict = {}

    class _FakeProc:
        returncode = 0
        pid = 4321
        stdout = None

        async def wait(self):
            return 0

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    # No-op reader so start() doesn't try to parse a real stream.
    async def _noop_reader(self):
        return None

    import performer.backends.pi as pi_mod
    monkeypatch.setattr(pi_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(PiBackend, "_read_loop", _noop_reader)

    stand = Stand(path=tmp_path, branch="main")
    score = Score(title="Close it", repo_url="https://github.com/x/y", branch="main", github_token="t")

    b = PiBackend()
    await b.start(stand, score, model="local/qwen3.6:35b")

    argv = captured["argv"]
    assert argv[0] == "pi" and "-p" in argv and "--mode" in argv and "json" in argv
    assert "--provider" in argv and "litellm" in argv
    assert "--model" in argv and "local/qwen3.6:35b" in argv

    # Provider config written to Pi's default registry path, pointing at the
    # proxy with an env-interpolated key + the compat block qwen needs.
    cfg = json.loads((tmp_path / ".pi" / "agent" / "models.json").read_text())
    prov = cfg["providers"]["litellm"]
    assert prov["baseUrl"] == "https://litellm.example/v1"
    assert prov["api"] == "openai-completions"
    assert prov["apiKey"] == "${LITELLM_MASTER_KEY}"  # env interpolation, not a literal/Pi-hosted key
    assert prov["compat"] == {"supportsDeveloperRole": False, "supportsReasoningEffort": False}
    assert any(m["id"] == "local/qwen3.6:35b" for m in prov["models"])


@pytest.mark.asyncio
async def test_cli_env_appends_cache_path_after_image_path(tmp_path: Path, monkeypatch) -> None:
    """088 B1: shared env policy — the pi CLI must launch under the IMAGE's
    node (22), not the env-cache's project-pinned node (e.g. 18.12.1).

    pi-tui uses the `v` (unicodeSets) regex flag, which crashes on Node < 20
    with `SyntaxError: Invalid regular expression flags` → pi exits 1 → the
    card is blocked. The CLI must launch on the IMAGE's node, but the cache
    toolchain dirs must stay REACHABLE: image PATH first, cache dirs appended
    deduplicated. Every OTHER cache var must survive.
    """
    captured: dict = {}

    class _FakeProc:
        returncode = 0
        pid = 4321
        stdout = None

        async def wait(self):
            return 0

    async def _fake_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    async def _noop_reader(self):
        return None

    import performer.backends.pi as pi_mod
    monkeypatch.setattr(pi_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(PiBackend, "_read_loop", _noop_reader)

    stand = Stand(path=tmp_path, branch="main")
    stand.cache_env = {
        "PATH": "/devenv/website-3ab3e0/node-v18.12.1/bin:/usr/bin:/bin",
        "RBENV_ROOT": "/devenv/website-3ab3e0/rbenv",
    }
    score = Score(title="Close it", repo_url="https://github.com/x/y", branch="main", github_token="t")

    await PiBackend().start(stand, score)

    env = captured["env"]
    image_path = os.environ["PATH"]
    # Image dirs FIRST — the CLI's interpreter resolves to the image's node.
    assert env["PATH"].startswith(image_path)
    # Cache toolchain dirs APPENDED — reachable, never shadowing the image.
    assert env["PATH"].index(image_path) < env["PATH"].index("node-v18.12.1")
    assert env.get("RBENV_ROOT") == "/devenv/website-3ab3e0/rbenv"
