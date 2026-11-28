"""435: QAre stage jobs with their own Docker engine, under the Sysbox runtime.

With ``stage_providers.<name>.sandbox: sysbox`` every container of a QAre provider runs with
``--runtime=sysbox-runc`` and nothing else changes: never ``--privileged``, never a Docker
socket, no relaxed security options. The sandbox image (``Dockerfile.sandbox``) starts its
own dockerd inside the job's one container, so #432's lookup by request id removes it with
the job. Docker is a fake ``docker`` on PATH throughout; nothing here starts a container.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coordinare import doctor
from coordinare.config import PerformerRoleConfig, PerformersConfig, ProjectConfiguration
from coordinare.doctor import CheckResult, Status, check_stage_providers
from coordinare.services import qare_runner
from coordinare.services.qare import GitHubAccess, QAreProvider
from coordinare.services.stage_contract import JobState, StageCriterion, StageRequest
from coordinare.services.stage_jobs import (
    SANDBOX_RUNTIME,
    DockerStageJobs,
    UnavailableStageJobs,
    job_name,
)
from coordinare.services.stage_providers import stage_jobs_for
from coordinare.stage_provider_config import ProgettareProviderConfig, QAreProviderConfig
from coordinare.transport.base import TransportError

ROOT = Path(__file__).resolve().parents[2]
PERFORMER = ROOT / "agent" / "performer"
TOKEN = "ghs_sandboxtokenvalue00000000000000000"
IMAGE = "ghcr.io/vividynamics/coordinare-performer-sandbox:test"
VERSION = "2026.11.44"
HEAD = "b" * 40
BASE = "a" * 40

_FAKE_DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
state = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
args = [a for a in args if not a.startswith("--runtime=")]
if args[:1] == ["info"]:
    if state.get("info_fails"):
        sys.stderr.write("Cannot connect to the Docker daemon\n")
        sys.exit(1)
    print(json.dumps({name: {"path": name} for name in state.get("runtimes", ["runc"])}))
elif args[:2] == ["run", "-d"]:
    print("c0ffee")
elif args[:2] == ["container", "inspect"]:
    if state.get("inspect", "missing") == "missing":
        sys.stderr.write("Error: No such container: x\n")
        sys.exit(1)
    print(state["inspect"])
elif args[:2] == ["rm", "-f"]:
    state["inspect"] = "missing"
    json.dump(state, open(os.environ["FAKE_DOCKER_STATE"], "w"))
elif args[:1] == ["run"] and state.get("run_fails"):
    sys.stderr.write(state["run_fails"] + "\n")
    sys.exit(125)
elif args[:1] == ["run"]:
    if args[-1].startswith("docker info"):
        if state.get("engine"):
            print(state["engine"])
        else:
            sys.stderr.write("Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n")
            sys.exit(1)
    else:
        print(state.get("version", ""))
'''


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake ``docker`` that records every argument list and answers from a state file."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(f"#!{sys.executable}\n{_FAKE_DOCKER}")
    script.chmod(0o755)
    state = tmp_path / "docker-state.json"
    log = tmp_path / "docker-log.jsonl"
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def set_state(**values: Any) -> None:
        state.write_text(json.dumps(values))

    def calls() -> list[list[str]]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def clear() -> None:
        log.unlink(missing_ok=True)

    set_state(runtimes=["runc", SANDBOX_RUNTIME], version=VERSION, engine="29.10.1")
    return {"bin": str(script), "set": set_state, "calls": calls, "clear": clear}


def _request() -> StageRequest:
    return StageRequest(
        card_id="PVTI_card", stage="qa", repository="acme/widgets", pr_number=7, pr_node_id="PR_7",
        head_sha=HEAD, base_sha=BASE, base_ref="main", issue_number=5, issue_node_id="I_5",
        criteria=(StageCriterion(id="ac-111111111111", text="The app boots"),),
    )


def _github() -> GitHubAccess:
    async def token(repository: str) -> str:
        del repository
        return TOKEN

    return GitHubAccess(token, lambda repository: "https://api.github.com")


def _config(sandbox: str | None) -> QAreProviderConfig:
    return QAreProviderConfig(kind="qare", version=VERSION, sandbox=sandbox, env_passthrough=["OPENAI_API_KEY"])


def _jobs(docker_bin: str, sandbox: str | None, *, egress: list[str] | None = None) -> DockerStageJobs:
    return DockerStageJobs(
        IMAGE, docker=docker_bin, egress_allowlist=egress, extra_hosts=["llm:192.0.2.10"] if egress else (),
        runtime=SANDBOX_RUNTIME if sandbox == "sysbox" else None,
    )


def _qare(work_dir: Path, docker_bin: str, sandbox: str | None, *, egress: list[str] | None = None) -> QAreProvider:
    return QAreProvider("qare", _config(sandbox), work_dir=work_dir, github=_github(),
                        jobs=_jobs(docker_bin, sandbox, egress=egress))


def _make_overdue(job_dir: Path) -> None:
    request = job_dir / qare_runner.REQUEST_FILE
    spec = json.loads(request.read_text())
    spec["created_at"] = time.time() - 10 * 86400
    request.write_text(json.dumps(spec))


async def _everything(provider: QAreProvider, job_dir_root: Path, docker: dict[str, Any]) -> list[list[str]]:
    """Every docker argument list a QAre provider produces: health, start, poll, stop."""
    docker["set"](runtimes=["runc", SANDBOX_RUNTIME], version=VERSION, engine="29.10.1", inspect="running")
    ok, detail = await provider.health()
    assert ok, detail
    request = _request()
    job = await provider.submit(request, request.request_id(1))
    assert (await provider.poll(job.ref)).state is JobState.PENDING
    _make_overdue(job_dir_root / job.ref)
    assert (await provider.poll(job.ref)).state is JobState.RETRYABLE
    return docker["calls"]()


# -- config --------------------------------------------------------------------------------


def test_the_sandbox_is_off_unless_configured() -> None:
    assert QAreProviderConfig(kind="qare", version=VERSION).sandbox is None


def test_sysbox_is_the_one_sandbox_the_config_accepts() -> None:
    assert _config("sysbox").sandbox == "sysbox"
    for wrong in ("dind", "privileged", "rootless", "gvisor", "Sysbox"):
        with pytest.raises(ValidationError):
            QAreProviderConfig(kind="qare", version=VERSION, sandbox=wrong)


def test_progettare_has_no_sandbox() -> None:
    with pytest.raises(ValidationError):
        ProgettareProviderConfig(kind="progettare", version="2026.11.2", runs_dir="/runs",
                                 gh_token_env="GH_TOKEN", sandbox="sysbox")


class _Config:
    def __init__(self) -> None:
        self.agent_transport = "docker"
        self.performer_endpoints: list[Any] = []


def test_a_sandboxed_provider_runs_under_sysbox_and_an_unsandboxed_one_does_not() -> None:
    config = _Config()
    on = stage_jobs_for("qare", _config("sysbox").model_copy(update={"image": IMAGE}), config, role="qa")
    off = stage_jobs_for("qare", _config(None).model_copy(update={"image": IMAGE}), config, role="qa")
    assert isinstance(on, DockerStageJobs)
    assert isinstance(off, DockerStageJobs)
    assert on.runtime == SANDBOX_RUNTIME
    assert off.runtime is None


def test_on_kubernetes_a_sandboxed_provider_is_unavailable_and_says_where_support_is_tracked() -> None:
    config = _Config()
    config.agent_transport = "kubernetes"
    jobs = stage_jobs_for("qare", _config("sysbox").model_copy(update={"image": IMAGE}), config, role="qa")
    assert isinstance(jobs, UnavailableStageJobs)
    assert "Kubernetes" in jobs.reason
    assert "#443" in jobs.reason


def test_a_sandbox_names_the_endpoint_whose_egress_allowlist_it_cannot_honour() -> None:
    """No endpoint serves qa, so the runs take the first endpoint's isolation: say which one."""
    from types import SimpleNamespace

    config = _Config()
    config.performer_endpoints = [SimpleNamespace(
        name="builders", mode="docker", image=IMAGE, roles=["implementer"], egress_allowlist=["api.github.com"],
        extra_hosts=[],
    )]
    jobs = stage_jobs_for("qare", _config("sysbox"), config, role="qa")
    assert isinstance(jobs, UnavailableStageJobs)
    assert "'builders'" in jobs.reason
    assert "egress_allowlist" in jobs.reason
    # Off, the endpoint's isolation applies as in #432.
    off = stage_jobs_for("qare", _config(None), config, role="qa")
    assert isinstance(off, DockerStageJobs)


