"""450: no process in a stage job's container that the pull request's code can read holds a secret.

QAre runs the pull request's code as root in the stage job's container. Root there reads every
process's initial environment (``/proc/<pid>/environ``) and, with ``ptrace`` rights, its memory.
So a secret may be held only by a process that needs it, and only until the pull request's code
starts:

- the sandbox entry point (``sandbox-entrypoint.sh``) leaves dockerd (and the containerd it
  starts) and the egress proxy heartbeat running beside the job: they get an environment of
  their own, never the container's, which holds the job's GitHub token and the operator's
  ``env_passthrough`` values;
- the stage job needs the token for the checkout and GitHub's compare diff only, both done before
  QAre runs anything from the pull request; the process that then waits on QAre is a fresh one
  (``execve``) that never held it, so nothing of it is left in its memory.

Secrets here are canaries, as in the #435 proof that found the leak.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from performer.stage_jobs import qare as runner

from coordinare.services.stage_jobs import CREDENTIAL_ENV, EGRESS_PROXY_ENV
from tests.unit import test_447_sandbox_egress as egress
from tests.unit.qare_fixtures import calls, make_remote, request_spec, scenario
from tests.unit.stage_job_fixtures import container_env
from tests.unit.test_435_qare_sandbox import _entry_stubs, _run_entry, _under_sysbox

TOKEN = "ghs_" + "450canary" + "0" * 27
PASSTHROUGH = "OPENAI_API_KEY"
LINUX = sys.platform.startswith("linux")
#: 447's fake ``docker``, a fixture.
docker = egress.docker
_LARGEST_REGION = 1 << 30


def _secrets() -> dict[str, str]:
    """A configured secret of each kind: the job's token and an ``env_passthrough`` value."""
    return {CREDENTIAL_ENV: f"canary-token-{uuid.uuid4().hex}", PASSTHROUGH: f"canary-key-{uuid.uuid4().hex}"}


def _leaks(environment: dict[str, str], secrets: dict[str, str]) -> list[str]:
    """Each variable whose name is a secret's, or whose value holds a secret's value."""
    return sorted(
        name for name, value in environment.items()
        if name in secrets or any(secret in value for secret in secrets.values())
    )


def _parse_env(text: str) -> list[dict[str, str]]:
    """``env -0`` records, one block per call, separated by a line ``--``."""
    blocks = []
    for block in text.split("\0--\0"):
        entries = [entry for entry in block.split("\0") if "=" in entry]
        if entries:
            blocks.append(dict(entry.split("=", 1) for entry in entries))
    return blocks


def _recording_stubs(tmp_path: Path) -> dict[str, Path]:
    """The 435 stubs, with dockerd, the heartbeat's interpreter and the job recording their environment."""
    stubs = _entry_stubs(tmp_path)
    envs = tmp_path / "envs"
    envs.mkdir()
    record = 'env -0 >> "{0}"; printf "\\0--\\0" >> "{0}"'
    dockerd = stubs["bin"] / "dockerd"
    dockerd.write_text(dockerd.read_text().replace("#!/bin/sh\n", "#!/bin/sh\n" + record.format(envs / "dockerd") + "\n", 1))
    probe = stubs["bin"] / "probe-python"
    probe.write_text(f'#!/bin/sh\necho "probe $3 $4" >> "{stubs["log"]}"\n{record.format(envs / "probe")}\nexit 0\n')
    probe.chmod(0o755)
    stubs["base"].write_text(
        f'#!/bin/sh\n{record.format(envs / "job")}\necho "base $*" >> "{stubs["log"]}"\nsleep 1.2\n'
        f'echo "base done" >> "{stubs["log"]}"\n',
    )
    stubs["envs"] = envs
    stubs["probe"] = probe
    return stubs


def _recorded(stubs: dict[str, Path], name: str) -> list[dict[str, str]]:
    path = stubs["envs"] / name
    return _parse_env(path.read_text()) if path.exists() else []


def _entry_env(stubs: dict[str, Path], secrets: dict[str, str], *, proxy: bool) -> dict[str, str]:
    env = {**secrets, "SANDBOX_PROXY_HEARTBEAT_SECONDS": "0.2"}
    if proxy:
        env.update({"SANDBOX_PYTHON": str(stubs["probe"]), "SANDBOX_PROXY_WAIT_SECONDS": "1",
                    EGRESS_PROXY_ENV: "http://198.51.100.2:3128"})
    return env


# -- the sandbox entry point ---------------------------------------------------------------


