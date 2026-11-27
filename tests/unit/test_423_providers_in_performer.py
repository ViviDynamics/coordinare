"""423: QAre and Progettare run in a performer, so coordinare's host never clones.

The production path is exercised here with a fake ``docker`` on PATH: whatever the
providers do on the daemon's side, the only program they start is ``docker``, and never
``git``. The runs themselves (checkout, CLI) are the performer's, tested in
``test_404_qare_runner.py`` and through ``LocalStageJobs`` in the provider tests.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from coordinare.services import progettare_runner, qare_runner, stage_jobs
from coordinare.services.progettare import ProgettareProvider
from coordinare.services.qare import GitHubAccess, QAreProvider
from coordinare.services.stage_contract import (
    JobState,
    StageCriterion,
    StageRequest,
    WorkItemRequest,
    work_item_digest,
    work_item_pin,
)
from coordinare.services.stage_jobs import (
    CREDENTIAL_ENV,
    STAGE_JOB_LABEL,
    DockerStageJobs,
    RunState,
    StageJobLaunch,
    UnavailableStageJobs,
    job_name,
)
from coordinare.services.stage_providers import stage_jobs_for
from coordinare.stage_provider_config import ProgettareProviderConfig, QAreProviderConfig

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "ghs_stagejobtokenvalue0000000000000000"
IMAGE = "ghcr.io/vividynamics/coordinare-performer-full:test"
HEAD = "b" * 40
BASE = "a" * 40
CRITERIA = (StageCriterion(id="ac-111111111111", text="The export works"),)

_FAKE_DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
env_file = ""
if "--env-file" in args:
    env_file = args[args.index("--env-file") + 1]
names = []
if env_file:
    names = [line.split("=", 1)[0] for line in open(env_file).read().splitlines() if line]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "env_file_names": names}) + "\n")
state = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
if args[:2] == ["run", "-d"]:
    if state.get("taken"):
        sys.stderr.write('Conflict. The container name "/x" is already in use by container "abc".\n')
        sys.exit(125)
    print("c0ffee")
elif args[:2] == ["container", "inspect"]:
    if state.get("inspect", "missing") == "missing":
        sys.stderr.write("Error: No such container: x\n")
        sys.exit(1)
    print(state["inspect"])
elif args[:2] == ["rm", "-f"]:
    state["taken"] = False
    state["inspect"] = "missing"
    json.dump(state, open(os.environ["FAKE_DOCKER_STATE"], "w"))
elif args[:1] == ["run"]:
    print(state.get("version", ""))
'''


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake ``docker`` that records what it is asked and answers from a state file."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(f"#!{sys.executable}\n{_FAKE_DOCKER}")
    script.chmod(0o755)
    state = tmp_path / "docker-state.json"
    log = tmp_path / "docker-log.jsonl"
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))

    def set_state(**values: Any) -> None:
        state.write_text(json.dumps(values))

    def calls() -> list[dict[str, Any]]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    set_state()
    return {"bin": str(script), "set": set_state, "calls": calls}


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every program the test process starts, whether through subprocess or asyncio."""
    seen: list[list[str]] = []
    real = vars(subprocess.Popen)["_execute_child"]

    def record(self: subprocess.Popen[Any], args: Any, *rest: Any, **kw: Any) -> Any:
        seen.append([str(a) for a in (args if isinstance(args, (list, tuple)) else [args])])
        return real(self, args, *rest, **kw)

    monkeypatch.setattr(subprocess.Popen, "_execute_child", record)
    return seen


def _assert_no_git(spawned: list[list[str]], docker_bin: str) -> None:
    assert spawned, "the providers started nothing at all"
    for argv in spawned:
        assert Path(argv[0]).name != "git", argv
        assert "git" not in [Path(a).name for a in argv[:2]], argv
        assert argv[0] == docker_bin, argv


def _qa_request() -> StageRequest:
    return StageRequest(
        card_id="PVTI_card", stage="qa", repository="acme/widgets", pr_number=7, pr_node_id="PR_7",
        head_sha=HEAD, base_sha=BASE, base_ref="main", issue_number=5, issue_node_id="I_5", criteria=CRITERIA,
    )


def _github() -> GitHubAccess:
    async def token(repository: str) -> str:
        return TOKEN

    return GitHubAccess(token, lambda repository: "https://api.github.com")


