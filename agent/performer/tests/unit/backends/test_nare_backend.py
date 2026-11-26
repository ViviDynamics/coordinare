"""#360: NareBackend command line, provider selection and feedback resume."""
from __future__ import annotations

import asyncio
import json
import datetime
import os
import pathlib
import tempfile
from typing import Any

import pytest

from performer.backends.nare import CONTRACT_VERSION, NareBackend
from performer.models import Score, Stand


def _score(**overrides: Any) -> Score:
    fields: dict[str, Any] = {
        "title": "Fix the bug",
        "description": "It is broken",
        "repo_url": "https://github.com/o/r.git",
        "branch": "nare/x",
        "persona_instructions": "Be precise.",
    }
    fields.update(overrides)
    return Score(**fields)


def _stand(tmp_path) -> Stand:
    return Stand(
        path=tmp_path, branch="nare/x",
        created_at=datetime.datetime.now(datetime.UTC),
    )


class _FakeProc:
    def __init__(self, *, live: bool = True) -> None:
        self.returncode = None if live else 0
        self.stdout = None
        self.pid = 1_234_567

    async def wait(self) -> int:
        return 0


def _capture_exec(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    recorded: list[list[str]] = []

    async def _fake_exec(*cmd: str, **kwargs: Any) -> _FakeProc:
        recorded.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return recorded


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def _file(cmd: list[str], name: str) -> str:
    return pathlib.Path(_flag(cmd, name)).read_text(encoding="utf-8")


def test_the_adapter_speaks_contract_3() -> None:
    assert CONTRACT_VERSION == "3"


@pytest.mark.asyncio
async def test_start_runs_nare_with_the_contract_and_files(tmp_path, monkeypatch) -> None:
    """The binary is nare; the task and persona go by file, never argv."""
    recorded = _capture_exec(monkeypatch)
    backend = NareBackend()
    await backend.start(_stand(tmp_path), _score(), model="claude-sonnet-5", max_tokens=900)
    cmd = recorded[0]
    assert cmd[:5] == ["nare", "run", "--yes", "--contract", "3"]
    assert "--jsonl" in cmd
    assert _flag(cmd, "--session").startswith(tempfile.gettempdir())
    assert _file(cmd, "--prompt-file").startswith("# Task: Fix the bug")
    assert _file(cmd, "--append-system-file") == "Be precise."
    assert "--append-system" not in cmd
    assert "--system" not in cmd
    assert _flag(cmd, "--root") == str(tmp_path)
    assert _flag(cmd, "--model") == "claude-sonnet-5"
    assert _flag(cmd, "--max-tokens") == "900"
    assert all("Fix the bug" not in arg for arg in cmd)


@pytest.mark.asyncio
async def test_a_large_prompt_never_reaches_argv(tmp_path, monkeypatch) -> None:
    """Linux caps one argument at 128 KiB; a big PR diff must ride the file."""
    recorded = _capture_exec(monkeypatch)
    diff = "+" + "x" * 300_000
    await NareBackend().start(_stand(tmp_path), _score(pr_diff=diff, role="reviewer"))
    cmd = recorded[0]
    assert max(len(arg) for arg in cmd) < 4096
    assert diff in _file(cmd, "--prompt-file")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["assessor", "reviewer", "security", "qa", "tech_writer", "closer"])
async def test_verdict_roles_end_with_the_json_only_instruction(tmp_path, monkeypatch, role) -> None:
    recorded = _capture_exec(monkeypatch)
    await NareBackend().start(_stand(tmp_path), _score(role=role))
    prompt = _file(recorded[0], "--prompt-file")
    assert "Return ONLY a valid JSON object for your role contract." in prompt
    assert "Commit your changes" not in prompt


@pytest.mark.asyncio
async def test_the_implementer_is_told_to_commit(tmp_path, monkeypatch) -> None:
    recorded = _capture_exec(monkeypatch)
    await NareBackend().start(_stand(tmp_path), _score(role="implementer"))
    prompt = _file(recorded[0], "--prompt-file")
    assert "Commit your changes" in prompt
    assert "Return ONLY a valid JSON object" not in prompt