@pytest.mark.parametrize("proxy", [False, True], ids=["no-allowlist", "egress-proxy"])
def test_no_daemon_the_sandbox_entry_point_starts_inherits_a_secret(tmp_path: Path, proxy: bool) -> None:
    stubs = _recording_stubs(tmp_path)
    _under_sysbox(stubs)
    secrets = _secrets()
    result = _run_entry(stubs, _entry_env(stubs, secrets, proxy=proxy), "true")
    assert result.returncode == 0, result.stderr
    time.sleep(0.5)  # any heartbeat under way has recorded
    dockerd = _recorded(stubs, "dockerd")
    assert len(dockerd) == 1
    assert _leaks(dockerd[0], secrets) == [], "dockerd, and the containerd it starts, keep their environment"
    assert dockerd[0]["PATH"] == f"{stubs['bin']}:/usr/bin:/bin", "dockerd still finds containerd and runc"
    if proxy:
        heartbeats = _recorded(stubs, "probe")[1:]  # the first is the wait for the proxy, which has ended
        assert len(heartbeats) >= 2, _recorded(stubs, "probe")
        for heartbeat in heartbeats:
            assert _leaks(heartbeat, secrets) == [], "the heartbeat runs as long as the job"


def test_dockerd_still_pulls_through_the_egress_proxy(tmp_path: Path) -> None:
    stubs = _recording_stubs(tmp_path)
    _under_sysbox(stubs)
    result = _run_entry(stubs, _entry_env(stubs, _secrets(), proxy=True), "true")
    assert result.returncode == 0, result.stderr
    dockerd = _recorded(stubs, "dockerd")[0]
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert dockerd[name] == "http://198.51.100.2:3128", name
    for name in ("NO_PROXY", "no_proxy"):
        assert dockerd[name] == "localhost,127.0.0.1,::1", name


def test_dockerd_gets_the_operators_proxy_when_there_is_no_allowlist(tmp_path: Path) -> None:
    stubs = _recording_stubs(tmp_path)
    _under_sysbox(stubs)
    env = {**_entry_env(stubs, _secrets(), proxy=False), "HTTPS_PROXY": "http://proxy.example:8080"}
    result = _run_entry(stubs, env, "true")
    assert result.returncode == 0, result.stderr
    dockerd = _recorded(stubs, "dockerd")[0]
    assert dockerd["HTTPS_PROXY"] == "http://proxy.example:8080"
    assert "HTTP_PROXY" not in dockerd, "a variable the container lacks is not invented"


def test_the_job_still_gets_its_token_and_passthrough(tmp_path: Path) -> None:
    """Only the job takes the container's environment; it lets go of the token itself."""
    stubs = _recording_stubs(tmp_path)
    _under_sysbox(stubs)
    secrets = _secrets()
    result = _run_entry(stubs, _entry_env(stubs, secrets, proxy=True), "true")
    assert result.returncode == 0, result.stderr
    job = _recorded(stubs, "job")
    assert len(job) == 1
    assert {name: job[0].get(name) for name in secrets} == secrets
    assert job[0]["HTTPS_PROXY"] == "http://198.51.100.2:3128"


@pytest.mark.skipif(not LINUX, reason="reads /proc, as the #435 proof did")
def test_inside_a_running_sandbox_no_process_holds_the_token_in_its_environment(tmp_path: Path) -> None:
    """The #435 proof's scan: once the job has let go of the token, no /proc/<pid>/environ holds it."""
    stubs = _recording_stubs(tmp_path)
    _under_sysbox(stubs)
    secrets = _secrets()
    found = tmp_path / "found"
    # The stand-in job drops the token as the stage job does (it starts itself again without
    # it), then scans while dockerd and the heartbeat run.
    scan = (
        f'sleep 0.6; for e in /proc/[0-9]*/environ; do grep -aqF "{secrets[CREDENTIAL_ENV]}" "$e" 2>/dev/null '
        f'&& echo "$e $(tr "\\0" " " < "${{e%/environ}}/cmdline")" >> "{found}"; done; :'
    )
    stubs["base"].write_text(f"#!/bin/sh\nexec env -u {CREDENTIAL_ENV} sh -c '{scan}'\n")
    result = _run_entry(stubs, _entry_env(stubs, secrets, proxy=True), "true")
    assert result.returncode == 0, result.stderr
    assert _recorded(stubs, "dockerd"), "dockerd was running during the scan"
    assert not found.exists(), found.read_text()


