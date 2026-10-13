"""Spec 129: hermes backend transcript records, driven through the real start/reader."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer import transcript
from performer.backends.hermes import HermesBackend
from performer.models import Score, Stand

GH_TOKEN = "ghp_" + "a" * 36
API_KEY = "sk-test-12345"
LONG_REPLY = "reply line that keeps going. " * 120  # well over the 200-char summary


@pytest.fixture
def buf():
    buffer = transcript.TranscriptBuffer("run-hermes", max_bytes=10_000_000)
    transcript.set_active_buffer(buffer)
    yield buffer
    transcript.set_active_buffer(None)


@pytest.fixture
def hermes_env(monkeypatch):
    monkeypatch.setenv("HERMES_PROVIDER", "openai")
    monkeypatch.setenv("HERMES_API_KEY", API_KEY)
    monkeypatch.setenv("HERMES_MODEL", "gpt-4o")
    monkeypatch.delenv("HERMES_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)


def _score(**kwargs) -> Score:
    defaults = dict(
        title="Transcript task",
        description="A short description.",
        acceptance_criteria=["Criterion A"],
        repo_url="https://github.com/org/repo",
        branch="feature/x",
        github_token="tok",
        persona_instructions="",
        role="implementer",
    )
    defaults.update(kwargs)
    return Score(**defaults)


def _proc(stdout: bytes = b"", stderr: bytes = b"", rc: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=rc)
    return proc


def _types(records: list[dict]) -> list[str]:
    return [r["type"] for r in records]


async def _run(tmp_path: Path, score: Score, proc: MagicMock) -> HermesBackend:
    adapter = HermesBackend()
    with patch(
        "performer.backends.hermes.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        await adapter.start(Stand(path=tmp_path, branch="feature/x"), score)
    assert adapter._reader_task is not None
    await adapter._reader_task
    return adapter


async def test_success_records_full_reply_and_single_capability(
    tmp_path: Path, hermes_env, buf,
) -> None:
    stdout = (LONG_REPLY + "\n\nsession: abc123 · 4 turns").encode()
    await _run(tmp_path, _score(), _proc(stdout=stdout))
    records = buf.after(0, 1000)

    types = _types(records)
    assert types[0] == "capability"
    assert types.count("capability") == 1
    assert types == ["capability", "system", "user", "assistant_text", "result"]

    assistant = records[3]
    assert assistant["text"] == stdout.decode().strip()
    assert len(assistant["text"]) > 200
    result = records[4]
    assert result["output"] == stdout.decode().strip()
    assert result["is_error"] is False


async def test_capability_declares_unavailable_types_honestly(
    tmp_path: Path, hermes_env, buf,
) -> None:
    await _run(tmp_path, _score(), _proc(stdout=b"done"))
    [cap] = [r for r in buf.after(0, 1000) if r["type"] == "capability"]
    unavailable = cap["input"]["unavailable"]
    provided = cap["input"]["provided"]
    assert set(unavailable) == {"tool_call", "tool_result", "thinking", "usage"}
    assert not set(provided) & set(unavailable)
    assert "--quiet" in cap["text"]


async def test_user_record_carries_full_prompt_and_offloaded_pointer(
    tmp_path: Path, hermes_env, buf,
) -> None:
    big_description = "requirement detail. " * 6000  # > 100 KB prompt, forces TASK.md offload
    await _run(tmp_path, _score(description=big_description), _proc(stdout=b"ok"))
    records = buf.after(0, 1000)
    [user] = [r for r in records if r["type"] == "user"]
    assert big_description in user["text"]
    assert len(user["text"]) > 100_000
    sent = user["input"]["sent_message"]
    assert "TASK.md" in sent
    assert len(sent) < 1000
    assert (tmp_path / "TASK.md").read_text(encoding="utf-8") == user["text"]


async def test_inline_prompt_has_no_sent_message_duplicate(
    tmp_path: Path, hermes_env, buf,
) -> None:
    await _run(tmp_path, _score(), _proc(stdout=b"ok"))
    [user] = [r for r in buf.after(0, 1000) if r["type"] == "user"]
    assert "input" not in user
    assert "Criterion A" in user["text"]


async def test_nonzero_exit_records_full_stderr_and_failed_result(
    tmp_path: Path, hermes_env, buf,
) -> None:
    stderr = ("traceback line. " * 100).encode()
    await _run(tmp_path, _score(), _proc(stdout=b"partial", stderr=stderr, rc=1))
    records = buf.after(0, 1000)

    [err] = [r for r in records if r["type"] == "error"]
    assert err["text"].endswith(stderr.decode().strip())
    assert err["text"].startswith("subprocess_exit:1")
    [result] = [r for r in records if r["type"] == "result"]
    assert result["is_error"] is True
    assert result["output"] == "partial"


async def test_malformed_json_role_records_error_with_full_stdout(
    tmp_path: Path, hermes_env, buf,
) -> None:
    prose = "I reviewed the diff and I think it is fine. " * 10
    await _run(tmp_path, _score(role="qa"), _proc(stdout=prose.encode(), rc=0))
    records = buf.after(0, 1000)

    [err] = [r for r in records if r["type"] == "error"]
    assert err["text"].startswith("malformed_output")
    assert prose.strip() in err["text"]
    [result] = [r for r in records if r["type"] == "result"]
    assert result["is_error"] is True
    assert result["output"] == prose


async def test_secret_in_reply_is_redacted_in_every_record(
    tmp_path: Path, hermes_env, buf,
) -> None:
    stdout = f"leaked token {GH_TOKEN} in the reply".encode()
    await _run(tmp_path, _score(), _proc(stdout=stdout))
    dumped = json.dumps(buf.after(0, 1000))
    assert GH_TOKEN not in dumped
    assert API_KEY not in dumped
    [assistant] = [r for r in buf.after(0, 1000) if r["type"] == "assistant_text"]
    assert "[REDACTED]" in assistant["text"]


async def test_launch_configuration_has_no_api_key(
    tmp_path: Path, hermes_env, buf,
) -> None:
    await _run(tmp_path, _score(), _proc(stdout=b"ok"))
    [system] = [r for r in buf.after(0, 1000) if r["type"] == "system"]
    assert system["input"]["provider"] == "openai"
    assert system["input"]["model"] == "gpt-4o"
    assert API_KEY not in json.dumps(system)


async def test_each_launch_records_its_prompt_but_capability_once(
    tmp_path: Path, hermes_env, buf,
) -> None:
    adapter = HermesBackend()
    stand = Stand(path=tmp_path, branch="feature/x")
    for stdout in (b"first attempt", b"second attempt"):
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_proc(stdout=stdout)),
        ):
            await adapter.start(stand, _score())
        assert adapter._reader_task is not None
        await adapter._reader_task
    types = _types(buf.after(0, 1000))
    assert types.count("capability") == 1
    assert types.count("user") == 2
    assert types.count("result") == 2


async def test_stop_while_running_records_error(
    tmp_path: Path, hermes_env, buf,
) -> None:
    release = asyncio.Event()

    async def _block() -> tuple[bytes, bytes]:
        await release.wait()
        return b"", b""

    proc = _proc(rc=0)
    proc.communicate = AsyncMock(side_effect=_block)
    adapter = HermesBackend()
    with patch(
        "performer.backends.hermes.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        await adapter.start(Stand(path=tmp_path, branch="feature/x"), _score())
    await adapter.stop()

    errors = [r for r in buf.after(0, 1000) if r["type"] == "error"]
    assert any("stopped by the performer" in (e["text"] or "") for e in errors)
    assert "result" not in _types(buf.after(0, 1000))


async def test_spawn_failure_records_error(
    tmp_path: Path, hermes_env, buf,
) -> None:
    adapter = HermesBackend()
    with patch(
        "performer.backends.hermes.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=OSError("exec failed")),
    ):
        with pytest.raises(OSError):
            await adapter.start(Stand(path=tmp_path, branch="feature/x"), _score())
    [err] = [r for r in buf.after(0, 1000) if r["type"] == "error"]
    assert "subprocess_spawn_failed" in err["text"]
    assert "exec failed" in err["text"]


async def test_no_buffer_active_is_a_noop(tmp_path: Path, hermes_env) -> None:
    transcript.set_active_buffer(None)
    await _run(tmp_path, _score(), _proc(stdout=b"ok"))
    assert transcript.transcript_enabled() is False