def _qare(tmp_path: Path, docker_bin: str) -> QAreProvider:
    config = QAreProviderConfig(kind="qare", version="2026.11.44", env_passthrough=["OPENAI_API_KEY"])
    return QAreProvider("qare", config, work_dir=tmp_path / "qare", github=_github(),
                        jobs=DockerStageJobs(IMAGE, docker=docker_bin))


TITLE, BODY = "Export widgets", "## Acceptance\n- [ ] a CSV\n"


def _work_item() -> WorkItemRequest:
    return WorkItemRequest(card_id="PVTI_card1", stage="architecting", repository="acme/widgets", number=12,
                           node_id="I_12", pin=work_item_pin(work_item_digest(TITLE, BODY), []))


def _progettare(tmp_path: Path, docker_bin: str) -> ProgettareProvider:
    config = ProgettareProviderConfig(
        kind="progettare", version="2026.11.2", runs_dir=str(tmp_path / "runs"),
        gh_token_env="PROGETTARE_423_TOKEN", pass_env=["OPENAI_API_KEY"],
    )
    return ProgettareProvider("progettare", config, jobs=DockerStageJobs(IMAGE, docker=docker_bin))


# -- the acceptance criterion: no git on the daemon's host ------------------------------------


@pytest.mark.asyncio
async def test_qare_never_spawns_git_on_the_daemon_host(
    tmp_path: Path, docker: dict[str, Any], spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    docker["set"](inspect="running", version="2026.11.44")
    provider = _qare(tmp_path, docker["bin"])
    assert (await provider.health())[0]
    request = _qa_request()
    job = await provider.submit(request, request.request_id(1))
    assert job.state is JobState.PENDING
    assert (await provider.poll(job.ref)).state is JobState.PENDING
    docker["set"](inspect="missing")
    assert (await provider.poll(job.ref)).state is JobState.RETRYABLE
    _assert_no_git(spawned, docker["bin"])
    job_dir = tmp_path / "qare" / job.ref
    assert not (job_dir / "repo").exists()
    assert not (job_dir / "base").exists()

    run = next(c for c in docker["calls"]() if c["args"][:2] == ["run", "-d"])
    args = run["args"]
    assert f"{job_dir}:{job_dir}" in args
    assert f"{STAGE_JOB_LABEL}={job.ref}" in args
    assert IMAGE in args
    assert args[args.index(IMAGE) + 1:][:2] == ["sh", "-c"]
    assert f"python -m performer.stage_jobs qare {job_dir}" in args[-1]
    # The token reaches the container in a private env file, never as an argument.
    assert run["env_file_names"] == [CREDENTIAL_ENV]
    assert not any(TOKEN in a for a in args)
    assert "OPENAI_API_KEY" in args
    assert "sk-test" not in " ".join(args)
    # Not a performer the startup sweep reaps, so a restart leaves the run going.
    assert not any(a.startswith("coordinare.performer.id") for a in args)


@pytest.mark.asyncio
async def test_progettare_never_spawns_git_on_the_daemon_host(
    tmp_path: Path, docker: dict[str, Any], spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROGETTARE_423_TOKEN", TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    docker["set"](inspect="missing", version="2026.11.2")
    provider = _progettare(tmp_path, docker["bin"])
    request = _work_item()
    job = await provider.submit(request, request.request_id(1))
    assert job.state is JobState.PENDING
    docker["set"](inspect="running")
    assert (await provider.poll(job.ref)).state is JobState.PENDING
    docker["set"](inspect="missing")
    assert (await provider.poll(job.ref)).state is JobState.RETRYABLE
    _assert_no_git(spawned, docker["bin"])
    directory = tmp_path / "runs" / job.ref
    assert not (directory / "checkout").exists()
    run = next(c for c in docker["calls"]() if c["args"][:2] == ["run", "-d"])
    assert f"python -m performer.stage_jobs progettare {directory}" in run["args"][-1]
    assert run["env_file_names"] == [CREDENTIAL_ENV]
    assert not any(TOKEN in a for a in run["args"])


def test_no_provider_module_names_git_as_a_program() -> None:
    """The daemon side of every stage provider starts no git, by construction."""
    services = ROOT / "src" / "coordinare" / "services"
    for name in ("qare.py", "qare_runner.py", "qare_publish.py", "progettare.py", "progettare_runner.py",
                 "stage_jobs.py", "stage_providers.py", "stage_contract.py", "scrutare.py"):
        text = (services / name).read_text(encoding="utf-8")
        assert not re.search(r"""(create_subprocess_exec|subprocess\.\w+|Popen|which)\(\s*\[?\s*["']git["']""", text), name
        assert "GIT_CONFIG" not in text, name


def test_agents_md_lists_no_exception_to_107_and_555() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8").lower()
    assert "exception to 107" not in text
    assert "exceptions to 107" not in text
    assert "one exception to 107" not in text


# -- a restart resumes the same performer job ------------------------------------------------


@pytest.mark.asyncio
async def test_a_restarted_daemon_finds_the_qare_job_still_running(tmp_path: Path, docker: dict[str, Any]) -> None:
    docker["set"](inspect="running")
    request = _qa_request()
    first = await _qare(tmp_path, docker["bin"]).submit(request, request.request_id(1))
    restarted = _qare(tmp_path, docker["bin"])  # a new daemon, no memory of the first
    again = await restarted.submit(request, request.request_id(1))
    assert again.ref == first.ref
    assert again.state is JobState.PENDING
    assert len([c for c in docker["calls"]() if c["args"][:2] == ["run", "-d"]]) == 1


@pytest.mark.asyncio
async def test_a_restarted_daemon_finds_the_progettare_job_still_running(
    tmp_path: Path, docker: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROGETTARE_423_TOKEN", TOKEN)
    docker["set"](inspect="missing", version="2026.11.2")
    request = _work_item()
    await _progettare(tmp_path, docker["bin"]).submit(request, request.request_id(1))
    docker["set"](inspect="running", version="2026.11.2")
    again = await _progettare(tmp_path, docker["bin"]).submit(request, request.request_id(1))
    assert again.state is JobState.PENDING
    assert len([c for c in docker["calls"]() if c["args"][:2] == ["run", "-d"]]) == 1


@pytest.mark.asyncio
async def test_a_docker_that_cannot_answer_is_retried_not_read_as_over(tmp_path: Path) -> None:
    jobs = DockerStageJobs(IMAGE, docker=str(tmp_path / "no-docker"))
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    from coordinare.transport.base import TransportError

    with pytest.raises(TransportError, match="could not be started"):
        await jobs.state(job_dir)


# -- the Docker runtime ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_job_started_twice_is_the_same_container(tmp_path: Path, docker: dict[str, Any]) -> None:
    docker["set"](taken=True, inspect="running")
    jobs = DockerStageJobs(IMAGE, docker=docker["bin"])
    launch = StageJobLaunch(kind="qare", ref="coordinare.qa.x.1", provider="qare", job_dir=tmp_path)
    assert await jobs.start(launch) == job_name(tmp_path)
    assert [c["args"][0] for c in docker["calls"]()] == ["run", "container"]


@pytest.mark.asyncio
async def test_an_ended_container_of_the_same_name_is_replaced(tmp_path: Path, docker: dict[str, Any]) -> None:
    docker["set"](taken=True, inspect="exited")
    jobs = DockerStageJobs(IMAGE, docker=docker["bin"])
    launch = StageJobLaunch(kind="qare", ref="coordinare.qa.x.1", provider="qare", job_dir=tmp_path)
    assert await jobs.start(launch) == job_name(tmp_path)
    assert [c["args"][0] for c in docker["calls"]()] == ["run", "container", "rm", "run"]


@pytest.mark.asyncio
async def test_a_docker_that_hangs_is_a_retryable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare.transport.base import TransportError

    monkeypatch.setattr(stage_jobs, "_DOCKER_TIMEOUT_SECONDS", 0.2)
    slow = tmp_path / "slow-docker"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    jobs = DockerStageJobs(IMAGE, docker=str(slow))
    with pytest.raises(TransportError, match="did not answer"):
        await jobs.state(tmp_path)
    await jobs.stop(tmp_path)  # best effort, never raises


def test_a_job_that_logged_nothing_never_started(tmp_path: Path) -> None:
    assert stage_jobs.never_started(tmp_path)
    (tmp_path / "runner.log").write_text("")
    assert stage_jobs.never_started(tmp_path)
    (tmp_path / "runner.log").write_text("stage job qare started\n")
    assert not stage_jobs.never_started(tmp_path)


def test_the_token_never_stays_in_the_jobs_initial_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry starts itself again without the token, and reads it from a pipe."""
    from performer.stage_jobs import __main__ as entry

    started: dict[str, Any] = {}

    def execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        started.update(path=path, argv=argv, env=env)
        raise SystemExit(0)

    monkeypatch.setattr(entry.os, "execve", execve)
    monkeypatch.setattr(entry.sys, "argv", ["stage_jobs", "qare", str(tmp_path)])
    monkeypatch.setenv(CREDENTIAL_ENV, TOKEN)
    with pytest.raises(SystemExit):
        entry.main()
    assert CREDENTIAL_ENV not in started["env"]
    assert TOKEN not in json.dumps(started)
    assert started["argv"][1:] == ["-m", "performer.stage_jobs", "qare", str(tmp_path)]
    # The process started again finds the token on the descriptor, once.
    monkeypatch.delenv(CREDENTIAL_ENV, raising=False)
    monkeypatch.setenv(entry.HANDOVER_FD_ENV, started["env"][entry.HANDOVER_FD_ENV])
    assert entry._handed_over_token() == TOKEN
    assert entry.HANDOVER_FD_ENV not in os.environ


@pytest.mark.asyncio
async def test_job_states_follow_docker(tmp_path: Path, docker: dict[str, Any]) -> None:
    jobs = DockerStageJobs(IMAGE, docker=docker["bin"])
    for answer, state in (("running", RunState.RUNNING), ("created", RunState.RUNNING),
                          ("exited", RunState.GONE), ("missing", RunState.GONE)):
        docker["set"](inspect=answer)
        assert await jobs.state(tmp_path) is state, answer


def test_the_container_name_is_fixed_by_the_job_directory(tmp_path: Path) -> None:
    assert job_name(tmp_path / "a") == job_name(tmp_path / "a")
    assert job_name(tmp_path / "a") != job_name(tmp_path / "b")


def test_the_isolation_of_the_roles_performer_applies(tmp_path: Path) -> None:
    jobs = DockerStageJobs(IMAGE, egress_allowlist=["api.github.com"], extra_hosts=["h:1.2.3.4"])
    args = jobs._isolation()
    assert args[2:4] == ["--cap-add", "NET_ADMIN"]
    assert "PERFORMER_EGRESS_ALLOWLIST=api.github.com" in args
    assert args[:2] == ["--add-host", "h:1.2.3.4"]


class _Endpoint:
    def __init__(self, roles: list[str], image: str | None, mode: str = "ephemeral") -> None:
        self.roles, self.image, self.mode = roles, image, mode
        self.egress_allowlist: list[str] | None = None
        self.extra_hosts: list[str] = []


class _Config:
    def __init__(self, endpoints: list[_Endpoint], transport: str = "subprocess") -> None:
        self.performer_endpoints = endpoints
        self.agent_transport = transport


def test_the_image_is_the_roles_performer_image_unless_set() -> None:
    qare = QAreProviderConfig(kind="qare", version="2026.11.44")
    config = _Config([_Endpoint(["implementer"], "impl:1"), _Endpoint(["qa"], "qa:1"), _Endpoint(["qa"], "x", "subprocess")])
    jobs = stage_jobs_for("qare", qare, config, role="qa")
    assert isinstance(jobs, DockerStageJobs)
    assert jobs.image == "qa:1"
    fallback = stage_jobs_for("qare", qare, _Config([_Endpoint(["implementer"], "impl:1")]), role="qa")
    assert isinstance(fallback, DockerStageJobs)
    assert fallback.image == "impl:1"
    own = stage_jobs_for("qare", qare.model_copy(update={"image": "mine:2"}), config, role="qa")
    assert isinstance(own, DockerStageJobs)
    assert own.image == "mine:2"


def test_without_an_image_or_docker_runs_say_why() -> None:
    qare = QAreProviderConfig(kind="qare", version="2026.11.44")
    none = stage_jobs_for("qare", qare, _Config([]), role="qa")
    assert isinstance(none, UnavailableStageJobs)
    assert "stage_providers.qare.image" in none.reason
    k8s = stage_jobs_for("qare", qare, _Config([_Endpoint(["qa"], "qa:1")], "kubernetes"), role="qa")
    assert isinstance(k8s, UnavailableStageJobs)
    assert "Kubernetes" in k8s.reason


@pytest.mark.asyncio
async def test_health_without_a_performer_says_why(tmp_path: Path) -> None:
    ok, detail = await QAreProvider(
        "qare", QAreProviderConfig(kind="qare", version="1"), work_dir=tmp_path,
        jobs=UnavailableStageJobs("no performer image"),
    ).health()
    assert not ok
    assert detail == "no performer image"


# -- both sides of the job directory agree ---------------------------------------------------


def test_qare_job_files_and_outcomes_agree_with_the_performer() -> None:
    from performer.stage_jobs import qare as job

    for name in ("REQUEST_FILE", "STATUS_FILE", "LOG_FILE", "PLAN_FILE", "CRITERIA_FILE", "DIFF_FILE",
                 "EVIDENCE_DIR", "RAN", "UNAVAILABLE", "MISCONFIGURED", "NO_PROFILE", "NO_CRITERIA",
                 "ERROR", "TIMEOUT"):
        assert getattr(qare_runner, name) == getattr(job, name), name
    assert qare_runner.LOG_FILE == stage_jobs.JOB_LOG


def test_progettare_job_files_agree_with_the_performer() -> None:
    from performer.stage_jobs import progettare as job

    for name in ("REQUEST_FILE", "RESULT_FILE", "RUNS_DIR"):
        assert getattr(progettare_runner, name) == getattr(job, name), name
    assert progettare_runner.LOG_FILE == stage_jobs.JOB_LOG


def test_the_job_kinds_and_credential_agree_with_the_performer() -> None:
    import performer.stage_jobs as performer_jobs
    from performer.stage_jobs.__main__ import KINDS

    assert CREDENTIAL_ENV == performer_jobs.CREDENTIAL_ENV
    assert {qare_runner.KIND, progettare_runner.KIND} == set(KINDS)
    assert performer_jobs.__name__ == stage_jobs.STAGE_JOB_MODULE


def test_the_job_command_runs_the_performer_module(tmp_path: Path) -> None:
    command = stage_jobs.job_command("qare", tmp_path / "a dir")
    assert command[:2] == ["sh", "-c"]
    assert f"exec python -m performer.stage_jobs qare '{tmp_path / 'a dir'}'" in command[2]
    assert command[2].endswith(f">>'{tmp_path / 'a dir'}'/runner.log 2>&1")


def test_stage_job_containers_are_not_swept_as_orphaned_performers() -> None:
    assert not STAGE_JOB_LABEL.startswith("coordinare.performer.id")
    assert os.path.basename(stage_jobs.__file__) == "stage_jobs.py"


@pytest.mark.asyncio
async def test_a_docker_that_cannot_run_the_image_is_retried_not_a_wrong_image(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    from coordinare.transport.base import TransportError

    script = Path(docker["bin"])
    script.write_text(script.read_text() + "\nif args[:1] == ['run'] and args[1:2] != ['-d']:\n    sys.exit(125)\n")
    with pytest.raises(TransportError, match="docker could not run"):
        await DockerStageJobs(IMAGE, docker=docker["bin"]).probe(["qare", "--version"])


@pytest.mark.asyncio
async def test_without_a_performer_progettare_is_retried_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinare.transport.base import TransportError

    monkeypatch.setenv("PROGETTARE_423_TOKEN", TOKEN)
    config = ProgettareProviderConfig(
        kind="progettare", version="2026.11.2", runs_dir=str(tmp_path / "runs"), gh_token_env="PROGETTARE_423_TOKEN",
    )
    provider = ProgettareProvider("progettare", config, jobs=UnavailableStageJobs("no performer image here"))
    with pytest.raises(TransportError, match="no performer image here"):
        await provider.submit(_work_item(), _work_item().request_id(1))
    ok, detail = await provider.health()
    assert not ok
    assert detail == "no performer image here"


class _StartFailsButRuns:
    """A start that raised although its container may run (a Docker that timed out)."""

    def __init__(self, after: RunState | None) -> None:
        self.after = after
        self.started = False
        self.stopped: list[Path] = []

    async def start(self, launch: StageJobLaunch) -> str:
        from coordinare.transport.base import TransportError

        self.started = True
        raise TransportError("docker run did not answer within 60 seconds")

    async def state(self, job_dir: Path) -> RunState:
        from coordinare.transport.base import TransportError

        if not self.started:
            return RunState.GONE  # nothing running before the start
        if self.after is None:
            raise TransportError("docker container inspect did not answer")
        return self.after

    async def stop(self, job_dir: Path) -> None:
        self.stopped.append(job_dir)

    async def probe(self, argv: Any, **kw: Any) -> tuple[int | None, str, str]:
        return 0, "2026.11.2\n", ""


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [RunState.RUNNING, None])
async def test_a_qare_start_that_may_have_run_keeps_its_attempt(tmp_path: Path, after: RunState | None) -> None:
    from coordinare.transport.base import TransportError

    jobs = _StartFailsButRuns(after)
    provider = QAreProvider("qare", QAreProviderConfig(kind="qare", version="2026.11.44"),
                            work_dir=tmp_path / "qare", github=_github(), jobs=jobs)
    request = _qa_request()
    with pytest.raises(TransportError):
        await provider.submit(request, request.request_id(1))
    job_dir = tmp_path / "qare" / request.request_id(1)
    assert jobs.stopped == [job_dir]
    # Kept, recorded as launched: the next poll asks Docker, the directory is never reused.
    assert (job_dir / qare_runner.REQUEST_FILE).exists()
    assert (job_dir / qare_runner.LAUNCH_FILE).exists()


@pytest.mark.asyncio
async def test_a_qare_start_whose_container_is_confirmed_gone_leaves_no_job(tmp_path: Path) -> None:
    from coordinare.transport.base import TransportError

    provider = QAreProvider("qare", QAreProviderConfig(kind="qare", version="2026.11.44"),
                            work_dir=tmp_path / "qare", github=_github(), jobs=_StartFailsButRuns(RunState.GONE))
    request = _qa_request()
    with pytest.raises(TransportError):
        await provider.submit(request, request.request_id(1))
    assert not (tmp_path / "qare" / request.request_id(1)).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("after", "kept"), [(RunState.RUNNING, True), (None, True), (RunState.GONE, False)])
async def test_a_progettare_start_keeps_its_request_until_the_container_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: RunState | None, kept: bool,
) -> None:
    from coordinare.transport.base import TransportError

    monkeypatch.setenv("PROGETTARE_423_TOKEN", TOKEN)
    config = ProgettareProviderConfig(
        kind="progettare", version="2026.11.2", runs_dir=str(tmp_path / "runs"), gh_token_env="PROGETTARE_423_TOKEN",
    )
    provider = ProgettareProvider("progettare", config, jobs=_StartFailsButRuns(after))
    request = _work_item()
    with pytest.raises(TransportError):
        await provider.submit(request, request.request_id(1))
    assert (tmp_path / "runs" / request.request_id(1) / progettare_runner.REQUEST_FILE).exists() is kept


@pytest.mark.asyncio
async def test_a_qare_failure_before_any_start_leaves_no_job(tmp_path: Path) -> None:
    from coordinare.transport.base import TransportError

    async def no_token(repository: str) -> str:
        raise RuntimeError("no installation")

    jobs = _StartFailsButRuns(None)  # Docker cannot answer either
    provider = QAreProvider("qare", QAreProviderConfig(kind="qare", version="2026.11.44"), work_dir=tmp_path / "qare",
                            github=GitHubAccess(no_token, lambda r: "https://api.github.com"), jobs=jobs)
    request = _qa_request()
    with pytest.raises(TransportError, match="GitHub token"):
        await provider.submit(request, request.request_id(1))
    assert not (tmp_path / "qare" / request.request_id(1)).exists()
    assert jobs.stopped == []


@pytest.mark.asyncio
async def test_a_kept_attempt_whose_container_is_gone_says_its_start_was_unconfirmed(tmp_path: Path) -> None:
    jobs = _StartFailsButRuns(None)
    provider = QAreProvider("qare", QAreProviderConfig(kind="qare", version="2026.11.44"),
                            work_dir=tmp_path / "qare", github=_github(), jobs=jobs)
    request = _qa_request()
    from coordinare.transport.base import TransportError

    with pytest.raises(TransportError):
        await provider.submit(request, request.request_id(1))
    jobs.after = RunState.GONE
    job = await provider.poll(request.request_id(1))
    assert job.state is JobState.RETRYABLE
    assert "could not be confirmed" in job.reason


@pytest.mark.asyncio
async def test_a_progettare_run_past_its_timeout_is_stopped_not_waited_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = _StartFailsButRuns(RunState.RUNNING)
    jobs.started = True
    config = ProgettareProviderConfig(
        kind="progettare", version="2026.11.2", runs_dir=str(tmp_path / "runs"), gh_token_env="PROGETTARE_423_TOKEN",
        timeout_seconds=60,
    )
    request = _work_item()
    directory = tmp_path / "runs" / request.request_id(1)
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(json.dumps({
        "pin": request.pin, "timeout_seconds": 60, "created_at": "2026-01-01T00:00:00+00:00",
    }))
    job = await ProgettareProvider("progettare", config, jobs=jobs).poll(request.request_id(1))
    assert job.state is JobState.RETRYABLE
    assert "passed its timeout" in job.reason
    assert jobs.stopped == [directory]
