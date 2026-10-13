"""Spec 129: junie backend transcript records, driven through _launch and the real reader."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from performer import transcript
from performer.backends.junie import JunieBackend
from performer.models import Score, Stand

GH_TOKEN = "ghp_" + "c" * 36
LONG_SUMMARY = "junie summary sentence that keeps going. " * 60


@pytest.fixture
def buf():
    buffer = transcript.TranscriptBuffer("run-junie", max_bytes=10_000_000)
    transcript.set_active_buffer(buffer)
    yield buffer
    transcript.set_active_buffer(None)


@pytest.fixture(autouse=True)
def no_custom_profile(monkeypatch):
    monkeypatch.delenv("JUNIE_PROVIDER_BASE_URL", raising=False)


def _types(records: list[dict]) -> list[str]:
    return [r["type"] for r in records]


def _writing_proc(backend: JunieBackend, result: object | None, **options: Any) -> MagicMock:
    """Fake junie process. On exit it writes ``result`` to the JSON output file,
    the way junie does, so the real reader parses it.

    Options: ``stdout``, ``stderr`` (bytes), ``rc`` (int), ``block`` (asyncio.Event).
    """
    stdout: bytes = options.get("stdout", b"")
    stderr: bytes = options.get("stderr", b"")
    rc: int = options.get("rc", 0)
    block: asyncio.Event | None = options.get("block")

    async def _communicate() -> tuple[bytes, bytes]:
        if block is not None:
            await block.wait()
        path = backend._json_output_path
        if result is not None and path is not None:
            path.write_text(json.dumps(result))
        return stdout, stderr

    proc = MagicMock()
    proc.pid = 4444
    proc.returncode = rc
    proc.communicate = _communicate
    proc.wait = _noop_wait
    return proc


async def _noop_wait() -> int:
    return 0


async def _launch_and_wait(
    tmp_path: Path, prompt: str, result: object | None, **proc_kwargs,
) -> JunieBackend:
    backend = JunieBackend()
    backend._stand = Stand(path=tmp_path, branch="main")
    backend._score = Score(title="t", repo_url="https://github.com/o/r", branch="main")
    backend._original_prompt = prompt  # start() sets this; relay_feedback joins against it
    proc = _writing_proc(backend, result, **proc_kwargs)
    with patch(
        "performer.backends.junie.asyncio.create_subprocess_exec",
        new=_return(proc),
    ):
        await backend._launch(prompt)
    assert backend._reader_task is not None
    await backend._reader_task
    return backend


def _return(value):
    async def _create(*_args, **_kwargs):
        return value

    return _create


async def test_full_run_records_capability_prompt_summary_and_result(
    tmp_path: Path, buf,
) -> None:
    prompt = "Task body. " * 800
    result = {"summary": LONG_SUMMARY}
    await _launch_and_wait(tmp_path, prompt, result)
    records = buf.after(0, 1000)

    assert _types(records) == [
        "capability", "system", "user", "assistant_text", "result",
    ]
    [user] = [r for r in records if r["type"] == "user"]
    assert user["text"] == prompt
    assert len(user["text"]) > 5000
    [assistant] = [r for r in records if r["type"] == "assistant_text"]
    assert assistant["text"] == LONG_SUMMARY
    [result_rec] = [r for r in records if r["type"] == "result"]
    assert result_rec["output"] == LONG_SUMMARY
    assert result_rec["is_error"] is False
    assert result_rec["input"]["json_output"] == result


async def test_capability_declares_tool_and_thinking_unavailable(tmp_path: Path, buf) -> None:
    await _launch_and_wait(tmp_path, "t", {"summary": "ok"})
    [cap] = [r for r in buf.after(0, 1000) if r["type"] == "capability"]
    assert set(cap["input"]["unavailable"]) == {"tool_call", "tool_result", "thinking"}
    assert "usage" in cap["input"]["provided"]
    assert not set(cap["input"]["provided"]) & set(cap["input"]["unavailable"])


async def test_usage_record_only_when_file_reports_counts(tmp_path: Path, buf) -> None:
    result = {"summary": "ok", "usage": {"input_tokens": 1200, "output_tokens": 340}}
    await _launch_and_wait(tmp_path, "t", result)
    [usage] = [r for r in buf.after(0, 1000) if r["type"] == "usage"]
    assert usage["usage"]["input_tokens"] == 1200
    assert usage["usage"]["output_tokens"] == 340
    assert usage["usage"].get("cache_read") is None
    assert "run total" in usage["text"]


async def test_no_usage_record_without_reported_counts(tmp_path: Path, buf) -> None:
    await _launch_and_wait(tmp_path, "t", {"summary": "ok"})
    assert "usage" not in _types(buf.after(0, 1000))


async def test_relay_feedback_launch_puts_feedback_in_user_record(tmp_path: Path, buf) -> None:
    backend = await _launch_and_wait(tmp_path, "original task", {"summary": "first"})
    second = _writing_proc(backend, {"summary": "second"})
    with patch(
        "performer.backends.junie.asyncio.create_subprocess_exec",
        new=_return(second),
    ):
        await backend.relay_feedback("FEEDBACK_MARKER fix the naming")
    assert backend._reader_task is not None
    await backend._reader_task

    users = [r for r in buf.after(0, 1000) if r["type"] == "user"]
    assert len(users) == 2
    assert users[0]["text"] == "original task"
    assert "FEEDBACK_MARKER fix the naming" in users[1]["text"]
    assert "original task" in users[1]["text"]
    types = _types(buf.after(0, 1000))
    assert types.count("capability") == 1


async def test_error_field_records_full_error_and_failed_result(tmp_path: Path, buf) -> None:
    long_error = "model refused the request: " + "detail " * 120
    await _launch_and_wait(tmp_path, "t", {"error": long_error})
    records = buf.after(0, 1000)
    [err] = [r for r in records if r["type"] == "error"]
    assert err["text"] == long_error
    [result] = [r for r in records if r["type"] == "result"]
    assert result["is_error"] is True


async def test_nonzero_exit_without_result_file_records_full_stderr(tmp_path: Path, buf) -> None:
    stderr = ("node stack frame. " * 90).encode()
    await _launch_and_wait(tmp_path, "t", None, stderr=stderr, rc=3)
    records = buf.after(0, 1000)
    [err] = [r for r in records if r["type"] == "error"]
    assert err["text"] == stderr.decode().strip()
    [result] = [r for r in records if r["type"] == "result"]
    assert result["is_error"] is True


async def test_summary_secret_is_redacted(tmp_path: Path, buf) -> None:
    await _launch_and_wait(tmp_path, "t", {"summary": f"pushed with {GH_TOKEN}"})
    dumped = json.dumps(buf.after(0, 1000))
    assert GH_TOKEN not in dumped
    assert "[REDACTED]" in dumped


async def test_stop_while_running_records_error(tmp_path: Path, buf) -> None:
    release = asyncio.Event()
    backend = JunieBackend()
    backend._stand = Stand(path=tmp_path, branch="main")
    backend._score = Score(title="t", repo_url="https://github.com/o/r", branch="main")
    proc = _writing_proc(backend, None, block=release, rc=0)
    with patch(
        "performer.backends.junie.asyncio.create_subprocess_exec",
        new=_return(proc),
    ):
        await backend._launch("t")
    await backend.stop()

    records = buf.after(0, 1000)
    errors = [r for r in records if r["type"] == "error"]
    assert any("stopped by the performer" in (e["text"] or "") for e in errors)
    assert "result" not in _types(records)


async def test_spawn_failure_records_error(tmp_path: Path, buf) -> None:
    backend = JunieBackend()
    backend._stand = Stand(path=tmp_path, branch="main")
    backend._score = Score(title="t", repo_url="https://github.com/o/r", branch="main")

    async def _fail(*_args, **_kwargs):
        raise OSError("junie not found")

    with patch("performer.backends.junie.asyncio.create_subprocess_exec", new=_fail):
        with pytest.raises(OSError):
            await backend._launch("t")
    [err] = [r for r in buf.after(0, 1000) if r["type"] == "error"]
    assert "subprocess_spawn_failed" in err["text"]
    assert "junie not found" in err["text"]