# -- the runtime flag, and nothing else ----------------------------------------------------


def _normalised(args: list[str], probe_names: dict[str, str]) -> list[str]:
    """Probe container names and the private env file are random; give each its order instead."""
    out = []
    for index, arg in enumerate(args):
        if re.fullmatch(r"coordinare-stage-probe-[0-9a-f]+", arg):
            arg = probe_names.setdefault(arg, f"probe-{len(probe_names)}")
        elif index and args[index - 1] == "--env-file":
            arg = "<env file>"
        out.append(arg)
    return out


@pytest.mark.asyncio
async def test_sysbox_adds_exactly_the_runtime_flag_to_every_container(tmp_path: Path, docker: dict[str, Any]) -> None:
    work_dir = tmp_path / "qare"
    off = await _everything(_qare(work_dir, docker["bin"], None), work_dir, docker)
    for child in work_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
    docker["clear"]()
    on = await _everything(_qare(work_dir, docker["bin"], "sysbox"), work_dir, docker)

    off_runs = [a for a in off if a[:1] == ["run"]]
    on_runs = [a for a in on if a[:1] == ["run"]]
    assert off_runs, "the provider started no container at all"
    # The off path asks Docker nothing new; the on path only adds the runtime check.
    assert [a for a in on if a[:1] != ["run"] and a[:1] != ["info"]] == [a for a in off if a[:1] != ["run"]]
    assert not any(a[:1] == ["info"] for a in off)
    # Every container the sandbox starts: the off arguments, plus the flag right after `run`.
    names_off: dict[str, str] = {}
    names_on: dict[str, str] = {}
    sandbox_only = [a for a in on_runs if a[-1].startswith("docker info")]
    assert len(sandbox_only) == 1, "health checks the job image's own engine once, in sandbox mode only"
    shared = [a for a in on_runs if a not in sandbox_only]
    assert len(shared) == len(off_runs)
    for before, after in zip(off_runs, shared, strict=True):
        assert after.count(f"--runtime={SANDBOX_RUNTIME}") == 1, after
        assert after[1] == f"--runtime={SANDBOX_RUNTIME}", after
        without = [after[0], *after[2:]]
        assert _normalised(without, names_on) == _normalised(before, names_off)
    assert sandbox_only[0][1] == f"--runtime={SANDBOX_RUNTIME}"