# -- the stage job ---------------------------------------------------------------------------


@pytest.fixture
def _no_network_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    def diff(request: dict, *, base: str, head: str, token: str, deadline: float) -> str:
        assert token == TOKEN
        return f"diff --git a/app.txt b/app.txt\n# {base[:7]}..{head[:7]}\n"

    monkeypatch.setattr(runner, "_change_diff", diff)


@pytest.mark.usefixtures("_no_network_diff")
def test_the_job_restarts_without_the_token_once_the_checkout_and_diff_are_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    web, base, head = make_remote(tmp_path)
    path = scenario(tmp_path)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(path))
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=web, base=base, head=head)
    handed: list[dict[str, Any]] = []

    def restart(state: dict[str, Any]) -> None:
        handed.append(state)
        # Nothing of the pull request has run yet: only QAre's version was asked for.
        assert [c["argv"][0] for c in calls(path)] == ["--version"]
        assert (job / runner.DIFF_FILE).read_text().startswith("diff --git")
        assert Path(state["scratch"], "repo", "app.txt").read_text() == "v2\n"
        # What the fresh process is started with holds no secret.
        assert TOKEN not in json.dumps(state)
        runner.resume_job(job, state)  # as the fresh process does

    status = runner.run_job(job, token=TOKEN, restart=restart)
    assert len(handed) == 1
    assert status["outcome"] == runner.RAN, status
    assert [c["argv"][0] for c in calls(path)] == ["--version", "plan", "run"]
    assert json.loads((job / runner.STATUS_FILE).read_text())["outcome"] == runner.RAN
    assert not Path(handed[0]["scratch"]).exists(), "the checkouts still go once the run ends"


@pytest.mark.usefixtures("_no_network_diff")
def test_a_run_that_stops_before_qare_runs_never_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web, base, head = make_remote(tmp_path, profile_on_base=False)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(scenario(tmp_path)))
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=web, base=base, head=head)
    handed: list[dict[str, Any]] = []
    status = runner.run_job(job, token=TOKEN, restart=handed.append)
    assert status["outcome"] == runner.NO_PROFILE
    assert handed == []


def test_a_resumed_run_whose_checkout_is_gone_ends_with_an_error(tmp_path: Path) -> None:
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base="file:///x", base="a" * 40, head="b" * 40)
    status = runner.resume_job(job, {"scratch": str(tmp_path / "gone"), "started": time.monotonic()})
    assert status["outcome"] == runner.ERROR
    assert "checkout" in status["reason"]


def _start_job(tmp_path: Path, **scene: Any) -> tuple[subprocess.Popen[bytes], Path, Path]:
    """The real entry, ``python -m performer.stage_jobs qare``, with the token as Docker gives it."""
    web, _base, head = make_remote(tmp_path, profile_on_head=True)
    path = scenario(tmp_path, **scene)
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=web, base=head, head=head)  # one-sided: no compare diff to fetch
    env = container_env(env={"FAKE_QARE_SCENARIO": str(path), CREDENTIAL_ENV: TOKEN})
    with (job / "job.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "performer.stage_jobs", "qare", str(job)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, cwd=job,
        )
    return process, job, path


def test_the_job_runs_to_its_verdict_through_the_restart(tmp_path: Path) -> None:
    process, job, path = _start_job(tmp_path)
    assert process.wait(timeout=120) == 0, (job / "job.log").read_text()
    status = json.loads((job / runner.STATUS_FILE).read_text())
    assert status["outcome"] == runner.RAN, status
    assert [c["argv"][0] for c in calls(path)] == ["--version", "plan", "run"]
    log = (job / "job.log").read_text()
    assert "stage job qare started" in log
    assert "without its token" in log
    assert TOKEN not in log


def _memory_holds(pid: int, needles: list[bytes]) -> list[str]:
    """Which of ``needles`` the process's readable memory holds, with where."""
    found = []
    with Path(f"/proc/{pid}/maps").open() as maps, Path(f"/proc/{pid}/mem").open("rb", buffering=0) as mem:
        for line in maps:
            span, perms = line.split()[:2]
            if not perms.startswith("r"):
                continue
            start, end = (int(part, 16) for part in span.split("-"))
            if end - start > _LARGEST_REGION:
                continue  # a reservation, not a heap a token could be in
            try:
                mem.seek(start)
                chunk = mem.read(end - start)
            except (OSError, OverflowError, ValueError):
                continue
            found += [f"{needle[:8]!r}... in {line.strip()}" for needle in needles if needle in chunk]
    return found


