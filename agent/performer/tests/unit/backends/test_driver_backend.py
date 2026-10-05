"""#521 — DriverBackend lifecycle: C1 command shape, §3.5 proxy seam, §4 resume."""
from __future__ import annotations

import asyncio
import datetime
import os
import tempfile
from typing import Any

import pytest

from performer.backends.driver import DriverBackend
from performer.models import Score, Stand


def _score() -> Score:
    return Score(
        title="Fix the bug",
        description="It is broken",
        repo_url="https://github.com/o/r.git",
        branch="driver/x",
        persona_instructions="Be precise.",
    )


def _stand(tmp_path) -> Stand:
    return Stand(
        path=tmp_path, branch="driver/x",
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


@pytest.mark.asyncio
async def test_start_assembles_the_c1_command(tmp_path, monkeypatch) -> None:
    """C1/C2/C3: --yes --contract 1 --jsonl, session under a writable state
    dir, persona via --system, workspace confinement via --root, prompt last."""
    recorded = _capture_exec(monkeypatch)
    backend = DriverBackend()
    await backend.start(_stand(tmp_path), _score(), model="glm-5.3-flash", temperature=0.2, max_tokens=900)
    cmd = recorded[0]
    assert cmd[:5] == ["driver", "run", "--yes", "--contract", "1"]
    assert "--jsonl" in cmd
    session_idx = cmd.index("--session")
    assert cmd[session_idx + 1].startswith(tempfile.gettempdir())
    assert cmd[cmd.index("--system") + 1] == "Be precise."
    assert cmd[cmd.index("--root") + 1] == str(tmp_path)
    assert cmd[cmd.index("--model") + 1] == "glm-5.3-flash"
    assert cmd[cmd.index("--temperature") + 1] == "0.2"
    assert cmd[cmd.index("--max-tokens") + 1] == "900"
    assert cmd[-1].startswith("# Task: Fix the bug")


@pytest.mark.asyncio
async def test_proxy_base_url_selects_openai_provider(tmp_path, monkeypatch) -> None:
    """§3.5: DRIVER_BASE_URL (set by the dual-model proxy seam) → --provider
    openai --base-url, keeping driver proxy-eligible for 080 orchestration."""
    recorded = _capture_exec(monkeypatch)
    monkeypatch.setenv("DRIVER_BASE_URL", "http://127.0.0.1:9000/v1")
    backend = DriverBackend()
    await backend.start(_stand(tmp_path), _score())
    cmd = recorded[0]
    assert cmd[cmd.index("--provider") + 1] == "openai"
    assert cmd[cmd.index("--base-url") + 1] == "http://127.0.0.1:9000/v1"


@pytest.mark.asyncio
async def test_no_base_url_leaves_provider_unset(tmp_path, monkeypatch) -> None:
    """§3.5: driver must also work strategy:single with no proxy — no provider
    flag, no base-url flag."""
    recorded = _capture_exec(monkeypatch)
    backend = DriverBackend()
    await backend.start(_stand(tmp_path), _score())
    cmd = recorded[0]
    assert "--provider" not in cmd
    assert "--base-url" not in cmd


@pytest.mark.asyncio
async def test_relay_feedback_with_live_session_resumes(tmp_path, monkeypatch) -> None:
    """§4 A2: a live session is SIGTERM-resumed with the feedback as the prompt."""
    recorded = _capture_exec(monkeypatch)

    def _no_pgid(pid: int) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(os, "getpgid", _no_pgid)
    backend = DriverBackend()
    await backend.start(_stand(tmp_path), _score())
    assert backend._proc is not None
    await backend.relay_feedback("the answer is X")
    resume_cmd = recorded[-1]
    assert resume_cmd[:2] == ["driver", "run"]
    assert "--resume" in resume_cmd
    assert resume_cmd[-1] == "the answer is X"
    assert backend.get_status().state == "working"


@pytest.mark.asyncio
async def test_relay_feedback_without_live_session_is_buffered(monkeypatch) -> None:
    """§4 A2: no live session → the feedback rides the next dispatch."""
    recorded = _capture_exec(monkeypatch)
    backend = DriverBackend()
    await backend.relay_feedback("no session running")
    assert recorded == []
    assert backend.get_status().state == "working"
