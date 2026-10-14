"""Spec 129: openclaw backend transcript records, driven through start and the real reader."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer import transcript
from performer.backends.openclaw import OpenClawBackend
from performer.models import Score, Stand

GH_TOKEN = "ghp_" + "b" * 36
LONG_TEXT = "assistant sentence that keeps going. " * 60  # well over the 200-char summary


@pytest.fixture
def buf():
    buffer = transcript.TranscriptBuffer("run-openclaw", max_bytes=10_000_000)
    transcript.set_active_buffer(buffer)
    yield buffer
    transcript.set_active_buffer(None)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Keep openclaw's HOME-relative workspace symlink and config inside tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENCLAW_PROVIDER_BASE_URL", raising=False)
    return home


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
    proc.pid = 4343
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=rc)
    return proc


def _types(records: list[dict]) -> list[str]:
    return [r["type"] for r in records]


def _turn(payloads: list[str], **meta_overrides) -> bytes:
    meta = {
        "stopReason": "stop",
        "aborted": False,
        "executionTrace": {"winnerProvider": "litellm", "winnerModel": "local/qwen"},
        "finalAssistantVisibleText": payloads[0] if payloads else "",
    }
    meta.update(meta_overrides)
    return json.dumps({"payloads": [{"text": t} for t in payloads], "meta": meta}).encode()


async def _parse(stdout: bytes, *, rc: int = 0, stderr: bytes = b"") -> OpenClawBackend:
    backend = OpenClawBackend()
    backend._proc = _proc(stdout=stdout, stderr=stderr, rc=rc)
    await backend._wait_and_parse()
    return backend


async def test_start_records_capability_system_and_full_user_prompt(
    tmp_path: Path, isolated_home, buf,
) -> None:
    big_description = "requirement detail. " * 400
    proc = _proc(stdout=_turn(["ok"]))
    with patch(
        "performer.backends.openclaw.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        backend = OpenClawBackend()
        await backend.start(
            Stand(path=tmp_path, branch="feature/x"),
            _score(description=big_description),
        )
    assert backend._reader_task is not None
    await backend._reader_task

    records = buf.after(0, 1000)
    assert _types(records) == [
        "capability", "system", "user", "assistant_text", "result",
    ]
    [user] = [r for r in records if r["type"] == "user"]
    assert big_description in user["text"]
    assert "Criterion A" in user["text"]
    assert "input" not in user


async def test_capability_declares_unavailable_types(
    tmp_path: Path, isolated_home, buf,
) -> None:
    with patch(
        "performer.backends.openclaw.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_proc(stdout=_turn(["ok"]))),
    ):
        backend = OpenClawBackend()
        await backend.start(Stand(path=tmp_path, branch="feature/x"), _score())
    assert backend._reader_task is not None
    await backend._reader_task
    [cap] = [r for r in buf.after(0, 1000) if r["type"] == "capability"]
    assert set(cap["input"]["unavailable"]) == {"tool_call", "tool_result", "thinking", "usage"}
    assert not set(cap["input"]["provided"]) & set(cap["input"]["unavailable"])


async def test_each_payload_is_one_full_assistant_message(isolated_home, buf) -> None:
    await _parse(_turn([LONG_TEXT, "second payload"]))
    records = buf.after(0, 1000)

    messages = [r for r in records if r["type"] == "assistant_text"]
    assert [m["text"] for m in messages] == [LONG_TEXT, "second payload"]
    assert len(messages[0]["text"]) > 200


async def test_result_keeps_full_meta_and_final_output(isolated_home, buf) -> None:
    await _parse(_turn([LONG_TEXT]))
    [result] = [r for r in buf.after(0, 1000) if r["type"] == "result"]
    assert result["output"] == LONG_TEXT
    assert result["is_error"] is False
    assert result["input"]["meta"]["executionTrace"]["winnerModel"] == "local/qwen"


async def test_meta_text_used_when_payloads_empty(isolated_home, buf) -> None:
    stdout = json.dumps({
        "payloads": [],
        "meta": {"stopReason": "stop", "finalAssistantVisibleText": LONG_TEXT},
    }).encode()
    await _parse(stdout)
    messages = [r for r in buf.after(0, 1000) if r["type"] == "assistant_text"]
    assert [m["text"] for m in messages] == [LONG_TEXT]


async def test_aborted_turn_records_error_and_failed_result(isolated_home, buf) -> None:
    await _parse(_turn([LONG_TEXT], stopReason="length"))
    records = buf.after(0, 1000)
    [err] = [r for r in records if r["type"] == "error"]
    assert "length" in err["text"]
    [result] = [r for r in records if r["type"] == "result"]
    assert result["is_error"] is True
    assert result["output"] == LONG_TEXT


async def test_nonzero_exit_records_full_stderr(isolated_home, buf) -> None:
    stderr = ("context trace line. " * 80).encode()
    await _parse(b"", rc=2, stderr=stderr)
    records = buf.after(0, 1000)
    [err] = [r for r in records if r["type"] == "error"]
    assert err["text"] == stderr.decode().strip()
    [result] = [r for r in records if r["type"] == "result"]
    assert result["is_error"] is True
    assert result["input"]["reason"] == "subprocess_exit:2"


async def test_malformed_stdout_records_error_and_raw_output(isolated_home, buf) -> None:
    raw = "not an object: " + "x" * 500
    await _parse(raw.encode())
    records = buf.after(0, 1000)
    [err] = [r for r in records if r["type"] == "error"]
    assert err["text"].startswith("malformed_output")
    [result] = [r for r in records if r["type"] == "result"]
    assert result["output"] == raw


async def test_secret_in_reply_is_redacted(isolated_home, buf) -> None:
    await _parse(_turn([f"the token is {GH_TOKEN}"]))
    dumped = json.dumps(buf.after(0, 1000))
    assert GH_TOKEN not in dumped
    assert "[REDACTED]" in dumped


async def test_capability_emitted_once_across_repeated_starts(
    tmp_path: Path, isolated_home, buf,
) -> None:
    backend = OpenClawBackend()
    stand = Stand(path=tmp_path, branch="feature/x")
    for stdout in (_turn(["first"]), _turn(["second"])):
        with patch(
            "performer.backends.openclaw.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_proc(stdout=stdout)),
        ):
            await backend.start(stand, _score())
        assert backend._reader_task is not None
        await backend._reader_task
    types = _types(buf.after(0, 1000))
    assert types.count("capability") == 1
    assert types.count("user") == 2
    assert types.count("result") == 2


async def test_stop_while_running_records_error(
    tmp_path: Path, isolated_home, buf,
) -> None:
    release = asyncio.Event()

    async def _block() -> tuple[bytes, bytes]:
        await release.wait()
        return b"", b""

    proc = _proc(rc=0)
    proc.communicate = AsyncMock(side_effect=_block)
    backend = OpenClawBackend()
    with patch(
        "performer.backends.openclaw.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        await backend.start(Stand(path=tmp_path, branch="feature/x"), _score())
    await backend.stop()

    records = buf.after(0, 1000)
    errors = [r for r in records if r["type"] == "error"]
    assert any("stopped by the performer" in (e["text"] or "") for e in errors)
    assert "result" not in _types(records)
