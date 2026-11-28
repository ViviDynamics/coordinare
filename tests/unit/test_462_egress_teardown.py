"""462: a stage job's egress proxy and network go once coordinare reads how the job ended.

A job behind the egress proxy (#447, #449) ends by writing its status (QAre) or result
(Progettare). coordinare reads that file and, before this, never asked Docker about the job
again, so the proxy stayed until its own ``--follow`` saw the job gone (up to its ten minute
grace for a job that ended within seconds) and the network stayed until a later start's sweep.
Now reading the job's last word also removes its proxy and network once its container is gone.
A container that is still finishing keeps them: the proxy follows it out, and the sweep stays
the backstop.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from coordinare.services import progettare_runner, qare_runner
from coordinare.services.progettare import ProgettareProvider
from coordinare.services.qare import QAreProvider
from coordinare.services.stage_contract import JobState
from coordinare.services.stage_jobs import (
    EGRESS_SUFFIX,
    DockerStageJobs,
    RunState,
    StageJobLaunch,
    UnavailableStageJobs,
    job_name,
)
from coordinare.stage_provider_config import ProgettareProviderConfig, QAreProviderConfig
from tests.unit import test_447_sandbox_egress as egress
from tests.unit.test_423_providers_in_performer import _github, _work_item
from tests.unit.test_435_qare_sandbox import _jobs

#: 447's fake ``docker``, a fixture.
docker = egress.docker
ALLOW = egress.ALLOW


def _egress_removal(name: str) -> list[list[str]]:
    return [["rm", "-f", name + EGRESS_SUFFIX], ["network", "rm", name + EGRESS_SUFFIX]]


# -- DockerStageJobs.ended ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox", [None, "sysbox"], ids=["no-sandbox", "sandbox"])
async def test_a_reported_job_that_is_gone_takes_its_proxy_and_network_with_it(
    tmp_path: Path, docker: dict[str, Any], sandbox: str | None,
) -> None:
    jobs = _jobs(docker["bin"], sandbox, egress=ALLOW)
    docker["set"](job="missing")
    await jobs.ended(tmp_path / "job")
    name = job_name(tmp_path / "job")
    calls = docker["calls"]()
    assert calls[0][:2] == ["container", "inspect"]
    assert calls[1:] == _egress_removal(name)


@pytest.mark.asyncio
async def test_a_reported_job_still_exiting_is_released_once_it_is_gone(tmp_path: Path, docker: dict[str, Any]) -> None:
    jobs = _jobs(docker["bin"], None, egress=ALLOW)
    jobs.ended_poll_seconds = 0.05
    docker["set"](job="running")

    async def exits() -> None:
        await asyncio.sleep(0.3)
        docker["set"](job="missing")

    exiting = asyncio.create_task(exits())
    await jobs.ended(tmp_path / "job")
    await exiting
    assert docker["calls"]()[-2:] == _egress_removal(job_name(tmp_path / "job"))


@pytest.mark.asyncio
async def test_a_reported_job_that_stays_up_keeps_its_proxy_and_network(tmp_path: Path, docker: dict[str, Any]) -> None:
    """Its proxy follows it out once it is gone; the sweep is the backstop for the network."""
    jobs = _jobs(docker["bin"], None, egress=ALLOW)
    jobs.ended_settle_seconds, jobs.ended_poll_seconds = 0.2, 0.05
    docker["set"](job="running")
    await jobs.ended(tmp_path / "job")
    assert docker["calls"](), "it asked"
    assert not any(a[:1] in (["network"], ["rm"]) for a in docker["calls"]())


@pytest.mark.asyncio
async def test_a_job_is_released_once_however_often_it_is_read(tmp_path: Path, docker: dict[str, Any]) -> None:
    jobs = _jobs(docker["bin"], None, egress=ALLOW)
    docker["set"](job="missing")
    await jobs.ended(tmp_path / "job")
    docker["clear"]()
    await jobs.ended(tmp_path / "job")
    assert docker["calls"]() == []


@pytest.mark.asyncio
async def test_a_docker_that_hangs_holds_up_the_verdict_only_briefly(tmp_path: Path) -> None:
    hung = tmp_path / "docker"
    hung.write_text("#!/bin/sh\nexec sleep 30\n")
    hung.chmod(0o755)
    jobs = DockerStageJobs("img", docker=str(hung), egress_allowlist=ALLOW)
    jobs.ended_wait_seconds = 0.2
    loop = asyncio.get_running_loop()
    began = loop.time()
    await jobs.ended(tmp_path / "job")
    assert loop.time() - began < 3
    for task in list(jobs._releasing.values()):  # the release goes on; end it with the test
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_job_without_a_proxy_costs_no_docker_call_when_it_reports(tmp_path: Path, docker: dict[str, Any]) -> None:
    for jobs in (_jobs(docker["bin"], None), _jobs(docker["bin"], None, egress=ALLOW, egress_mode="iptables")):
        await jobs.ended(tmp_path / "job")
    assert docker["calls"]() == []


@pytest.mark.asyncio
async def test_a_docker_that_cannot_answer_never_holds_up_the_verdict(tmp_path: Path) -> None:
    jobs = DockerStageJobs("img", docker=str(tmp_path / "no-such-docker"), egress_allowlist=ALLOW)
    await jobs.ended(tmp_path / "job")  # no exception: best effort
    await UnavailableStageJobs("no image").ended(tmp_path / "job")


# -- the providers call it when they read the job's last word ----------------------------------


class _Recording:
    """StageJobs that records which jobs were told they ended."""

    def __init__(self, state: RunState = RunState.GONE) -> None:
        self.run_state = state
        self.ended_dirs: list[Path] = []

    async def start(self, launch: StageJobLaunch) -> str:
        raise AssertionError("not started here")

    async def state(self, job_dir: Path) -> RunState:
        return self.run_state

    async def stop(self, job_dir: Path) -> None:
        raise AssertionError("a reported job is not stopped")

    async def ended(self, job_dir: Path) -> None:
        self.ended_dirs.append(job_dir)

    async def probe(self, argv: Any, **kw: Any) -> tuple[int | None, str, str]:
        raise AssertionError("no probe here")


@pytest.mark.asyncio
async def test_reading_a_qare_status_releases_the_jobs_egress(tmp_path: Path) -> None:
    jobs = _Recording()
    provider = QAreProvider("qare", QAreProviderConfig(kind="qare", version="2026.11.44"),
                            work_dir=tmp_path / "qare", github=_github(), jobs=jobs)
    ref = "coordinare.qa.abc.1"
    job_dir = tmp_path / "qare" / ref
    job_dir.mkdir(parents=True)
    (job_dir / qare_runner.STATUS_FILE).write_text(json.dumps({
        "outcome": qare_runner.NO_PROFILE, "step": "profile", "reason": "no QA profile on either side",
    }))
    job = await provider.poll(ref)
    assert job.state is JobState.NEEDS_HUMAN
    assert jobs.ended_dirs == [job_dir]


@pytest.mark.asyncio
async def test_a_running_qare_job_is_not_told_it_ended(tmp_path: Path) -> None:
    jobs = _Recording(RunState.RUNNING)
    provider = QAreProvider("qare", QAreProviderConfig(kind="qare", version="2026.11.44"),
                            work_dir=tmp_path / "qare", github=_github(), jobs=jobs)
    ref = "coordinare.qa.abc.1"
    job_dir = tmp_path / "qare" / ref
    job_dir.mkdir(parents=True)
    (job_dir / qare_runner.LAUNCH_FILE).write_text(json.dumps({"job": "x", "started_at": 0}))
    job = await provider.poll(ref)
    assert job.state is JobState.PENDING
    assert jobs.ended_dirs == []


@pytest.mark.asyncio
async def test_reading_a_progettare_result_releases_the_jobs_egress(tmp_path: Path) -> None:
    jobs = _Recording()
    config = ProgettareProviderConfig(
        kind="progettare", version="2026.11.2", runs_dir=str(tmp_path / "runs"), gh_token_env="PROGETTARE_462_TOKEN",
    )
    provider = ProgettareProvider("progettare", config, jobs=jobs)
    ref = _work_item().request_id(1)
    directory = tmp_path / "runs" / ref
    directory.mkdir(parents=True)
    (directory / progettare_runner.REQUEST_FILE).write_text(json.dumps({"request_id": ref, "timeout_seconds": 60}))
    (directory / progettare_runner.RESULT_FILE).write_text(json.dumps(
        {"phase": "runner", "exit_code": None, "detail": "the runner stopped unexpectedly"},
    ))
    await provider.poll(ref)
    assert jobs.ended_dirs == [directory]


@pytest.mark.asyncio
async def test_a_release_that_docker_refused_is_tried_again_on_the_next_read(tmp_path: Path, docker: dict[str, Any]) -> None:
    jobs = _jobs(docker["bin"], None, egress=ALLOW)
    docker["set"](job="missing", network_rm_fails="Error: network is busy")
    await jobs.ended(tmp_path / "job")
    docker["clear"]()
    docker["set"](job="missing")
    await jobs.ended(tmp_path / "job")
    assert docker["calls"]()[-2:] == _egress_removal(job_name(tmp_path / "job"))


@pytest.mark.asyncio
async def test_a_job_whose_egress_state_already_removed_costs_no_more_docker_calls(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    jobs = _jobs(docker["bin"], None, egress=ALLOW)
    docker["set"](job="missing")
    assert await jobs.state(tmp_path / "job") is RunState.GONE  # removes the egress (447)
    docker["clear"]()
    await jobs.ended(tmp_path / "job")
    assert docker["calls"]() == []


@pytest.mark.asyncio
async def test_a_status_written_while_the_job_was_read_still_releases_its_egress(tmp_path: Path) -> None:
    """The second status read in QAre's ``_read`` (written between the first read and Docker's answer)."""
    status = {"outcome": qare_runner.NO_PROFILE, "step": "profile", "reason": "no QA profile on either side"}

    class _ReportsWhileAsked(_Recording):
        async def state(self, job_dir: Path) -> RunState:
            (job_dir / qare_runner.STATUS_FILE).write_text(json.dumps(status))  # it reports just now
            return RunState.GONE

    jobs = _ReportsWhileAsked()
    provider = QAreProvider("qare", QAreProviderConfig(kind="qare", version="2026.11.44"),
                            work_dir=tmp_path / "qare", github=_github(), jobs=jobs)
    ref = "coordinare.qa.abc.1"
    job_dir = tmp_path / "qare" / ref
    job_dir.mkdir(parents=True)
    (job_dir / qare_runner.LAUNCH_FILE).write_text(json.dumps({"job": "x", "started_at": 0}))
    job = await provider.poll(ref)
    assert job.state is JobState.NEEDS_HUMAN
    assert jobs.ended_dirs == [job_dir]


@pytest.mark.asyncio
async def test_a_removal_refused_in_state_is_still_done_when_the_job_reports(tmp_path: Path, docker: dict[str, Any]) -> None:
    jobs = _jobs(docker["bin"], None, egress=ALLOW)
    docker["set"](job="missing", network_rm_fails="Error: network is busy")
    assert await jobs.state(tmp_path / "job") is RunState.GONE
    docker["clear"]()
    docker["set"](job="missing")
    await jobs.ended(tmp_path / "job")
    assert docker["calls"]()[-2:] == _egress_removal(job_name(tmp_path / "job"))


@pytest.mark.asyncio
async def test_a_removal_docker_refuses_once_is_retried_within_the_same_release(
    tmp_path: Path, docker: dict[str, Any],
) -> None:
    """A reported job is usually not read again, so the release itself tries again."""
    jobs = _jobs(docker["bin"], None, egress=ALLOW)
    jobs.ended_poll_seconds = 0.5  # the first refusal is in well before the second try
    docker["set"](job="missing", network_rm_fails="Error: network has active endpoints")
    network_rm = ["network", "rm", job_name(tmp_path / "job") + EGRESS_SUFFIX]

    async def frees() -> None:
        while network_rm not in docker["calls"]():  # Docker is refusing once
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)  # that call has read its state and failed
        docker["set"](job="missing")

    freeing = asyncio.create_task(frees())
    await jobs.ended(tmp_path / "job")
    await freeing
    calls = docker["calls"]()
    assert calls.count(["network", "rm", job_name(tmp_path / "job") + EGRESS_SUFFIX]) >= 2
    assert docker["calls"]()[-2:] == _egress_removal(job_name(tmp_path / "job"))
    docker["clear"]()
    await jobs.ended(tmp_path / "job")
    assert docker["calls"]() == [], "released, so a later read asks nothing"


@pytest.mark.asyncio
async def test_a_new_start_of_the_job_cancels_an_earlier_attempts_release(tmp_path: Path, docker: dict[str, Any]) -> None:
    """A stale release must never take down the egress a new attempt just made."""
    jobs = _jobs(docker["bin"], None, egress=ALLOW)
    jobs.ended_settle_seconds, jobs.ended_poll_seconds, jobs.ended_wait_seconds = 60, 2, 0.05
    job_dir = tmp_path / "job"
    docker["set"](job="running")
    await jobs.ended(job_dir)  # its container still up: the release waits in the background
    stale = jobs._releasing[job_name(job_dir)]
    for _ in range(100):  # until it has looked once and gone to sleep
        if any(c[:2] == ["container", "inspect"] for c in docker["calls"]()):
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    assert not stale.done()
    docker["set"](job="missing")  # the container goes, and the job is started again at once
    await jobs.start(StageJobLaunch(kind="qare", ref="r1", provider="qare", job_dir=job_dir))
    assert stale.cancelled()
    assert job_name(job_dir) not in jobs._releasing


@pytest.mark.asyncio
async def test_a_progettare_result_written_while_the_job_was_read_still_releases_its_egress(tmp_path: Path) -> None:
    class _ReportsWhileAsked(_Recording):
        async def state(self, job_dir: Path) -> RunState:
            (job_dir / progettare_runner.RESULT_FILE).write_text(json.dumps(
                {"phase": "runner", "exit_code": None, "detail": "the runner stopped unexpectedly"},
            ))
            return RunState.GONE

    jobs = _ReportsWhileAsked()
    config = ProgettareProviderConfig(
        kind="progettare", version="2026.11.2", runs_dir=str(tmp_path / "runs"), gh_token_env="PROGETTARE_462_TOKEN",
    )
    provider = ProgettareProvider("progettare", config, jobs=jobs)
    ref = _work_item().request_id(1)
    directory = tmp_path / "runs" / ref
    directory.mkdir(parents=True)
    (directory / progettare_runner.REQUEST_FILE).write_text(json.dumps({"request_id": ref, "timeout_seconds": 60}))
    await provider.poll(ref)
    assert jobs.ended_dirs == [directory]


@pytest.mark.parametrize(("command", "err", "gone"), [
    ("rm", "Error response from daemon: No such container: x-egress", True),
    ("network", "Error response from daemon: network x-egress not found", True),
    ("network", "Error: network is busy: has active endpoints", False),
    ("network", "error during connect: dial unix /var/run/docker.sock: connect: no such file or directory", False),
    ("rm", 'context "remote" not found', False),
])
def test_only_dockers_nothing_to_remove_answer_counts_as_gone(command: str, err: str, gone: bool) -> None:
    from coordinare.services.stage_jobs import _already_gone

    assert _already_gone(command, err) is gone
