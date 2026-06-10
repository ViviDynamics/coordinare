"""Spec 077 US6 — OpenClawBackend unit tests.

Covers protocol conformance, the ``openclaw agent --json`` terminal contract
(payloads[0].text / meta.stopReason), and OPENCLAW_PROVIDER_* routing to
LiteLLM (writes ~/.openclaw/openclaw.json + model allowlist, never a
vendor-hosted model). The subprocess is mocked — no real ``openclaw`` CLI
needed. The JSON shapes are taken verbatim from the live POC (R-07).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from performer.backends.base import BackendAdapter
from performer.backends.openclaw import OpenClawBackend, _extract_final_text
from performer.models import Score, Stand


def _score(role: str = "reviewer") -> Score:
    return Score(
        title="Review it",
        repo_url="https://github.com/x/y",
        branch="main",
        github_token="t",
        role=role,
    )


# ---------------------------------------------------------------------------
# protocol + final-text extraction
# ---------------------------------------------------------------------------


def test_openclaw_satisfies_backend_adapter_protocol() -> None:
    assert isinstance(OpenClawBackend(), BackendAdapter)


@pytest.mark.parametrize(
    "parsed,expected",
    [
        ({"payloads": [{"text": "pong"}], "meta": {}}, "pong"),
        ({"payloads": [], "meta": {"finalAssistantVisibleText": "from meta"}}, "from meta"),
        ({"payloads": [{"text": "  "}], "meta": {"finalAssistantRawText": "raw"}}, "raw"),
        ({"meta": {}}, ""),
        ("not-a-dict", ""),
    ],
)
def test_extract_final_text(parsed, expected) -> None:
    assert _extract_final_text(parsed) == expected


# ---------------------------------------------------------------------------
# terminal contract via _wait_and_parse (mocked subprocess)
# ---------------------------------------------------------------------------


def _fake_proc(stdout: bytes, *, rc: int = 0, stderr: bytes = b"") -> object:
    proc = AsyncMock()
    proc.returncode = rc
    proc.pid = 4242
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
async def test_stop_reason_stop_sets_done_with_output() -> None:
    payload = {
        "payloads": [{"text": "pong"}],
        "meta": {
            "stopReason": "stop",
            "aborted": False,
            "executionTrace": {"winnerProvider": "litellm", "winnerModel": "spark/qwen3.6:35b"},
        },
    }
    b = OpenClawBackend()
    b._proc = _fake_proc(json.dumps(payload).encode())
    await b._wait_and_parse()
    assert b._status.state == "done"
    assert b._status.output == "pong"
    assert b._status.stop_reason == "stop"


@pytest.mark.asyncio
async def test_aborted_sets_error() -> None:
    payload = {"payloads": [{"text": "partial"}], "meta": {"stopReason": "length", "aborted": True}}
    b = OpenClawBackend()
    b._proc = _fake_proc(json.dumps(payload).encode())
    await b._wait_and_parse()
    assert b._status.state == "error"
    assert "length" in (b._status.error_reason or "") or "aborted" in (b._status.error_reason or "")


@pytest.mark.asyncio
async def test_nonzero_exit_sets_error() -> None:
    b = OpenClawBackend()
    b._proc = _fake_proc(b"", rc=1, stderr=b"boom")
    await b._wait_and_parse()
    assert b._status.state == "error"
    assert "subprocess_exit:1" in (b._status.error_reason or "")


@pytest.mark.asyncio
async def test_malformed_output_sets_error() -> None:
    b = OpenClawBackend()
    b._proc = _fake_proc(b"not json at all")
    await b._wait_and_parse()
    assert b._status.state == "error"
    assert b._status.error_reason == "malformed_output"


# ---------------------------------------------------------------------------
# OPENCLAW_PROVIDER_* routing to LiteLLM (not vendor-hosted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_routed_to_soul_md_and_dropped_from_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    """068 FR-015 for openclaw: persona is written to <workspace>/SOUL.md (its
    native identity slot) and NOT duplicated in the prompt body; score is
    restored afterward."""
    monkeypatch.setenv("HOME", str(tmp_path))
    captured: dict = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        return _fake_proc(b'{"payloads":[{"text":"ok"}],"meta":{"stopReason":"stop"}}')

    async def _noop_reader(self):
        return None

    import performer.backends.openclaw as oc_mod
    monkeypatch.setattr(oc_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(OpenClawBackend, "_wait_and_parse", _noop_reader)

    checkout = tmp_path / "repo"
    checkout.mkdir()
    score = Score(
        title="Review it", repo_url="https://github.com/x/y", branch="main",
        github_token="t", role="reviewer",
        persona_instructions="PERSONA_SOUL_MARKER: be a strict binary reviewer.",
    )
    stand = Stand(path=checkout, branch="main")
    b = OpenClawBackend()
    await b.start(stand, score, model="spark/qwen3.6:35b")

    # persona landed in SOUL.md (openclaw's identity file)
    assert (checkout / "SOUL.md").read_text() == "PERSONA_SOUL_MARKER: be a strict binary reviewer."
    # not duplicated in the prompt body (the --message arg)
    msg = captured["argv"][captured["argv"].index("--message") + 1]
    assert "PERSONA_SOUL_MARKER" not in msg
    # score restored for downstream callers / relay replay
    assert score.persona_instructions == "PERSONA_SOUL_MARKER: be a strict binary reviewer."


@pytest.mark.asyncio
async def test_provider_override_writes_config_and_prefixes_model(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENCLAW_PROVIDER_BASE_URL", "https://litellm.example/v1")
    monkeypatch.setenv("OPENCLAW_PROVIDER_NAME", "litellm")
    monkeypatch.setenv("OPENCLAW_PROVIDER_ENV_KEY", "LITELLM_MASTER_KEY")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "secret-key")
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate ~/.openclaw

    captured: dict = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        return _fake_proc(b'{"payloads":[{"text":"ok"}],"meta":{"stopReason":"stop"}}')

    async def _noop_reader(self):
        return None

    import performer.backends.openclaw as oc_mod
    monkeypatch.setattr(oc_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(OpenClawBackend, "_wait_and_parse", _noop_reader)

    stand = Stand(path=tmp_path, branch="main")
    b = OpenClawBackend()
    await b.start(stand, _score(), model="spark/qwen3.6:35b")

    argv = captured["argv"]
    assert argv[0] == "openclaw" and "agent" in argv and "--local" in argv and "--json" in argv
    # model is provider-prefixed so OpenClaw routes to the custom provider.
    assert "--model" in argv and "litellm/spark/qwen3.6:35b" in argv

    cfg = json.loads((tmp_path / ".openclaw" / "openclaw.json").read_text())
    assert cfg["models"]["mode"] == "merge"
    prov = cfg["models"]["providers"]["litellm"]
    assert prov["baseUrl"] == "https://litellm.example/v1"
    assert prov["api"] == "openai-completions"
    assert prov["apiKey"] == "${LITELLM_MASTER_KEY}"  # env interpolation, not a literal
    assert any(m["id"] == "spark/qwen3.6:35b" for m in prov["models"])
    # model allowlisted (OpenClaw rejects non-allowlisted models)
    assert "litellm/spark/qwen3.6:35b" in cfg["agents"]["defaults"]["models"]
    # default budget preserved when no override env is set (qwen-on-spark baseline).
    model_entry = next(m for m in prov["models"] if m["id"] == "spark/qwen3.6:35b")
    assert model_entry["contextWindow"] == 32768
    assert model_entry["maxTokens"] == 8192


@pytest.mark.asyncio
async def test_context_window_and_max_tokens_overridable_via_env(
    tmp_path: Path, monkeypatch
) -> None:
    """The declared provider budget must be raisable per-model via env so a
    large-context model (e.g. gpt-oss:120b served at 131072) isn't gated by the
    hardcoded 32768 default — OpenClaw enforces ``contextWindow`` client-side and
    emits "Context overflow: prompt too large for the model" when the reviewing
    prompt (diff + accumulated feedback) exceeds it."""
    monkeypatch.setenv("OPENCLAW_PROVIDER_BASE_URL", "https://litellm.example/v1")
    monkeypatch.setenv("OPENCLAW_PROVIDER_ENV_KEY", "LITELLM_MASTER_KEY")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "secret-key")
    monkeypatch.setenv("OPENCLAW_CONTEXT_WINDOW", "131072")
    monkeypatch.setenv("OPENCLAW_MAX_TOKENS", "32768")
    monkeypatch.setenv("HOME", str(tmp_path))

    async def _fake_exec(*argv, **kwargs):
        return _fake_proc(b'{"payloads":[{"text":"ok"}],"meta":{"stopReason":"stop"}}')

    async def _noop_reader(self):
        return None

    import performer.backends.openclaw as oc_mod
    monkeypatch.setattr(oc_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(OpenClawBackend, "_wait_and_parse", _noop_reader)

    stand = Stand(path=tmp_path, branch="main")
    await OpenClawBackend().start(stand, _score(), model="gpt-oss:120b")

    cfg = json.loads((tmp_path / ".openclaw" / "openclaw.json").read_text())
    prov = cfg["models"]["providers"]["litellm"]
    model_entry = next(m for m in prov["models"] if m["id"] == "gpt-oss:120b")
    assert model_entry["contextWindow"] == 131072
    assert model_entry["maxTokens"] == 32768


@pytest.mark.asyncio
async def test_cli_env_excludes_cache_path_keeps_other_cache_vars(tmp_path, monkeypatch) -> None:
    """077: the env-cache PATH (project's pinned node, e.g. 18.12.1) must NOT be
    the interpreter the openclaw CLI launches under — openclaw needs Node >=22.19
    and exits 1 at startup under an older node. So PATH is excluded from the CLI
    launch env (CLI runs on image node 22); the agent's own bash -lc commands get
    the project node via login-shell activate.sh sourcing. Non-PATH cache vars are
    still passed through."""
    monkeypatch.setenv("HOME", str(tmp_path))
    captured: dict = {}

    async def _fake_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _fake_proc(b'{"payloads":[{"text":"ok"}],"meta":{"stopReason":"stop"}}')

    async def _noop_reader(self):
        return None

    import performer.backends.openclaw as oc_mod
    monkeypatch.setattr(oc_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(OpenClawBackend, "_wait_and_parse", _noop_reader)

    stand = Stand(path=tmp_path, branch="main")
    stand.cache_env = {
        "PATH": "/devenv/x/node-v18.12.1/bin:/usr/bin:/bin",
        "RBENV_ROOT": "/devenv/x/rbenv",
    }
    await OpenClawBackend().start(stand, _score())

    env = captured["env"]
    # The project node-18 PATH must NOT shadow the CLI's interpreter.
    assert "node-v18.12.1" not in env.get("PATH", "")
    assert env["PATH"] != stand.cache_env["PATH"]
    # ...but other env-cache vars are still passed to the CLI.
    assert env.get("RBENV_ROOT") == "/devenv/x/rbenv"


def test_point_workspace_at_symlinks_to_checkout(tmp_path: Path) -> None:
    """openclaw's --local agent only sees ~/.openclaw/workspace, not cwd — so the
    adapter must symlink it at the job checkout (else the reviewer sees an empty
    workspace)."""
    home = tmp_path / "home"
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / "README.md").write_text("hi")
    b = OpenClawBackend()
    b._point_workspace_at(home, checkout)
    ws = home / ".openclaw" / "workspace"
    assert ws.is_symlink()
    assert ws.resolve() == checkout.resolve()
    assert (ws / "README.md").read_text() == "hi"  # agent now sees the repo


def test_point_workspace_at_replaces_existing_dir(tmp_path: Path) -> None:
    """A pre-existing default workspace dir (openclaw scaffolding) is replaced."""
    home = tmp_path / "home"
    ws = home / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    (ws / "AGENTS.md").write_text("scaffold")
    checkout = tmp_path / "repo"
    checkout.mkdir()
    OpenClawBackend()._point_workspace_at(home, checkout)
    assert ws.is_symlink()
    assert ws.resolve() == checkout.resolve()


@pytest.mark.asyncio
async def test_no_provider_env_writes_no_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_PROVIDER_BASE_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    async def _fake_exec(*argv, **kwargs):
        return _fake_proc(b'{"payloads":[{"text":"ok"}],"meta":{"stopReason":"stop"}}')

    async def _noop_reader(self):
        return None

    import performer.backends.openclaw as oc_mod
    monkeypatch.setattr(oc_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(OpenClawBackend, "_wait_and_parse", _noop_reader)

    stand = Stand(path=tmp_path, branch="main")
    b = OpenClawBackend()
    await b.start(stand, _score(), model="vendor-model")

    assert not (tmp_path / ".openclaw" / "openclaw.json").exists()


@pytest.mark.asyncio
async def test_card_docs_written_to_workspace_and_referenced_in_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    """077: the card title/description/acceptance criteria are materialised as
    CARD.md in the workspace (openclaw's reviewer looks for 'card documentation
    files', not the inline prompt body) and the prompt points at it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    captured: dict = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        return _fake_proc(b'{"payloads":[{"text":"ok"}],"meta":{"stopReason":"stop"}}')

    async def _noop_reader(self):
        return None

    import performer.backends.openclaw as oc_mod
    monkeypatch.setattr(oc_mod.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(OpenClawBackend, "_wait_and_parse", _noop_reader)

    checkout = tmp_path / "repo"
    checkout.mkdir()
    score = Score(
        title="Reduce contact-form margins", repo_url="https://github.com/x/y",
        branch="main", github_token="t", role="reviewer",
        description="The card holding the contact form has excessive margins on mobile.",
        acceptance_criteria=["Margins reduced on mobile", "No regression on desktop"],
    )
    stand = Stand(path=checkout, branch="main")
    b = OpenClawBackend()
    await b.start(stand, score, model="spark/qwen3.6:35b")

    # CARD.md exists at the workspace root with the card context.
    card_md = checkout / "CARD.md"
    assert card_md.is_file()
    body = card_md.read_text()
    assert "Reduce contact-form margins" in body
    assert "excessive margins on mobile" in body
    assert "Margins reduced on mobile" in body
    assert "No regression on desktop" in body

    # The prompt points the agent at CARD.md.
    msg = captured["argv"][captured["argv"].index("--message") + 1]
    assert "CARD.md" in msg
