"""#360: the nare adapter against the real nare release the performer image pins.

Every other nare adapter test feeds hand-written JSONL lines, which can only be
as right as the person who wrote them. Here the adapter launches the actual
``nare`` binary, on nare's ``fake`` provider: the model replies are scripted,
and everything above the transport (tools, the session file, the event stream,
the result line, exit codes) is nare's own code. No key and no network after
the wheel is installed.

The wheel and its hashed requirements are the ones ``agent/performer/Dockerfile.base``
installs, so a release bump that changes the wire is caught here.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest
from performer.backends.nare import CONTRACT_VERSION, NareBackend
from performer.models import Score, Stand

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "agent" / "performer" / "Dockerfile.base"
REQUIREMENTS = REPO / "agent" / "performer" / "nare-requirements.txt"


def _pinned_release() -> tuple[str, str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    version = re.search(r"^ARG NARE_VERSION=(\S+)$", text, re.MULTILINE)
    sha = re.search(r"^ARG NARE_SHA256=([0-9a-f]{64})$", text, re.MULTILINE)
    assert version and sha, "Dockerfile.base must pin NARE_VERSION and NARE_SHA256"
    return version.group(1), sha.group(1)


@pytest.fixture(scope="module")
def real_nare(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """The pinned nare, installed into a private virtualenv.

    ``NARE_BIN`` points at an existing install instead (an offline machine).
    """
    override = os.environ.get("NARE_BIN")
    if override:
        return pathlib.Path(override)
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv is required to install the pinned nare wheel; or set NARE_BIN")
    version, sha = _pinned_release()
    venv = tmp_path_factory.mktemp("nare-venv")
    subprocess.run([uv, "venv", "-q", "--python", sys.executable, str(venv)], check=True)
    python = venv / ("Scripts" if os.name == "nt" else "bin") / "python"
    url = (
        f"https://github.com/ViviDynamics/nare/releases/download/{version}/"
        f"nare-{version}-py3-none-any.whl#sha256={sha}"
    )
    install = [uv, "pip", "install", "-q", "--python", str(python)]
    subprocess.run([*install, "--require-hashes", "-r", str(REQUIREMENTS)], check=True)
    subprocess.run([*install, "--no-deps", f"nare @ {url}"], check=True)
    binary = python.parent / "nare"
    out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == version
    return binary


@pytest.fixture
def scripted(real_nare: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Put a ``nare`` first on PATH that runs the real one with a reply script."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "nare"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{real_nare}" "$@" --replay "$NARE_TEST_REPLAY"\n', encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("NARE_PROVIDER", "fake")
    monkeypatch.delenv("NARE_BASE_URL", raising=False)
    # The fake rail discovers no context window, and nare's fallback is 32000.
    monkeypatch.setenv("NARE_CONTEXT_WINDOW", "1000000")

    def _script(*replies: dict[str, Any]) -> None:
        path = tmp_path / "replies.json"
        path.write_text(json.dumps({"replies": list(replies)}), encoding="utf-8")
        monkeypatch.setenv("NARE_TEST_REPLAY", str(path))

    return _script


def _stand(path: pathlib.Path) -> Stand:
    path.mkdir(exist_ok=True)
    return Stand(path=path, branch="nare/x", created_at=datetime.datetime.now(datetime.UTC))


def _score(**overrides: Any) -> Score:
    fields: dict[str, Any] = {
        "title": "Add a greeting",
        "description": "Write hello.txt",
        "repo_url": "https://github.com/o/r.git",
        "branch": "nare/x",
        "persona_instructions": "You are the implementer persona.",
    }
    fields.update(overrides)
    return Score(**fields)


async def _run(backend: NareBackend, stand: Stand, score: Score) -> None:
    await backend.start(stand, score, model="fake-model")
    assert backend._reader_task is not None
    await backend._reader_task


def test_the_requirements_belong_to_the_pinned_release() -> None:
    version, _sha = _pinned_release()
    assert f"# nare {version} runtime dependencies" in REQUIREMENTS.read_text(encoding="utf-8")


def test_the_pinned_release_speaks_the_adapters_contract(real_nare: pathlib.Path) -> None:
    contract = json.loads(subprocess.run(
        [str(real_nare), "contract"], capture_output=True, text=True, check=True,
    ).stdout)
    assert str(contract["contract"]) == CONTRACT_VERSION


@pytest.mark.asyncio
async def test_a_working_run_edits_the_workspace_and_ends_done(scripted, tmp_path) -> None:
    scripted(
        {"tool_calls": [{"name": "write", "id": "c1",
                         "args": {"path": "hello.txt", "content": "hello\n"}}],
         "usage": {"input": 100, "output": 20, "cache_read": 30}},
        {"text": "Wrote hello.txt.", "usage": {"input": 60, "output": 8}},
    )
    stand = _stand(tmp_path / "ws")
    backend = NareBackend()
    await _run(backend, stand, _score(role="implementer"))
    status = backend.get_status()
    assert status.state == "done", status.error_reason
    assert status.output == "Wrote hello.txt."
    assert status.tokens_processed == 218
    assert (stand.path / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    events = backend.drain_events()
    calls = [e for e in events if e.type.value == "tool_use" and e.tool == "write"]
    assert [e.arg for e in calls] == ["hello.txt"]
    results = [e for e in events if e.type.value == "tool_use" and e.status]
    assert [e.status for e in results] == ["ok"]


@pytest.mark.asyncio
async def test_a_verdict_role_report_is_the_final_text(scripted, tmp_path) -> None:
    report = {"approved": True, "findings": []}
    scripted({"text": json.dumps(report), "usage": {"input": 10, "output": 5}})
    backend = NareBackend()
    await _run(backend, _stand(tmp_path / "ws"), _score(role="reviewer", pr_diff="+x\n"))
    status = backend.get_status()
    assert status.state == "done", status.error_reason
    assert json.loads(status.output or "") == report


@pytest.mark.asyncio
async def test_the_persona_reaches_nares_system_prompt(scripted, tmp_path) -> None:
    scripted({"text": "ok"})
    backend = NareBackend()
    await _run(backend, _stand(tmp_path / "ws"), _score())
    assert backend._session_path is not None
    session = json.loads(backend._session_path.read_text(encoding="utf-8"))
    assert "You are the implementer persona." in session["system"]


@pytest.mark.asyncio
async def test_an_ask_ends_blocked_with_the_question(scripted, tmp_path) -> None:
    scripted({"tool_calls": [{"name": "ask", "args": {"questions": ["Which database?"]}}]})
    backend = NareBackend()
    await _run(backend, _stand(tmp_path / "ws"), _score())
    status = backend.get_status()
    assert status.state == "blocked"
    assert status.questions == ["Which database?"]


@pytest.mark.asyncio
async def test_a_transport_failure_ends_error(scripted, tmp_path) -> None:
    scripted({"error": "provider unreachable"})
    backend = NareBackend()
    await _run(backend, _stand(tmp_path / "ws"), _score())
    status = backend.get_status()
    assert status.state == "error"
    assert "provider unreachable" in (status.error_reason or "")


@pytest.mark.asyncio
async def test_a_large_review_diff_runs(scripted, tmp_path) -> None:
    """Past Linux's 128 KiB per-argument limit, which an argv prompt would hit."""
    scripted({"text": "{}"})
    backend = NareBackend()
    await _run(backend, _stand(tmp_path / "ws"), _score(role="reviewer", pr_diff="+" + "x" * 300_000))
    assert backend.get_status().state == "done", backend.get_status().error_reason


@pytest.mark.asyncio
async def test_an_env_bootstrap_run_writes_into_its_cache(scripted, tmp_path) -> None:
    """--add-root lets the write tool reach the cache beside the checkout."""
    cache_parent = tmp_path / "devenv"
    cache_parent.mkdir()
    target = cache_parent / "demo-1" / "bin" / "tool"
    scripted(
        {"tool_calls": [{"name": "write", "id": "c1", "args": {"path": str(target), "content": "#!/bin/sh\n"}}]},
        {"text": "installed"},
    )
    backend = NareBackend()
    await _run(backend, _stand(tmp_path / "ws"), _score(role="env_bootstrap", env_cache_path=str(cache_parent / "demo-1")))
    assert backend.get_status().state == "done", backend.get_status().error_reason
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\n"