@pytest.mark.asyncio
async def test_with_the_sandbox_off_no_container_names_a_runtime(tmp_path: Path, docker: dict[str, Any]) -> None:
    calls = await _everything(_qare(tmp_path / "qare", docker["bin"], None), tmp_path / "qare", docker)
    for args in calls:
        assert not any(a.startswith("--runtime") for a in args), args
    assert not any(a[:1] == ["info"] for a in calls)


# -- never the host's Docker, never privileged ---------------------------------------------

_FORBIDDEN_FLAGS = (
    "--privileged", "--security-opt", "--device", "--cap-drop", "--userns", "--pid", "--ipc",
    "--uts", "--cgroupns", "--cgroup-parent", "--network=host", "--net=host", "--volumes-from",
    "--mount", "--tmpfs", "--sysctl",
)
_SOCKETS = ("docker.sock", "containerd.sock", "/var/run/docker", "/run/docker", "/var/lib/docker", "podman.sock")


def _assert_never_the_host_or_privileged(args: list[str], job_dirs: set[str]) -> None:
    for index, arg in enumerate(args):
        flag = arg.split("=", 1)[0]
        assert flag not in _FORBIDDEN_FLAGS, args
        if arg == "host":
            assert args[index - 1] not in ("--network", "--net", "--pid", "--ipc", "--userns"), args
        for socket in _SOCKETS:
            assert socket not in arg, args
        for word in ("seccomp", "apparmor", "systempaths", "unconfined", "label=disable"):
            assert word not in arg.lower(), args
        assert not arg.startswith("DOCKER_HOST"), args
        if arg == "--cap-add":
            # The one capability a stage job ever gets: NET_ADMIN, for its own egress allowlist.
            assert args[index + 1] == "NET_ADMIN", args
            assert any(a.startswith("PERFORMER_EGRESS_ALLOWLIST=") for a in args), args
        assert not arg.startswith("--cap-add="), args
        if arg in ("-v", "--volume"):
            source = args[index + 1].split(":", 1)[0]
            assert source in job_dirs, f"a container mounts {source!r}, not its own job directory: {args}"
        if arg == "--runtime" or arg.startswith("--runtime="):
            assert arg == f"--runtime={SANDBOX_RUNTIME}", args