@pytest.fixture(autouse=True)
def _no_ambient_rail(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NARE_PROVIDER", "NARE_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_no_endpoint_leaves_nare_on_its_default_rail(tmp_path, monkeypatch) -> None:
    recorded = _capture_exec(monkeypatch)
    await NareBackend().start(_stand(tmp_path), _score())
    cmd = recorded[0]
    assert "--provider" not in cmd
    assert "--base-url" not in cmd


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["anthropic", "openai"])
async def test_a_native_endpoint_picks_its_rail(tmp_path, monkeypatch, kind) -> None:
    recorded = _capture_exec(monkeypatch)
    await NareBackend().start(_stand(tmp_path), _score(endpoint_kind=kind))
    assert _flag(recorded[0], "--provider") == kind
    assert "--base-url" not in recorded[0]


@pytest.mark.asyncio
async def test_a_self_hosted_endpoint_rides_the_openai_rail(tmp_path, monkeypatch) -> None:
    recorded = _capture_exec(monkeypatch)
    await NareBackend().start(
        _stand(tmp_path), _score(endpoint_kind="ollama", base_url="http://localhost:11434/v1"),
    )
    cmd = recorded[0]
    assert _flag(cmd, "--provider") == "openai"
    assert _flag(cmd, "--base-url") == "http://localhost:11434/v1"


@pytest.mark.asyncio
async def test_a_keyless_gateway_gets_a_placeholder_key(tmp_path, monkeypatch) -> None:
    """nare's openai rail refuses to start with no OPENAI_API_KEY; Ollama needs none."""
    seen: list[dict[str, str]] = []

    async def _fake_exec(*cmd: str, **kwargs: Any) -> _FakeProc:
        seen.append(dict(kwargs["env"]))
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    await NareBackend().start(
        _stand(tmp_path), _score(endpoint_kind="ollama", base_url="http://localhost:11434/v1"),
    )
    assert seen[0]["OPENAI_API_KEY"]


@pytest.mark.asyncio
async def test_the_proxy_base_url_wins_over_a_native_endpoint(tmp_path, monkeypatch) -> None:
    """The dual-model proxy points NARE_BASE_URL at its OpenAI-compatible loopback;
    the anthropic SDK against that base would post to /v1/v1/messages."""
    recorded = _capture_exec(monkeypatch)
    monkeypatch.setenv("NARE_BASE_URL", "http://127.0.0.1:9000/v1")
    await NareBackend().start(_stand(tmp_path), _score(endpoint_kind="anthropic"))
    cmd = recorded[0]
    assert _flag(cmd, "--provider") == "openai"
    assert _flag(cmd, "--base-url") == "http://127.0.0.1:9000/v1"


@pytest.mark.asyncio
async def test_an_operator_provider_applies_when_no_endpoint_decides(tmp_path, monkeypatch) -> None:
    recorded = _capture_exec(monkeypatch)
    monkeypatch.setenv("NARE_PROVIDER", "bedrock")
    await NareBackend().start(_stand(tmp_path), _score())
    assert _flag(recorded[0], "--provider") == "bedrock"


@pytest.mark.asyncio
async def test_temperature_is_sent_only_on_the_openai_rail(tmp_path, monkeypatch) -> None:
    """The anthropic and bedrock rails refuse --temperature at start (exit 2)."""
    recorded = _capture_exec(monkeypatch)
    await NareBackend().start(_stand(tmp_path), _score(), temperature=0.2)
    assert "--temperature" not in recorded[0]
    await NareBackend().start(_stand(tmp_path), _score(endpoint_kind="openai"), temperature=0.2)
    assert _flag(recorded[1], "--temperature") == "0.2"


@pytest.mark.asyncio
async def test_max_tool_calls_is_enforced_by_nare(tmp_path, monkeypatch) -> None:
    recorded = _capture_exec(monkeypatch)
    await NareBackend().start(_stand(tmp_path), _score(max_tool_calls=40))
    assert _flag(recorded[0], "--max-tool-calls") == "40"


