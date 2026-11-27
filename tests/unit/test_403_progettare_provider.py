"""403: the Progettare provider and its detached runner, against a fake Progettare CLI.

The runner is a real subprocess, the clone is a real ``git clone`` of a local repository,
and the CLI is ``tests/unit/fake_progettare.py``. Nothing reaches the network.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from coordinare.services.progettare import ProgettareProvider
from coordinare.services.stage_contract import (
    JobState,
    StageJob,
    StageVerdict,
    WorkItemRequest,
    WorkItemSnapshot,
    WorkItemStageService,
    work_item_digest,
    work_item_pin,
)
from coordinare.stage_provider_config import ProgettareProviderConfig
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError

FAKE = Path(__file__).with_name("fake_progettare.py")
TITLE = "Export widgets as CSV"
BODY = "## Acceptance\n- [ ] a CSV download exists\n"
PASS_ENV = [
    "FAKE_PROGETTARE_MODE", "FAKE_PROGETTARE_SLEEP", "FAKE_PROGETTARE_TITLE", "FAKE_PROGETTARE_BODY",
    "FAKE_PROGETTARE_ARTIFACT_VERSION", "FAKE_PROGETTARE_DOCUMENTER", "FAKE_PROGETTARE_VERSION", "OPENAI_API_KEY",
]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """The repository Progettare plans, with its own progettare.yaml."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "progettare.yaml").write_text("survey: {}\n")
    (repo / "app.py").write_text("print('widgets')\n")
    _git("add", ".", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def command(tmp_path: Path) -> Path:
    wrapper = tmp_path / "bin" / "progettare"
    wrapper.parent.mkdir()
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROGETTARE_TEST_GH_TOKEN", "ghs_test_token")
    monkeypatch.setenv("FAKE_PROGETTARE_TITLE", TITLE)
    monkeypatch.setenv("FAKE_PROGETTARE_BODY", BODY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("NOT_FOR_PROGETTARE", "secret")
    monkeypatch.setenv("FAKE_PROGETTARE_MODE", "complete")
    monkeypatch.setenv("FAKE_PROGETTARE_SLEEP", "30")
    monkeypatch.setenv("FAKE_PROGETTARE_ARTIFACT_VERSION", "1")
    monkeypatch.setenv("FAKE_PROGETTARE_DOCUMENTER", "1")
    monkeypatch.setenv("FAKE_PROGETTARE_VERSION", "2026.11.3")


def _config(tmp_path: Path, cli: Path, **kw: Any) -> ProgettareProviderConfig:
    base: dict[str, Any] = {
        "kind": "progettare", "command": str(cli), "version": "2026.11.3",
        "runs_dir": str(tmp_path / "runs"), "gh_token_env": "PROGETTARE_TEST_GH_TOKEN",
        "pass_env": PASS_ENV, "timeout_seconds": 60,
    }
    base.update(kw)
    return ProgettareProviderConfig(**base)


def _provider(tmp_path: Path, cli: Path, origin: Path, **kw: Any) -> ProgettareProvider:
    return ProgettareProvider("progettare", _config(tmp_path, cli, **kw), clone_url=lambda _r: f"file://{origin}")


def _request(**kw: Any) -> WorkItemRequest:
    base: dict[str, Any] = {
        "card_id": "PVTI_card1", "stage": "architecting", "repository": "acme/widgets",
        "number": 12, "node_id": "I_node12", "pin": work_item_pin(work_item_digest(TITLE, BODY), []),
    }
    base.update(kw)
    return WorkItemRequest(**base)


async def _settled(provider: ProgettareProvider, ref: str, timeout: float = 30) -> StageJob:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        job = await provider.poll(ref)
        if job.state is not JobState.PENDING or loop.time() > deadline:
            return job
        await asyncio.sleep(0.05)


def _invocations(tmp_path: Path, ref: str) -> list[dict[str, Any]]:
    log = tmp_path / "runs" / ref / "fake-invocations.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


# -- a plan ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_complete_run_is_a_plan_with_progettares_briefs(tmp_path: Path, command: Path, origin: Path) -> None:
    provider = _provider(tmp_path, command, origin)
    request = _request()
    ref = request.request_id(1)
    started = await provider.submit(request, ref)
    assert started.state is JobState.PENDING
    job = await _settled(provider, ref)
    assert job.state is JobState.DONE, job.reason
    outcome = job.outcome
    assert outcome is not None
    assert outcome.verdict is StageVerdict.PASSED
    assert outcome.reviewed_head == request.pin
    assert outcome.input_digest == work_item_digest(TITLE, BODY)
    blueprint = outcome.blueprint or {}
    assert blueprint["summary"] == TITLE
    assert blueprint["milestones"] == [{
        "goal": "CSV export", "scope": ["add export.py", "add a download route"],
        "done_when": "every change in scope is in place",
    }]
    assert [c["action"] for c in blueprint["criteria"]] == ["GET /widgets.csv returns every widget"]
    assert [d["say"] for d in blueprint["docs"]] == ["the CSV export in the user guide"]
    assert blueprint["size"] == "small"
    assert blueprint["source"]["progettare"] == "2026.11.3"
    assert Path(outcome.record_ref).name == "20261127T000000Z-acme-widgets-12"

    (invocation,) = _invocations(tmp_path, ref)
    assert invocation["argv"][:3] == ["blueprint", "--issue", "acme/widgets#12"]
    assert invocation["gh_token"] == "ghs_test_token"
    assert "progettare.yaml" in invocation["checkout"]
    assert invocation["config_exists"] is True
    assert "OPENAI_API_KEY" in invocation["env"]
    assert "NOT_FOR_PROGETTARE" not in invocation["env"]
    # The clone is removed once the run is over; the run's record stays.
    assert not (tmp_path / "runs" / ref / "checkout").exists()


@pytest.mark.asyncio
async def test_a_finished_request_is_found_again_never_run_twice(tmp_path: Path, command: Path, origin: Path) -> None:
    provider = _provider(tmp_path, command, origin)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    await _settled(provider, ref)
    restarted = _provider(tmp_path, command, origin)
    again = await restarted.submit(_request(), ref)
    assert again.state is JobState.DONE
    assert len(_invocations(tmp_path, ref)) == 1


@pytest.mark.asyncio
async def test_a_run_in_flight_survives_a_restart_and_is_not_started_twice(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROGETTARE_MODE", "sleep")
    monkeypatch.setenv("FAKE_PROGETTARE_SLEEP", "1.5")
    provider = _provider(tmp_path, command, origin)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while not _invocations(tmp_path, ref) and loop.time() < deadline:
        await asyncio.sleep(0.05)
    restarted = _provider(tmp_path, command, origin)  # a new daemon: no memory of the child
    assert (await restarted.submit(_request(), ref)).state is JobState.PENDING
    assert (await restarted.poll(ref)).state is JobState.PENDING
    job = await _settled(restarted, ref)
    assert job.state is JobState.DONE
    assert len(_invocations(tmp_path, ref)) == 1


# -- the other endings -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_carries_progettares_questions(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROGETTARE_MODE", "blocked")
    provider = _provider(tmp_path, command, origin)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    job = await _settled(provider, ref)
    assert job.state is JobState.DONE
    assert job.outcome is not None
    assert job.outcome.verdict is StageVerdict.NEEDS_HUMAN
    assert job.outcome.questions[0] == "What are the acceptance criteria for this issue?"
    assert "editing the issue" in job.outcome.questions[-1]


@pytest.mark.parametrize(("mode", "reason"), [
    ("failed", "progettare failed at survey: nare exited 1"),
    ("crash", "something broke before the run"),
])
@pytest.mark.asyncio
async def test_a_run_that_ends_without_a_plan_is_retryable(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch, mode: str, reason: str,
) -> None:
    monkeypatch.setenv("FAKE_PROGETTARE_MODE", mode)
    provider = _provider(tmp_path, command, origin)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    job = await _settled(provider, ref)
    assert job.state is JobState.RETRYABLE
    assert reason in job.reason


@pytest.mark.asyncio
async def test_a_run_that_is_too_slow_is_stopped_and_retryable(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROGETTARE_MODE", "sleep")
    provider = _provider(tmp_path, command, origin, timeout_seconds=1)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    job = await _settled(provider, ref)
    assert job.state is JobState.RETRYABLE
    assert "longer than 1s" in job.reason


@pytest.mark.asyncio
async def test_a_run_whose_runner_died_is_retryable(tmp_path: Path, command: Path, origin: Path) -> None:
    ref = _request().request_id(1)
    directory = tmp_path / "runs" / ref
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(json.dumps({"pin": _request().pin}))
    (directory / "lock").write_text("")
    job = await _provider(tmp_path, command, origin).poll(ref)
    assert job.state is JobState.RETRYABLE
    assert "recorded no run" in job.reason


@pytest.mark.asyncio
async def test_a_clone_that_fails_is_retryable(tmp_path: Path, command: Path) -> None:
    provider = _provider(tmp_path, command, tmp_path / "missing")
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    job = await _settled(provider, ref)
    assert job.state is JobState.RETRYABLE
    assert job.reason.startswith("clone failed")


@pytest.mark.asyncio
async def test_a_record_of_another_contract_version_is_refused(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROGETTARE_ARTIFACT_VERSION", "2")
    provider = _provider(tmp_path, command, origin)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    job = await _settled(provider, ref)
    assert job.state is JobState.NEEDS_HUMAN
    assert "contract version 2" in job.reason
    assert "speaks Progettare contract version 1" in job.reason


@pytest.mark.asyncio
async def test_a_repository_without_progettare_config_blocks(tmp_path: Path, command: Path, origin: Path) -> None:
    (origin / "progettare.yaml").unlink()
    _git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qam", "drop config", cwd=origin)
    provider = _provider(tmp_path, command, origin)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    job = await _settled(provider, ref)
    assert job.state is JobState.NEEDS_HUMAN
    assert "no Progettare config" in job.reason


@pytest.mark.asyncio
async def test_a_configured_progettare_yaml_is_used(tmp_path: Path, command: Path, origin: Path) -> None:
    (origin / "progettare.yaml").unlink()
    _git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qam", "drop config", cwd=origin)
    config = tmp_path / "progettare.yaml"
    config.write_text("survey: {}\n")
    provider = _provider(tmp_path, command, origin, config_path=str(config))
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    assert (await _settled(provider, ref)).state is JobState.DONE
    assert _invocations(tmp_path, ref)[0]["argv"][5:7] == ["--config", str(config)]


@pytest.mark.asyncio
async def test_no_documenter_brief_means_no_docs(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROGETTARE_DOCUMENTER", "0")
    provider = _provider(tmp_path, command, origin)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    job = await _settled(provider, ref)
    assert job.outcome is not None
    assert (job.outcome.blueprint or {})["docs"] == []


# -- refusals before a run starts --------------------------------------------------------


@pytest.mark.asyncio
async def test_without_its_token_nothing_starts(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROGETTARE_TEST_GH_TOKEN")
    provider = _provider(tmp_path, command, origin)
    with pytest.raises(PerformerAuthError, match="PROGETTARE_TEST_GH_TOKEN is not set"):
        await provider.submit(_request(), _request().request_id(1))
    assert not (tmp_path / "runs").exists()


@pytest.mark.asyncio
async def test_another_installed_version_is_refused(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_PROGETTARE_VERSION", "2026.12.0")
    provider = _provider(tmp_path, command, origin)
    with pytest.raises(PerformerAuthError, match=r"reports 2026\.12\.0, but version 2026\.11\.3 is pinned"):
        await provider.submit(_request(), _request().request_id(1))


@pytest.mark.asyncio
async def test_a_pull_request_is_not_progettares_to_plan(tmp_path: Path, command: Path, origin: Path) -> None:
    from coordinare.services.stage_contract import StageRequest

    pr = StageRequest(card_id="c", stage="reviewing", repository="acme/widgets", pr_number=1, pr_node_id="P", head_sha="a" * 40)
    with pytest.raises(TransportError, match="work items"):
        await _provider(tmp_path, command, origin).submit(pr, pr.request_id(1))


@pytest.mark.asyncio
async def test_an_unknown_request_is_a_retryable_error(tmp_path: Path, command: Path, origin: Path) -> None:
    with pytest.raises(TransportError, match="no run"):
        await _provider(tmp_path, command, origin).poll(_request().request_id(1))
    with pytest.raises(TransportError, match="not a request id"):
        await _provider(tmp_path, command, origin).poll("../escape")


# -- health ------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def gh_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs need gh; a stub keeps the health checks independent of the test host."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "gh").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.mark.asyncio
async def test_health_passes_when_installed_at_the_pinned_version(tmp_path: Path, command: Path, origin: Path) -> None:
    ok, detail = await _provider(tmp_path, command, origin).health()
    assert ok, detail
    assert "progettare 2026.11.3" in detail


@pytest.mark.parametrize(("change", "expected"), [
    ({"version": "2026.10.0"}, "but 2026.10.0 is pinned"),
    ({"command": "/nonexistent/progettare"}, "not installed"),
    ({"pass_env": ["UNSET_MODEL_KEY"]}, "UNSET_MODEL_KEY"),
    ({"config_path": "/nonexistent/progettare.yaml"}, "does not exist"),
])
@pytest.mark.asyncio
async def test_health_names_what_is_wrong(
    tmp_path: Path, command: Path, origin: Path, change: dict[str, Any], expected: str,
) -> None:
    os.environ.pop("UNSET_MODEL_KEY", None)
    ok, detail = await _provider(tmp_path, command, origin, **change).health()
    assert not ok
    assert expected in detail


# -- end to end through the stage service ------------------------------------------------


@pytest.mark.asyncio
async def test_the_architecting_stage_reaches_a_plan_through_the_contract(
    tmp_path: Path, command: Path, origin: Path,
) -> None:
    async def read(repository: str, node: str) -> WorkItemSnapshot:
        return WorkItemSnapshot(title=TITLE, body=BODY, state="OPEN")

    service = WorkItemStageService(_provider(tmp_path, command, origin), stage="architecting", work_item_reader=read)
    card = {"id": "PVTI_card1", "issue_url": "https://github.com/acme/widgets/issues/12", "issue_id": "I_node12"}
    session = (await service.dispatch_card(card))["session_id"]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    status = await service.check_status(session)
    while status["status"] == "working" and loop.time() < deadline:
        await asyncio.sleep(0.05)
        status = await service.check_status(session)
    assert status["status"] == "plan_committed"
    assert status["report"]["blueprint"]["milestones"][0]["goal"] == "CSV export"


@pytest.mark.asyncio
async def test_a_malformed_documenter_brief_is_refused_not_raised(
    tmp_path: Path, command: Path, origin: Path,
) -> None:
    provider = _provider(tmp_path, command, origin)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    assert (await _settled(provider, ref)).state is JobState.DONE
    (brief,) = (tmp_path / "runs" / ref / "runs").glob("*/briefs/documenter.json")
    brief.write_text(json.dumps({"artifact_version": 1, "artifact": "documenter_brief", "brief": None}))
    job = await provider.poll(ref)
    assert job.state is JobState.NEEDS_HUMAN
    assert "no documenter brief" in job.reason


@pytest.mark.asyncio
async def test_an_unusable_runs_directory_is_a_retryable_error(tmp_path: Path, command: Path, origin: Path) -> None:
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    provider = _provider(tmp_path, command, origin, runs_dir=str(blocked / "runs"))
    with pytest.raises(TransportError):
        await provider.submit(_request(), _request().request_id(1))


@pytest.mark.asyncio
async def test_a_runner_that_died_says_why_from_its_log(tmp_path: Path, command: Path, origin: Path) -> None:
    ref = _request().request_id(1)
    directory = tmp_path / "runs" / ref
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(json.dumps({"pin": _request().pin}))
    (directory / "runner.log").write_text("ModuleNotFoundError: No module named 'coordinare'\n")
    job = await _provider(tmp_path, command, origin).poll(ref)
    assert job.state is JobState.RETRYABLE
    assert "the runner's log ends: ModuleNotFoundError" in job.reason


@pytest.mark.asyncio
async def test_the_version_is_checked_for_every_run(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(tmp_path, command, origin)
    await provider.submit(_request(), _request().request_id(1))
    monkeypatch.setenv("FAKE_PROGETTARE_VERSION", "2026.12.0")
    with pytest.raises(PerformerAuthError, match="is pinned"):
        await provider.submit(_request(), _request().request_id(2))


def test_the_run_gets_the_proxy_and_import_path_it_needs(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("PYTHONPATH", "/opt/coordinare/src")
    env = _provider(tmp_path, command, origin)._child_env("t", "github.com")
    assert env["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert env["PYTHONPATH"] == "/opt/coordinare/src"
    assert "NOT_FOR_PROGETTARE" not in env


@pytest.mark.asyncio
async def test_a_version_that_cannot_be_read_in_time_is_retryable(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coordinare.services.progettare as progettare

    monkeypatch.setattr(progettare, "_VERSION_TIMEOUT_SECONDS", 0.2)
    slow = tmp_path / "bin" / "slow-progettare"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    provider = _provider(tmp_path, slow, origin)
    with pytest.raises(TransportError, match="could not read"):
        await provider.submit(_request(), _request().request_id(1))
    missing = _provider(tmp_path, tmp_path / "bin" / "absent", origin)
    with pytest.raises(PerformerAuthError, match="could not be started"):
        await missing.submit(_request(), _request().request_id(1))


@pytest.mark.asyncio
async def test_progettare_does_not_inherit_coordinares_python(
    tmp_path: Path, command: Path, origin: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/opt/coordinare/.venv")
    provider = _provider(tmp_path, command, origin)
    ref = _request().request_id(1)
    await provider.submit(_request(), ref)
    assert (await _settled(provider, ref)).state is JobState.DONE
    (invocation,) = _invocations(tmp_path, ref)
    assert "VIRTUAL_ENV" not in invocation["env"]


def test_coordinares_virtualenv_is_taken_off_the_path() -> None:
    from coordinare.services.progettare_runner import without_coordinare_python

    env = {"PATH": "/opt/coordinare/.venv/bin:/usr/local/bin:/usr/bin", "VIRTUAL_ENV": "/opt/coordinare/.venv",
           "PYTHONPATH": "/opt/coordinare/src", "HOME": "/srv/coordinare"}
    cleaned = without_coordinare_python(env)
    assert cleaned["PATH"].split(":")[-2:] == ["/usr/local/bin", "/usr/bin"]
    assert "/opt/coordinare/.venv/bin" not in cleaned["PATH"]
    assert "VIRTUAL_ENV" not in cleaned
    assert "PYTHONPATH" not in cleaned
    assert cleaned["HOME"] == "/srv/coordinare"


def test_the_daemons_own_virtualenv_leaves_the_path_without_virtual_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import coordinare.services.progettare_runner as runner

    monkeypatch.setattr(runner.sys, "prefix", "/opt/coordinare/.venv")
    monkeypatch.setattr(runner.sys, "base_prefix", "/usr")
    cleaned = runner.without_coordinare_python({"PATH": "/opt/coordinare/.venv/bin:/usr/bin"})
    assert cleaned["PATH"] == "/usr/bin"
    monkeypatch.setattr(runner.sys, "prefix", "/usr")
    assert runner.without_coordinare_python({"PATH": "/usr/bin:/bin"})["PATH"] == "/usr/bin:/bin"
