"""461: QAre and Progettare, behind a stage job's egress proxy, send their Node requests through it.

Node's ``http``, ``https`` and ``fetch`` ignore ``HTTPS_PROXY`` unless ``NODE_USE_ENV_PROXY=1``.
A stage job behind the egress proxy (#447, #449) sits on a network with no route out, so a Node
request that skips the proxy cannot connect at all. The performer image's entry point sets the
variable next to the proxy variables, for every process it starts; the stage job then builds
QAre's (and Progettare's) environment from a short list of names, which must carry it on.

These run the real entry points and the real ``python -m performer.stage_jobs qare`` entry, with a
fake QAre that records what it sees, through the #450 restart without the token.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from performer.stage_jobs import progettare
from performer.stage_jobs import qare as runner

from coordinare.services.stage_jobs import CREDENTIAL_ENV, EGRESS_PROXY_ENV, JOB_LOG, job_command
from tests.unit.qare_fixtures import calls, make_remote, request_spec, scenario
from tests.unit.stage_job_fixtures import container_env
from tests.unit.test_435_qare_sandbox import _entry_stubs, _run_entry

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "agent" / "performer" / "entrypoint.sh"
TOKEN = "ghs_" + "461canary" + "0" * 27
LOOPBACK = "localhost,127.0.0.1,::1"


@pytest.fixture
def listening_proxy() -> Iterator[str]:
    """A port that accepts connections, which is all the entry points wait for."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"


def _python_on(bindir: Path) -> None:
    """``python`` on PATH, as in the image, for ``job_command``'s ``exec python -m ...``."""
    bindir.mkdir(exist_ok=True)
    wrapper = bindir / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)


def _run_job(tmp_path: Path, *, sandbox: bool, proxy: str | None) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """A QAre stage job as its container runs it: the image's entry point, then ``job_command``."""
    web, _base, head = make_remote(tmp_path, profile_on_head=True)
    path = scenario(tmp_path)
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=web, base=head, head=head)  # one-sided: no compare diff to fetch
    env = container_env(env={
        "FAKE_QARE_SCENARIO": str(path), CREDENTIAL_ENV: TOKEN, "PERFORMER_PYTHON": sys.executable,
    })
    if proxy:
        env[EGRESS_PROXY_ENV] = proxy
    command = job_command("qare", job)
    if sandbox:
        stubs = _entry_stubs(tmp_path)  # dockerd and docker stand-ins
        _python_on(stubs["bin"])
        env.pop("PATH")  # _run_entry's, with the stubs first
        # The image runs the real entry point; here it is not executable, so a shell runs it.
        stubs["base"].write_text(f'#!/bin/sh\nexec sh "{ENTRYPOINT}" "$@"\n')
        env.update({
            "PERFORMER_BASE_ENTRYPOINT": str(stubs["base"]),
            "SANDBOX_PYTHON": sys.executable, "SANDBOX_PROXY_WAIT_SECONDS": "5",
            "SANDBOX_PROXY_HEARTBEAT_SECONDS": "0.2",  # so it ends soon after the job
        })
        result = _run_entry(stubs, env, *command)
    else:
        bindir = tmp_path / "bin"
        _python_on(bindir)
        env["PATH"] = f"{bindir}:/usr/bin:/bin"
        result = subprocess.run(
            ["sh", str(ENTRYPOINT), *command], env=env, capture_output=True, text=True, timeout=120, check=False,
        )
    return result, job, path


def _log(job: Path) -> str:
    log = job / JOB_LOG
    return log.read_text() if log.exists() else ""


def _qare_calls(path: Path) -> list[dict[str, Any]]:
    return [c for c in calls(path) if c["argv"][0] in ("plan", "run")]