@pytest.mark.asyncio
@pytest.mark.parametrize(("sandbox", "egress"), [(None, None), (None, ["api.github.com"]), ("sysbox", None)])
async def test_no_qare_container_gets_the_host_docker_or_privilege(
    tmp_path: Path, docker: dict[str, Any], sandbox: str | None, egress: list[str] | None,
) -> None:
    """Fails if any container a QAre provider starts gets a Docker socket, privilege, or relaxed security."""
    work_dir = tmp_path / "qare"
    calls = await _everything(_qare(work_dir, docker["bin"], sandbox, egress=egress), work_dir, docker)
    job_dirs = {str(p) for p in work_dir.iterdir() if p.is_dir()}
    runs = [a for a in calls if a[:1] == ["run"]]
    assert len(runs) >= 2 + (1 if sandbox else 0)  # the version probe, the job, and the engine probe
    for args in calls:
        _assert_never_the_host_or_privileged(args, job_dirs)


def test_no_stage_job_module_names_a_docker_socket_or_privilege() -> None:
    services = ROOT / "src" / "coordinare" / "services"
    for name in ("stage_jobs.py", "qare.py", "stage_providers.py", "progettare.py"):
        text = (services / name).read_text(encoding="utf-8")
        for word in ("docker.sock", "--privileged", "--security-opt", "seccomp=", "apparmor="):
            assert word not in text, f"{name} names {word}"


# -- hosts without Sysbox ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_sysbox_runc_the_sandboxed_provider_is_unavailable_and_says_why(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    docker["set"](runtimes=["io.containerd.runc.v2", "runc"], version=VERSION, engine="29.10.1")
    provider = _qare(tmp_path / "qare", docker["bin"], "sysbox")
    ok, detail = await provider.health()
    assert not ok
    assert SANDBOX_RUNTIME in detail
    assert "Sysbox" in detail
    request = _request()
    with pytest.raises(TransportError, match=SANDBOX_RUNTIME):
        await provider.submit(request, request.request_id(1))
    assert not any(a[:1] == ["run"] for a in docker["calls"]()), "nothing may run without the runtime"
    assert not (tmp_path / "qare" / request.request_id(1)).exists()


