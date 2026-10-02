"""PrimeAgentBackend unit tests.

Covers the JSON event-stream parser (terminal contract), the OpenAI-compatible
provider routing (PRIME_AGENT_PROVIDER_* → LiteLLM, not Prime Intellect-hosted
models), and the three places Prime Agent's CLI diverges from Pi's despite the
shared event vocabulary: the ``~/.prime/agent`` config directory, the bare
env-var-name ``apiKey`` form, and the ``--thinking`` reasoning-depth flag.

The parser is tested deterministically against the documented ``--mode json``
schema; the subprocess is mocked (no real ``prime-agent`` CLI needed).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from performer.backends import UnsupportedBackendError, get_backend
from performer.backends.base import BackendAdapter
from performer.backends.prime_agent import PrimeAgentBackend, _normalize_thinking
from performer.models import Score, Stand


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_prime_agent_satisfies_backend_adapter_protocol() -> None:
    assert isinstance(PrimeAgentBackend(), BackendAdapter)


def test_factory_resolves_prime_agent() -> None:
    """``prime_agent`` is the canonical key — the factory must construct it."""
    assert isinstance(get_backend("prime_agent"), PrimeAgentBackend)


def test_factory_rejects_hyphenated_alias() -> None:
    """The CLI is ``prime-agent`` but the backend key is ``prime_agent``.

    A config written from the binary's name would otherwise fail deep inside
    the container instead of at the factory.
    """
    with pytest.raises(UnsupportedBackendError):
        get_backend("prime-agent")


def test_coordinare_backend_allowlist_carries_prime_agent() -> None:
    """config_validation's allowlist must stay in sync with the factory keys,
    or a valid config is rejected pre-dispatch."""
    from coordinare.config_validation import SUPPORTED_PERFORMER_BACKENDS

    assert "prime_agent" in SUPPORTED_PERFORMER_BACKENDS


def test_every_supported_backend_has_a_provider_base_url_env() -> None:
    """A config-valid backend with no ``PROVIDER_BASE_URL_ENV`` mapping cannot be
    routed: ``_launch_for_target`` raises for a shim target, the dual-model proxy
    rejects it, and in plain single mode nothing sets its provider base URL — so
    the CLI silently falls back to vendor-hosted models.

    Asserting the whole-set invariant (not just ``prime_agent``) catches this for
    every future backend that lands in the allowlist without a launch mapping.
    """
    from coordinare.config_validation import SUPPORTED_PERFORMER_BACKENDS
    from performer.proxy.launch import PROVIDER_BASE_URL_ENV

    missing = SUPPORTED_PERFORMER_BACKENDS - PROVIDER_BASE_URL_ENV.keys()
    assert not missing, f"backends missing a provider-base-URL mapping: {sorted(missing)}"


def test_prime_agent_provider_base_url_env_name() -> None:
    """The mapping must name the exact var ``PrimeAgentBackend`` reads, or the
    provider config is never written and ``--provider`` is never passed."""
    from performer.proxy.launch import PROVIDER_BASE_URL_ENV

    assert PROVIDER_BASE_URL_ENV["prime_agent"] == "PRIME_AGENT_PROVIDER_BASE_URL"


def test_prime_config_dir_is_commit_noise() -> None:
    """``.prime`` is agent state and must never be committed into a symphony repo."""
    from performer.noise_paths import AGENT_CONFIG_DIRS, path_has_agent_config

    assert ".prime" in AGENT_CONFIG_DIRS
    assert path_has_agent_config(".prime/agent/models.json") is True
    # Segment equality, not substring — a real source path must survive.
    assert path_has_agent_config("src/prime/primes.py") is False


# ---------------------------------------------------------------------------
# JSON event parser / terminal contract
# ---------------------------------------------------------------------------


def test_text_delta_accumulates_and_reports_progress() -> None:
    b = PrimeAgentBackend()
    b._handle_event({"type": "message_update",
                     "assistantMessageEvent": {"type": "text_delta", "delta": "Hello "}})
    b._handle_event({"type": "message_update",
                     "assistantMessageEvent": {"type": "text_delta", "delta": "world"}})
    assert b._status.state == "working"
    assert b._status.progress
    assert "".join(b._output_accumulator) == "Hello world"


def test_agent_end_sets_done_with_final_assistant_message() -> None:
    b = PrimeAgentBackend()
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
    b = PrimeAgentBackend()
    b._handle_event({"type": "message_update",
                     "assistantMessageEvent": {"type": "text_delta", "delta": "streamed answer"}})
    b._handle_event({"type": "agent_end", "messages": []})
    assert b._status.state == "done"
    assert b._status.output == "streamed answer"


def test_tool_execution_start_emits_tool_use_event() -> None:
    b = PrimeAgentBackend()
    b._handle_event({"type": "tool_execution_start", "toolName": "bash"})
    assert any(e.text == "bash" for e in b.drain_events())


def test_session_header_is_ignored() -> None:
    """The stream opens with a session header; it carries no status meaning."""
    b = PrimeAgentBackend()
    b._handle_event({"type": "session", "version": 3, "id": "abc", "cwd": "/w"})
    assert b._status.state == "working"
    assert b._saw_terminal is False


def test_error_event_sets_error_state_when_terminal() -> None:
    b = PrimeAgentBackend()
    b._handle_event({"type": "error", "error": {"message": "boom"}, "willRetry": False})
    assert b._saw_terminal is True
    assert b._status.state == "error"
    assert b._status.error_reason == "boom"


def test_retryable_error_does_not_terminate() -> None:
    b = PrimeAgentBackend()
    b._handle_event({"type": "error", "error": {"message": "rate limited"}, "willRetry": True})
    assert b._saw_terminal is False
    assert b._status.state == "working"


# --- Prime-Agent-only stream events ----------------------------------------


def test_auto_retry_start_surfaces_but_does_not_terminate() -> None:
    """The CLI retries transient upstream failures itself; the run has not
    failed until those retries are exhausted."""
    b = PrimeAgentBackend()
    b._handle_event({
        "type": "auto_retry_start",
        "attempt": 1,
        "maxAttempts": 3,
        "delayMs": 1000,
        "errorMessage": "upstream 503",
    })
    assert b._saw_terminal is False
    assert b._status.state == "working"
    assert any("upstream 503" in e.text for e in b.drain_events())


def test_auto_retry_end_failure_is_terminal() -> None:
    """Without this the run would sit at 'working' until the stream closed."""
    b = PrimeAgentBackend()
    b._handle_event({
        "type": "auto_retry_end",
        "success": False,
        "attempt": 3,
        "finalError": "upstream gone",
    })
    assert b._saw_terminal is True
    assert b._status.state == "error"
    assert b._status.error_reason == "upstream gone"


def test_auto_retry_end_success_leaves_run_working() -> None:
    b = PrimeAgentBackend()
    b._handle_event({"type": "auto_retry_end", "success": True, "attempt": 2})
    assert b._saw_terminal is False
    assert b._status.state == "working"


def test_aborted_compaction_without_retry_is_terminal() -> None:
    """A compaction the CLI aborts and will not retry leaves the session unable
    to continue — report it rather than hanging."""
    b = PrimeAgentBackend()
    b._handle_event({
        "type": "compaction_end",
        "reason": "overflow",
        "aborted": True,
        "willRetry": False,
        "errorMessage": "context overflow",
    })
    assert b._saw_terminal is True
    assert b._status.state == "error"
    assert b._status.error_reason == "context overflow"


def test_agent_end_with_failed_assistant_is_not_done() -> None:
    """agent_end is forwarded BEFORE Prime Agent decides to retry a failed
    model turn, so it must not be read as completion — otherwise the performer
    finalizes (and stops) a CLI that is about to retry."""
    b = PrimeAgentBackend()
    b._handle_event({
        "type": "agent_end",
        "messages": [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "", "stopReason": "error",
             "errorMessage": "provider 500"},
        ],
    })
    assert b._saw_terminal is False
    assert b._status.state == "working"
    assert b._pending_failure == "provider 500"
    assert any("provider 500" in e.text for e in b.drain_events())


def test_agent_end_then_auto_retry_start_keeps_run_working() -> None:
    """The real upstream order: agent_end (failed assistant) → auto_retry_start."""
    b = PrimeAgentBackend()
    b._handle_event({
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": "",
                      "stopReason": "error", "errorMessage": "provider 500"}],
    })
    b._handle_event({
        "type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
        "errorMessage": "provider 500",
    })
    assert b._saw_terminal is False
    assert b._status.state == "working"


def test_retry_recovery_clears_the_held_failure() -> None:
    """A retry that lands emits auto_retry_end(success) and later a clean
    agent_end; the run is done on that, not stuck on the earlier failure."""
    b = PrimeAgentBackend()
    b._handle_event({
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": "",
                      "stopReason": "error", "errorMessage": "provider 500"}],
    })
    b._handle_event({"type": "auto_retry_end", "success": True, "attempt": 1})
    assert b._pending_failure is None
    b._handle_event({
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": "PLAN COMPLETE",
                      "stopReason": "end_turn"}],
    })
    assert b._saw_terminal is True
    assert b._status.state == "done"
    assert b._status.output == "PLAN COMPLETE"


def test_aborted_assistant_without_error_message_still_holds_a_reason() -> None:
    b = PrimeAgentBackend()
    b._handle_event({
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": "", "stopReason": "aborted",
                      "errorMessage": None}],
    })
    assert b._status.state == "working"
    assert b._pending_failure == "assistant aborted"


def test_agent_end_without_stop_reason_is_a_normal_completion() -> None:
    """Older/simpler streams omit stopReason entirely; that is not a failure."""
    b = PrimeAgentBackend()
    b._handle_event({
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": "done"}],
    })
    assert b._saw_terminal is True
    assert b._status.state == "done"


def test_successful_compaction_is_not_terminal() -> None:
    b = PrimeAgentBackend()
    b._handle_event({
        "type": "compaction_end", "reason": "threshold", "aborted": False, "willRetry": False,
    })
    assert b._saw_terminal is False
    assert b._status.state == "working"


# ---------------------------------------------------------------------------
# Stream EOF outcome
# ---------------------------------------------------------------------------


class _StreamProc:
    """A finished process whose stdout replays a fixed event stream."""

    pid = 4321

    def __init__(self, *lines: bytes, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line + b"\n")
        self.stdout.feed_eof()

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_failed_assistant_then_eof_is_error_not_done() -> None:
    """A failed assistant the CLI never retried to a good outcome ends the run
    as an error — previously _saw_terminal suppressed the exit-code fallback
    and the rc=1 run was reported done."""
    b = PrimeAgentBackend()
    b._proc = _StreamProc(
        b'{"type":"agent_end","messages":[{"role":"assistant","content":"",'
        b'"stopReason":"error","errorMessage":"provider 500"}]}',
        returncode=1,
    )
    await b._read_loop()
    assert b._status.state == "error"
    assert b._status.error_reason == "provider 500"


@pytest.mark.asyncio
async def test_failed_assistant_then_eof_is_error_even_when_rc_is_zero() -> None:
    """The held failure, not the exit code, decides the outcome."""
    b = PrimeAgentBackend()
    b._proc = _StreamProc(
        b'{"type":"agent_end","messages":[{"role":"assistant","content":"",'
        b'"stopReason":"aborted","errorMessage":"user abort"}]}',
        returncode=0,
    )
    await b._read_loop()
    assert b._status.state == "error"
    assert b._status.error_reason == "user abort"


@pytest.mark.asyncio
async def test_clean_agent_end_at_eof_is_done() -> None:
    b = PrimeAgentBackend()
    b._proc = _StreamProc(
        b'{"type":"agent_end","messages":[{"role":"assistant",'
        b'"content":"PLAN COMPLETE","stopReason":"end_turn"}]}',
        returncode=0,
    )
    await b._read_loop()
    assert b._status.state == "done"
    assert b._status.output == "PLAN COMPLETE"


# ---------------------------------------------------------------------------
# --thinking mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "effort,expected",
    [
        ("high", "high"),
        ("XHIGH", "xhigh"),   # normalized
        ("  max  ", "max"),   # trimmed
        ("off", "off"),
        ("minimal", "minimal"),
        (None, None),
        ("", None),
        ("ludicrous", None),  # unknown level dropped, not forwarded
    ],
)
def test_normalize_thinking(effort, expected) -> None:
    assert _normalize_thinking(effort) == expected


# ---------------------------------------------------------------------------
# PRIME_AGENT_PROVIDER_* routing to LiteLLM
# ---------------------------------------------------------------------------


class _FakeProc:
    returncode = 0
    pid = 4321
    stdout = None

    async def wait(self):
        return 0


async def _noop_reader(self):
    return None


def _patch_exec(monkeypatch, captured: dict) -> None:
    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    import performer.backends.prime_agent as pa_mod
    monkeypatch.setattr(pa_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(PrimeAgentBackend, "_read_loop", _noop_reader)


@pytest.mark.asyncio
async def test_provider_override_routes_to_litellm(tmp_path: Path, monkeypatch) -> None:
    """With PRIME_AGENT_PROVIDER_BASE_URL set, start() writes an OpenAI-compatible
    provider config pointing at the proxy and launches with --provider/--model —
    never a Prime Intellect-hosted model."""
    monkeypatch.setenv("PRIME_AGENT_PROVIDER_BASE_URL", "https://litellm.example/v1")
    monkeypatch.setenv("PRIME_AGENT_PROVIDER_NAME", "litellm")
    monkeypatch.setenv("PRIME_AGENT_PROVIDER_ENV_KEY", "LITELLM_MASTER_KEY")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "secret-key")
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: dict = {}
    _patch_exec(monkeypatch, captured)

    stand = Stand(path=tmp_path, branch="main")
    score = Score(title="Close it", repo_url="https://github.com/x/y", branch="main",
                  github_token="t")

    b = PrimeAgentBackend()
    await b.start(stand, score, model="local/qwen3.6:35b", effort="high")

    argv = captured["argv"]
    assert argv[0] == "prime-agent"
    assert "-p" in argv and "--mode" in argv and "json" in argv
    assert "--provider" in argv and "litellm" in argv
    assert "--model" in argv and "local/qwen3.6:35b" in argv
    assert "--thinking" in argv and "high" in argv

    # Written to Prime Agent's registry path — .prime, not Pi's .pi.
    cfg = json.loads((tmp_path / ".prime" / "agent" / "models.json").read_text())
    prov = cfg["providers"]["litellm"]
    assert prov["baseUrl"] == "https://litellm.example/v1"
    assert prov["api"] == "openai-completions"
    assert prov["compat"] == {"supportsDeveloperRole": False, "supportsReasoningEffort": False}
    assert any(m["id"] == "local/qwen3.6:35b" for m in prov["models"])


@pytest.mark.asyncio
async def test_api_key_is_bare_env_var_name_not_pi_interpolation(
    tmp_path: Path, monkeypatch
) -> None:
    """The one schema divergence from Pi.

    Pi interpolates ``${VAR}``; Prime Agent resolves a bare variable NAME at
    request time. Copying Pi's braces verbatim would make it look up a variable
    literally called ``${LITELLM_MASTER_KEY}`` and authenticate with nothing.
    """
    monkeypatch.setenv("PRIME_AGENT_PROVIDER_BASE_URL", "https://litellm.example/v1")
    monkeypatch.setenv("PRIME_AGENT_PROVIDER_ENV_KEY", "LITELLM_MASTER_KEY")
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: dict = {}
    _patch_exec(monkeypatch, captured)

    stand = Stand(path=tmp_path, branch="main")
    score = Score(title="t", repo_url="https://github.com/x/y", branch="main", github_token="t")
    await PrimeAgentBackend().start(stand, score)

    cfg = json.loads((tmp_path / ".prime" / "agent" / "models.json").read_text())
    api_key = cfg["providers"]["litellm"]["apiKey"]
    assert api_key == "LITELLM_MASTER_KEY"
    assert "${" not in api_key
    # The secret itself must never be written to disk.
    assert "secret" not in json.dumps(cfg)


@pytest.mark.asyncio
async def test_coding_agent_dir_override_is_honored(tmp_path: Path, monkeypatch) -> None:
    """PRIME_AGENT_CODING_AGENT_DIR redirects the CLI's config dir; the provider
    config has to follow it or the CLI reads a registry we never wrote."""
    custom = tmp_path / "custom-agent-dir"
    monkeypatch.setenv("PRIME_AGENT_PROVIDER_BASE_URL", "https://litellm.example/v1")
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", str(custom))
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: dict = {}
    _patch_exec(monkeypatch, captured)

    stand = Stand(path=tmp_path, branch="main")
    score = Score(title="t", repo_url="https://github.com/x/y", branch="main", github_token="t")
    await PrimeAgentBackend().start(stand, score)

    assert (custom / "models.json").exists()
    assert not (tmp_path / ".prime" / "agent" / "models.json").exists()


@pytest.mark.asyncio
async def test_no_provider_override_launches_without_provider_flag(
    tmp_path: Path, monkeypatch
) -> None:
    """Absent PRIME_AGENT_PROVIDER_BASE_URL the CLI keeps its own configured
    provider — we must not write a registry or pass --provider."""
    monkeypatch.delenv("PRIME_AGENT_PROVIDER_BASE_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: dict = {}
    _patch_exec(monkeypatch, captured)

    stand = Stand(path=tmp_path, branch="main")
    score = Score(title="t", repo_url="https://github.com/x/y", branch="main", github_token="t")
    await PrimeAgentBackend().start(stand, score)

    assert "--provider" not in captured["argv"]
    assert not (tmp_path / ".prime" / "agent" / "models.json").exists()


@pytest.mark.asyncio
async def test_unsupported_thinking_level_is_not_forwarded(
    tmp_path: Path, monkeypatch
) -> None:
    """An invalid --thinking value makes the CLI exit before doing any work, so
    a cosmetic config mismatch must not become a failed card."""
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: dict = {}
    _patch_exec(monkeypatch, captured)

    stand = Stand(path=tmp_path, branch="main")
    score = Score(title="t", repo_url="https://github.com/x/y", branch="main", github_token="t")
    await PrimeAgentBackend().start(stand, score, effort="ludicrous")

    assert "--thinking" not in captured["argv"]
    assert "ludicrous" not in captured["argv"]


@pytest.mark.asyncio
async def test_relay_feedback_preserves_provider_model_and_thinking(
    tmp_path: Path, monkeypatch
) -> None:
    """``-p`` is one-shot, so feedback is a fresh run — it must carry the same
    routing, or the follow-up silently lands on a different model."""
    monkeypatch.setenv("PRIME_AGENT_PROVIDER_BASE_URL", "https://litellm.example/v1")
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: dict = {}
    _patch_exec(monkeypatch, captured)

    stand = Stand(path=tmp_path, branch="main")
    score = Score(title="t", repo_url="https://github.com/x/y", branch="main", github_token="t")

    b = PrimeAgentBackend()
    await b.start(stand, score, model="local/qwen3.6:35b", effort="medium")
    await b.relay_feedback("address the review comments")

    argv = captured["argv"]
    assert argv[0] == "prime-agent"
    assert "--provider" in argv and "litellm" in argv
    assert "--model" in argv and "local/qwen3.6:35b" in argv
    assert "--thinking" in argv and "medium" in argv
    assert argv[-1] == "address the review comments"
    # State reset so the second run can reach its own terminal event.
    assert b._saw_terminal is False
    assert b._status.state == "working"