@pytest.mark.skipif(not LINUX, reason="reads /proc/<pid>/mem")
def test_the_process_that_waits_on_qare_never_held_the_token(tmp_path: Path) -> None:
    """While QAre runs (and with it the pull request's code), its parent holds no copy of the token."""
    hold = tmp_path / "hold"
    process, job, _path = _start_job(tmp_path, hold=str(hold))
    try:
        ready = Path(f"{hold}.ready")
        deadline = time.monotonic() + 60
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), (job / "job.log").read_text()
        parent = json.loads(ready.read_text())["ppid"]
        assert parent == process.pid, "QAre's parent is the job, started again in the same process"
        assert TOKEN.encode() not in Path(f"/proc/{parent}/environ").read_bytes()
        assert TOKEN.encode() not in Path(f"/proc/{parent}/cmdline").read_bytes()
        header = base64.b64encode(f"x-access-token:{TOKEN}".encode())
        try:
            held = _memory_holds(parent, [TOKEN.encode(), header])
        except PermissionError:
            pytest.skip("this host does not let a parent read its child's memory (ptrace restrictions)")
        assert held == []
    finally:
        Path(f"{hold}.release").write_text("")
        process.wait(timeout=60)
    assert json.loads((job / runner.STATUS_FILE).read_text())["outcome"] == runner.RAN


# -- coordinare's side -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_egress_proxy_container_is_given_no_variable(tmp_path: Path, docker: dict[str, Any]) -> None:
    """The proxy runs nothing of the pull request, and needs no secret: it gets none."""
    _job, _dir, docker_calls = await egress._submit(tmp_path / "qare", docker)
    proxy = next(args for args in docker_calls if egress._is_proxy_run(args))
    assert not {"-e", "--env", "--env-file"} & set(proxy), proxy
    assert not any(arg.startswith(("--env=", "--env-file=")) for arg in proxy), proxy



def test_a_dockerd_that_never_answers_is_stopped_itself(tmp_path: Path) -> None:
    """The pid the entry point stops is dockerd's own, not a wrapper's that would leave it running."""
    stubs = _recording_stubs(tmp_path)
    pid_file = tmp_path / "dockerd.pid"
    (stubs["bin"] / "dockerd").write_text(f'#!/bin/sh\necho $$ > "{pid_file}"\nexec sleep 30\n')
    result = _run_entry(stubs, {"SANDBOX_DOCKERD_WAIT_SECONDS": "1"}, "true")
    assert result.returncode == 0, result.stderr  # outside Sysbox it goes on without Docker
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("dockerd kept running after the entry point gave up on it")


def test_dockerd_keeps_the_certificates_the_operator_passes(tmp_path: Path) -> None:
    """Pulls through a TLS-intercepting proxy need its CA: not a secret, so dockerd gets it."""
    stubs = _recording_stubs(tmp_path)
    _under_sysbox(stubs)
    env = {**_entry_env(stubs, _secrets(), proxy=False), "SSL_CERT_FILE": "/etc/ca/proxy.pem", "SSL_CERT_DIR": "/etc/ca"}
    result = _run_entry(stubs, env, "true")
    assert result.returncode == 0, result.stderr
    dockerd = _recorded(stubs, "dockerd")[0]
    assert dockerd["SSL_CERT_FILE"] == "/etc/ca/proxy.pem"
    assert dockerd["SSL_CERT_DIR"] == "/etc/ca"


@pytest.mark.parametrize("state", ["{not json", '{"scratch": "/nowhere"}'], ids=["not-json", "no-start"])
def test_a_resume_state_that_cannot_be_read_still_ends_with_a_status(tmp_path: Path, state: str) -> None:
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base="file:///x", base="a" * 40, head="b" * 40)
    env = container_env(env={"COORDINARE_STAGE_RESUME": state})
    done = subprocess.run([sys.executable, "-m", "performer.stage_jobs", "qare", str(job)], env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert json.loads((job / runner.STATUS_FILE).read_text())["outcome"] == runner.ERROR


def test_only_a_qare_job_resumes(tmp_path: Path) -> None:
    env = container_env(env={"COORDINARE_STAGE_RESUME": json.dumps({"scratch": str(tmp_path), "started": 0})})
    done = subprocess.run([sys.executable, "-m", "performer.stage_jobs", "progettare", str(tmp_path)], env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 2
    assert "resume" in done.stderr