@pytest.mark.asyncio
async def test_a_docker_that_cannot_list_its_runtimes_is_retried_not_read_as_missing(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    docker["set"](info_fails=True)
    with pytest.raises(TransportError, match="runtimes"):
        await _jobs(docker["bin"], "sysbox").probe(["qare", "--version"])


@pytest.mark.asyncio
async def test_the_runtime_is_checked_once_it_is_found(tmp_path: Path, docker: dict[str, Any]) -> None:
    jobs = _jobs(docker["bin"], "sysbox")
    for _ in range(3):
        await jobs.probe(["qare", "--version"])
    assert len([a for a in docker["calls"]() if a[:1] == ["info"]]) == 1


@pytest.mark.asyncio
async def test_a_runtime_that_goes_away_is_reported_as_missing_and_checked_again(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    jobs = _jobs(docker["bin"], "sysbox")
    await jobs.probe(["qare", "--version"])
    docker["set"](runtimes=["runc"], run_fails="docker: Error response from daemon: unknown or invalid runtime name: sysbox-runc.")
    with pytest.raises(TransportError, match="Install Sysbox"):
        await jobs.probe(["qare", "--version"])
    with pytest.raises(TransportError, match="Install Sysbox"):
        await jobs.probe(["qare", "--version"])
    assert len([a for a in docker["calls"]() if a[:1] == ["info"]]) == 2


@pytest.mark.asyncio
async def test_a_sandbox_image_without_its_own_engine_is_unhealthy(tmp_path: Path, docker: dict[str, Any]) -> None:
    docker["set"](runtimes=["runc", SANDBOX_RUNTIME], version=VERSION, engine="")
    ok, detail = await _qare(tmp_path / "qare", docker["bin"], "sysbox").health()
    assert not ok
    assert "Docker engine" in detail
    assert "Dockerfile.sandbox" in detail


@pytest.mark.asyncio
async def test_a_sandbox_with_an_egress_allowlist_is_unavailable_and_says_why(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    """The allowlist would not hold for containers the inner Docker starts: never run it half-enforced."""
    ok, detail = await _qare(tmp_path / "qare", docker["bin"], "sysbox", egress=["api.github.com"]).health()
    assert not ok
    assert "egress_allowlist" in detail
    assert not any(a[:1] == ["run"] for a in docker["calls"]())


# -- cleanup after a restart ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restarted_daemon_removes_the_sandbox_it_finds_by_request_id(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    """The inner dockerd lives in the job's one container: removing that is the whole cleanup."""
    work_dir = tmp_path / "qare"
    docker["set"](runtimes=["runc", SANDBOX_RUNTIME], inspect="running")
    request = _request()
    ref = request.request_id(1)
    await _qare(work_dir, docker["bin"], "sysbox").submit(request, ref)
    start = next(a for a in docker["calls"]() if a[0] == "run" and "-d" in a)
    job_dir = work_dir / ref
    # One mount, the job directory: no host directory or volume holds the inner Docker's data.
    mounts = [start[i + 1] for i, a in enumerate(start) if a in ("-v", "--volume", "--mount")]
    assert mounts == [f"{job_dir}:{job_dir}"]
    assert "--rm" in start
    assert f"coordinare.stage_job={ref}" in start

    docker["clear"]()
    _make_overdue(job_dir)
    restarted = _qare(work_dir, docker["bin"], "sysbox")  # a new daemon, no memory of the job
    job = await restarted.poll(ref)
    assert job.state is JobState.RETRYABLE
    calls = docker["calls"]()
    assert calls == [["rm", "-f", job_name(job_dir)]], calls


# -- doctor --------------------------------------------------------------------------------


def _project(sandbox: str | None, **kw: Any) -> Any:
    performers = PerformersConfig(implementer=PerformerRoleConfig(), qa=PerformerRoleConfig(provider="qare"))
    return ProjectConfiguration(
        project_name="widgets", github_org="acme", github_project_number=1, github_token="token",
        human_reviewers=["alice"], performers=performers,
        stage_providers={"qare": {"kind": "qare", "version": VERSION, "image": IMAGE, "sandbox": sandbox}},
        **kw,
    )


@pytest.fixture
def quiet_qare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor's other QAre checks, answered without Docker or GitHub."""
    async def health(self: QAreProvider) -> tuple[bool, str]:
        del self
        return True, "ok"

    async def profile(*args: Any) -> CheckResult:
        del args
        return CheckResult("profile", Status.OK, "ok", None)

    monkeypatch.setattr(QAreProvider, "health", health)
    monkeypatch.setattr(doctor, "_check_qare_profile", profile)


@pytest.mark.asyncio
async def test_doctor_reports_the_sandbox_unavailable_without_sysbox_runc(
    docker: dict[str, Any], quiet_qare: None,
) -> None:
    docker["set"](runtimes=["runc"])
    results = await check_stage_providers(_project("sysbox"))
    sandbox = [r for r in results if "sandbox" in r.name]
    assert len(sandbox) == 1
    assert sandbox[0].status is Status.FAIL
    assert SANDBOX_RUNTIME in sandbox[0].detail
    assert sandbox[0].fix is not None
    assert "Sysbox" in sandbox[0].fix
    assert ["info", "--format", "{{json .Runtimes}}"] in docker["calls"]()


@pytest.mark.asyncio
async def test_doctor_passes_the_sandbox_when_docker_lists_sysbox_runc(
    docker: dict[str, Any], quiet_qare: None,
) -> None:
    results = await check_stage_providers(_project("sysbox"))
    (sandbox,) = [r for r in results if "sandbox" in r.name]
    assert sandbox.status is Status.OK, sandbox.detail


@pytest.mark.asyncio
async def test_doctor_reports_the_sandbox_unavailable_on_kubernetes(
    docker: dict[str, Any], quiet_qare: None,
) -> None:
    results = await check_stage_providers(_project("sysbox", agent_transport="kubernetes"))
    (sandbox,) = [r for r in results if "sandbox" in r.name]
    assert sandbox.status is Status.FAIL
    assert "#443" in sandbox.detail
    assert not any(a[:1] == ["info"] for a in docker["calls"]())


@pytest.mark.asyncio
async def test_doctor_checks_no_sandbox_when_it_is_off(docker: dict[str, Any], quiet_qare: None) -> None:
    results = await check_stage_providers(_project(None))
    assert not [r for r in results if "sandbox" in r.name]
    assert docker["calls"]() == []


# -- the sandbox image ---------------------------------------------------------------------


def test_the_sandbox_image_is_a_variant_of_full_and_base_is_untouched() -> None:
    sandbox = (PERFORMER / "Dockerfile.sandbox").read_text(encoding="utf-8")
    assert re.search(r"^ARG BASE_IMAGE=coordinare-performer:full$", sandbox, re.MULTILINE)
    assert re.search(r"^FROM \$BASE_IMAGE$", sandbox, re.MULTILINE)
    for package in ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"):
        assert re.search(rf"{re.escape(package)}=\$\{{\w+\}}", sandbox), f"{package} is not pinned"
    for check in ("dockerd --version", "docker compose version", "docker buildx version"):
        assert check in sandbox
    assert 'ENTRYPOINT ["/sandbox-entrypoint.sh"]' in sandbox
    assert "VOLUME" not in sandbox
    # apt trusts exactly Docker's key: only that fingerprint is exported into the keyring.
    assert '--export "${DOCKER_KEY_FINGERPRINT}"' in sandbox
    assert "signed-by=/etc/apt/keyrings/docker.gpg" in sandbox  # an anonymous host volume would outlive nothing, but say so plainly
    for name in ("Dockerfile.base", "Dockerfile.full"):
        text = (PERFORMER / name).read_text(encoding="utf-8")
        assert "dockerd" not in text, f"{name} must not carry a Docker engine"
        assert "docker-ce" not in text, f"{name} must not carry a Docker engine"


def test_ci_builds_the_sandbox_image() -> None:
    workflow = (ROOT / ".github" / "workflows" / "pr-ci.yml").read_text(encoding="utf-8")
    assert "-f agent/performer/Dockerfile.sandbox agent/performer/" in workflow


# -- the sandbox entry point ---------------------------------------------------------------

ENTRYPOINT = PERFORMER / "sandbox-entrypoint.sh"


def _entry_stubs(tmp_path: Path, *, dockerd: str = "serve") -> dict[str, Path]:
    """Stub dockerd, docker and the base entry point on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    ready = tmp_path / "dockerd.ready"
    stubs = {
        # serve: becomes ready and stays up until killed; die: exits at once, as outside Sysbox.
        "dockerd": (
            f'echo "dockerd $*" >> "{log}"\n'
            + (f': > "{ready}"\nexec sleep 5\n' if dockerd == "serve" else "echo 'mount: permission denied' >&2\nexit 1\n")
        ),
        "docker": f'echo "docker $*" >> "{log}"\n[ -e "{ready}" ] || exit 1\n',
    }
    for name, body in stubs.items():
        path = bindir / name
        path.write_text(f"#!/bin/sh\n{body}")
        path.chmod(0o755)
    base = tmp_path / "entrypoint.sh"
    base.write_text(f'#!/bin/sh\necho "base $*" >> "{log}"\n')
    base.chmod(0o755)
    return {"bin": bindir, "log": log, "base": base}


def _run_entry(stubs: dict[str, Path], extra: dict[str, str] | None = None, *args: str) -> subprocess.CompletedProcess[str]:
    mountinfo = stubs["bin"].parent / "mountinfo"
    if not mountinfo.exists():
        mountinfo.write_text("22 1 0:21 / / rw - overlay overlay rw\n")
    env = {
        "SANDBOX_MOUNTINFO": str(mountinfo),
        "PATH": f"{stubs['bin']}:/usr/bin:/bin", "HOME": str(stubs["bin"].parent),
        "PERFORMER_BASE_ENTRYPOINT": str(stubs["base"]), "SANDBOX_DOCKERD_WAIT_SECONDS": "5",
        "SANDBOX_DOCKERD_LOG": str(stubs["bin"].parent / "dockerd.log"),
        **(extra or {}),
    }
    return subprocess.run(["sh", str(ENTRYPOINT), *args], env=env, capture_output=True, text=True, timeout=30,
                          check=False)


def _log(stubs: dict[str, Path]) -> list[str]:
    return stubs["log"].read_text().splitlines() if stubs["log"].exists() else []


def test_the_entry_point_starts_dockerd_before_the_job(tmp_path: Path) -> None:
    stubs = _entry_stubs(tmp_path)
    result = _run_entry(stubs, None, "sh", "-c", "exec python -m performer.stage_jobs qare /job")
    assert result.returncode == 0, result.stderr
    log = _log(stubs)
    started = [line for line in log if line.startswith("dockerd")]
    assert len(started) == 1
    job = "base sh -c exec python -m performer.stage_jobs qare /job"
    assert job in log
    assert log.index(job) == len(log) - 1, "the job starts last, once dockerd answered"
    assert log[log.index(job) - 1] == "docker info"
    # The inner engine is never told to listen anywhere but its own socket in the container.
    assert "-H" not in started[0].split()
    assert "tcp://" not in started[0]


def test_outside_sysbox_the_entry_point_says_so_and_runs_the_job_without_docker(tmp_path: Path) -> None:
    stubs = _entry_stubs(tmp_path, dockerd="die")
    result = _run_entry(stubs, None, "qare", "--version")
    assert result.returncode == 0, result.stderr
    assert "base qare --version" in _log(stubs)
    assert "sysbox-runc" in result.stderr
    assert "WARNING" in result.stderr


def _under_sysbox(stubs: dict[str, Path]) -> None:
    (stubs["bin"].parent / "mountinfo").write_text(
        "22 1 0:21 / / rw - overlay overlay rw\n"
        "30 22 0:44 /sys /proc/sys rw,nosuid - sysboxfs sysboxfs rw\n",
    )


def test_under_sysbox_a_dockerd_that_does_not_answer_stops_the_container(tmp_path: Path) -> None:
    """The sandbox was asked for: never run QAre without its Docker and blame the pull request."""
    stubs = _entry_stubs(tmp_path, dockerd="die")
    _under_sysbox(stubs)
    result = _run_entry(stubs, None, "sh", "-c", "exec python -m performer.stage_jobs qare /job")
    assert result.returncode != 0
    assert "refusing to start" in result.stderr
    assert not any(line.startswith("base") for line in _log(stubs))


def test_under_sysbox_a_dockerd_that_answers_runs_the_job(tmp_path: Path) -> None:
    stubs = _entry_stubs(tmp_path)
    _under_sysbox(stubs)
    result = _run_entry(stubs, None, "true")
    assert result.returncode == 0, result.stderr
    assert _log(stubs)[-1] == "base true"


def test_with_an_egress_allowlist_the_entry_point_refuses_to_start_the_sandbox(tmp_path: Path) -> None:
    """The base entry point's allowlist is OUTPUT only: the inner containers' traffic is forwarded past it."""
    stubs = _entry_stubs(tmp_path)
    result = _run_entry(stubs, {"PERFORMER_EGRESS_ALLOWLIST": "api.github.com"}, "true")
    assert result.returncode != 0
    assert "refusing to start" in result.stderr
    assert "base true" not in _log(stubs)