@pytest.mark.parametrize("sandbox", [False, True], ids=["no-sandbox", "sandbox"])
def test_qare_behind_the_egress_proxy_runs_with_node_pointed_at_it(
    tmp_path: Path, listening_proxy: str, sandbox: bool,
) -> None:
    result, job, path = _run_job(tmp_path, sandbox=sandbox, proxy=listening_proxy)
    log = _log(job)
    assert result.returncode == 0, result.stderr + log
    assert json.loads((job / runner.STATUS_FILE).read_text())["outcome"] == runner.RAN, log
    assert "without its token" in log, "the run went through the #450 restart"
    qare = _qare_calls(path)
    assert [c["argv"][0] for c in qare] == ["plan", "run"]
    for call in qare:
        assert call["proxy"] == {
            "HTTPS_PROXY": listening_proxy, "HTTP_PROXY": listening_proxy, "NO_PROXY": LOOPBACK,
            "NODE_USE_ENV_PROXY": "1",
        }, call["argv"][0]


@pytest.mark.parametrize("sandbox", [False, True], ids=["no-sandbox", "sandbox"])
def test_without_an_allowlist_qare_gets_no_proxy_and_no_node_proxy_switch(tmp_path: Path, sandbox: bool) -> None:
    result, job, path = _run_job(tmp_path, sandbox=sandbox, proxy=None)
    log = _log(job)
    assert result.returncode == 0, result.stderr + log
    qare = _qare_calls(path)
    assert [c["argv"][0] for c in qare] == ["plan", "run"]
    for call in qare:
        assert call["proxy"] == {}
        assert "NODE_USE_ENV_PROXY" not in call["env"]


def _job_env_under_the_sandbox(tmp_path: Path, extra: dict[str, str]) -> dict[str, str]:
    """What the sandbox entry point hands the base entry point, read by a base that prints it."""
    stubs = _entry_stubs(tmp_path)
    seen = tmp_path / "job.env"
    stubs["base"].write_text(f'#!/bin/sh\nenv > "{seen}"\n')
    result = _run_entry(stubs, extra, "true")
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in seen.read_text().splitlines() if "=" in line)


def test_the_sandbox_entry_point_itself_points_node_at_the_proxy(tmp_path: Path, listening_proxy: str) -> None:
    """Not left to the base entry point: the sandbox's own exports carry it."""
    env = _job_env_under_the_sandbox(tmp_path, {
        EGRESS_PROXY_ENV: listening_proxy, "SANDBOX_PYTHON": sys.executable, "SANDBOX_PROXY_WAIT_SECONDS": "5",
        "SANDBOX_PROXY_HEARTBEAT_SECONDS": "0.2",
    })
    assert env["HTTPS_PROXY"] == listening_proxy
    assert env["NODE_USE_ENV_PROXY"] == "1"


def test_the_sandbox_entry_point_without_a_proxy_sets_no_node_proxy_switch(tmp_path: Path) -> None:
    env = _job_env_under_the_sandbox(tmp_path, {})
    assert "NODE_USE_ENV_PROXY" not in env
    assert "HTTPS_PROXY" not in env


def test_qare_env_carries_the_node_proxy_switch_only_when_the_job_has_it() -> None:
    proxied = {"PATH": "/usr/bin", "HTTPS_PROXY": "http://198.51.100.2:3128", "NODE_USE_ENV_PROXY": "1"}
    assert runner.qare_env(proxied, [])["NODE_USE_ENV_PROXY"] == "1"
    assert "NODE_USE_ENV_PROXY" not in runner.qare_env({"PATH": "/usr/bin"}, [])


def test_progettare_env_carries_the_node_proxy_switch_only_when_the_job_has_it() -> None:
    proxied = {"PATH": "/usr/bin", "HTTPS_PROXY": "http://198.51.100.2:3128", "NODE_USE_ENV_PROXY": "1"}
    assert progettare.progettare_env(proxied, {}, "")["NODE_USE_ENV_PROXY"] == "1"
    assert "NODE_USE_ENV_PROXY" not in progettare.progettare_env({"PATH": "/usr/bin"}, {}, "")