@pytest.mark.asyncio
async def test_relay_feedback_with_live_session_resumes_with_the_same_flags(tmp_path, monkeypatch) -> None:
    """A nare session does not record the model or provider, so a resume repeats them."""
    recorded = _capture_exec(monkeypatch)

    def _no_pgid(pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(os, "getpgid", _no_pgid)
    backend = NareBackend()
    await backend.start(
        _stand(tmp_path), _score(endpoint_kind="litellm", base_url="http://gw/v1"), model="local/qwen",
    )
    session = _flag(recorded[0], "--session")
    await backend.relay_feedback("the answer is X")
    resume_cmd = recorded[-1]
    assert resume_cmd[:2] == ["nare", "run"]
    assert _flag(resume_cmd, "--resume") == session
    assert "--session" not in resume_cmd
    assert _file(resume_cmd, "--prompt-file") == "the answer is X"
    assert _flag(resume_cmd, "--model") == "local/qwen"
    assert _flag(resume_cmd, "--provider") == "openai"
    assert _flag(resume_cmd, "--root") == str(tmp_path)
    assert "--append-system-file" not in resume_cmd
    assert backend.get_status().state == "working"


@pytest.mark.asyncio
async def test_relay_feedback_cancels_the_old_reader(tmp_path, monkeypatch) -> None:
    """The reader of the stopped run must not survive into the resumed one."""
    _capture_exec(monkeypatch)
    monkeypatch.setattr(os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
    backend = NareBackend()
    await backend.start(_stand(tmp_path), _score())
    gate = asyncio.Event()

    async def _blocked_reader() -> None:
        await gate.wait()

    old_reader = asyncio.create_task(_blocked_reader())
    backend._reader_task = old_reader
    await backend.relay_feedback("more")
    assert old_reader.cancelled()


@pytest.mark.asyncio
async def test_relay_feedback_without_live_session_is_buffered(monkeypatch) -> None:
    recorded = _capture_exec(monkeypatch)
    backend = NareBackend()
    await backend.relay_feedback("no session running")
    assert recorded == []
    assert backend.get_status().state == "working"


@pytest.mark.asyncio
async def test_a_resume_passes_only_the_tool_calls_left(tmp_path, monkeypatch) -> None:
    """Each nare process counts --max-tool-calls from zero, so a resume gets the rest."""
    recorded = _capture_exec(monkeypatch)
    monkeypatch.setattr(os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
    backend = NareBackend()
    await backend.start(_stand(tmp_path), _score(max_tool_calls=5))
    for i in range(3):
        backend._handle_line(json.dumps({"type": "tool_use", "text": "read", "tool_use_id": f"c{i}", "detail": {}}))
    await backend.relay_feedback("more")
    assert _flag(recorded[-1], "--max-tool-calls") == "2"
    for i in range(2):
        backend._handle_line(json.dumps({"type": "tool_use", "text": "read", "tool_use_id": f"d{i}", "detail": {}}))
    backend._proc = _FakeProc()
    await backend.relay_feedback("again")
    assert _flag(recorded[-1], "--tools") == "none"
    assert "--max-tool-calls" not in recorded[-1]


@pytest.mark.asyncio
async def test_tokens_keep_counting_across_a_resume(tmp_path, monkeypatch) -> None:
    _capture_exec(monkeypatch)
    monkeypatch.setattr(os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
    backend = NareBackend()
    await backend.start(_stand(tmp_path), _score())
    backend._handle_line(json.dumps({"type": "cost", "text": "", "detail": {"total": 40_000}}))
    await backend.relay_feedback("more")
    backend._handle_line(json.dumps({"type": "result", "status": "done", "text": "ok",
                                     "usage": {"total": 5_000}}))
    assert backend.get_status().tokens_processed == 45_000


@pytest.mark.asyncio
async def test_start_and_stop_remove_the_run_directory(tmp_path, monkeypatch) -> None:
    """Persona, prompts (whole PR diffs) and the session file must not pile up in /tmp."""
    _capture_exec(monkeypatch)
    backend = NareBackend()
    await backend.start(_stand(tmp_path), _score())
    first = backend._run_dir
    assert first is not None and first.exists()
    await backend.start(_stand(tmp_path), _score())
    assert not first.exists()
    second = backend._run_dir
    backend._proc = None
    await backend.stop()
    assert second is not None and not second.exists()
