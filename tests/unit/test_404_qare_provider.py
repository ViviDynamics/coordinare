"""404: the QAre provider: submit, poll, restart, and what a result means for the stage."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from coordinare.services import qare_runner as runner
from coordinare.services.qare import (
    GitHubAccess,
    QAreProvider,
    job_from_status,
    parse_result,
    web_base_for,
)
from coordinare.services.stage_contract import JobState, StageCriterion, StageRequest, StageVerdict
from coordinare.stage_provider_config import QAreProviderConfig
from coordinare.transport.base import TransportError
from tests.unit import fake_qare
from tests.unit.qare_fixtures import CRITERIA, FAKE_QARE, make_remote, request_spec, scenario

TOKEN = "ghs_providertokenvalue00000000000000000"
HEAD = "b" * 40
SPEC = {"request_id": "coordinare.qa.abc.1", "head_sha": HEAD, "criteria": CRITERIA}


def _config(**kw: Any) -> QAreProviderConfig:
    base: dict[str, Any] = {
        "kind": "qare", "version": fake_qare.VERSION, "command": FAKE_QARE,
        "env_passthrough": ["FAKE_QARE_SCENARIO"],
    }
    base.update(kw)
    return QAreProviderConfig.model_validate(base)


def _result(verdict: str = "passed", outcomes: dict[str, str] | None = None, **extra: Any) -> str:
    outcomes = outcomes or {}
    criteria = []
    for c in CRITERIA:
        outcome = outcomes.get(c["id"], "proven")
        entry: dict[str, Any] = {"id": c["id"], "outcome": outcome, "evidence": [f"checks/{c['id']}/1/stdout.txt"]}
        if outcome == "unverified":
            entry = {"id": c["id"], "outcome": outcome, "reason": "stub missing"}
        criteria.append(entry)
    document = {"schemaVersion": "1", "verdict": verdict, "job": {"id": SPEC["request_id"]}, "criteria": criteria}
    document.update(extra)
    return json.dumps(document)


# -- reading a result ----------------------------------------------------------------------


def test_a_pass_maps_every_criterion_with_its_evidence() -> None:
    outcome, problem = parse_result(_result(), SPEC, exit_code=0)
    assert problem == ""
    assert outcome is not None
    assert outcome.verdict is StageVerdict.PASSED
    assert outcome.reviewed_head == HEAD
    assert [c.outcome for c in outcome.criteria] == ["proven", "proven"]
    assert outcome.criteria[0].text == CRITERIA[0]["text"]
    assert outcome.criteria[0].evidence == [f"checks/{CRITERIA[0]['id']}/1/stdout.txt"]


def test_the_schema_version_is_checked_before_anything_else() -> None:
    outcome, problem = parse_result(json.dumps({"schemaVersion": "2", "verdict": "no such"}), SPEC, exit_code=0)
    assert outcome is None
    assert "unknown schemaVersion '2'" in problem
    outcome, problem = parse_result(json.dumps({"verdict": "passed", "criteria": []}), SPEC, exit_code=0)
    assert outcome is None
    assert "schemaVersion None" in problem


@pytest.mark.parametrize(("verdict", "code"), [("failed", 1), ("blocked", 2), ("refused", 3), ("waived", 5)])
def test_only_passed_is_a_pass(verdict: str, code: int) -> None:
    outcome, _ = parse_result(_result(verdict, {CRITERIA[0]["id"]: "failed"}), SPEC, exit_code=code)
    assert outcome is not None
    assert outcome.verdict is not StageVerdict.PASSED
    expected = StageVerdict.CHANGES_REQUESTED if verdict == "failed" else StageVerdict.NEEDS_HUMAN
    assert outcome.verdict is expected
    assert outcome.provider_verdict == verdict


def test_a_waiver_is_named_and_held() -> None:
    text = _result("waived", waived=[{"criterionId": CRITERIA[0]["id"], "by": "alice"}])
    outcome, _ = parse_result(text, SPEC, exit_code=5)
    assert outcome is not None
    assert "by alice" in outcome.summary
    assert "never a pass" in outcome.summary


def test_an_exit_code_that_contradicts_the_verdict_is_refused() -> None:
    outcome, problem = parse_result(_result("passed"), SPEC, exit_code=1)
    assert outcome is None
    assert "exited 1" in problem


def test_a_result_of_another_job_is_refused() -> None:
    outcome, problem = parse_result(_result(job={"id": "someone-else"}), SPEC, exit_code=0)
    assert outcome is None
    assert "someone-else" in problem


def test_a_criterion_qare_did_not_report_is_unverified() -> None:
    document = json.loads(_result())
    document["criteria"] = document["criteria"][:1]
    outcome, _ = parse_result(json.dumps(document), SPEC, exit_code=0)
    assert outcome is not None
    assert outcome.criteria[1].outcome == "unverified"
    assert "reported nothing" in outcome.criteria[1].reason


def _job_dir(tmp_path: Path, status: dict[str, Any], result: str | None = None) -> Path:
    job = tmp_path / SPEC["request_id"]
    request_spec(job, web_base="file:///x", base="a" * 40, head=HEAD)
    (job / runner.STATUS_FILE).write_text(json.dumps(status))
    if result is not None:
        (job / "evidence").mkdir()
        (job / "evidence" / "result.json").write_text(result)
    return job


def test_exit_four_is_never_a_verdict(tmp_path: Path) -> None:
    job = _job_dir(tmp_path, {"outcome": "ran", "exit_code": 4}, _result())
    assert job_from_status(job.name, json.loads((job / runner.STATUS_FILE).read_text()), job).state is JobState.RETRYABLE


def test_an_unknown_schema_version_holds_the_card(tmp_path: Path) -> None:
    job = _job_dir(tmp_path, {"outcome": "ran", "exit_code": 0}, json.dumps({"schemaVersion": "9"}))
    stage_job = job_from_status(job.name, {"outcome": "ran", "exit_code": 0}, job)
    assert stage_job.state is JobState.NEEDS_HUMAN
    assert "schemaVersion" in stage_job.reason


def test_a_run_with_no_result_is_retried(tmp_path: Path) -> None:
    job = _job_dir(tmp_path, {"outcome": "ran", "exit_code": 0})
    assert job_from_status(job.name, {"outcome": "ran", "exit_code": 0}, job).state is JobState.RETRYABLE


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (runner.UNAVAILABLE, JobState.RETRYABLE),
        (runner.ERROR, JobState.RETRYABLE),
        (runner.TIMEOUT, JobState.RETRYABLE),
        (runner.NO_PROFILE, JobState.NEEDS_HUMAN),
        (runner.NO_CRITERIA, JobState.NEEDS_HUMAN),
        (runner.MISCONFIGURED, JobState.NEEDS_HUMAN),
    ],
)
def test_runs_that_end_before_a_verdict(tmp_path: Path, outcome: str, state: JobState) -> None:
    job = _job_dir(tmp_path, {"outcome": outcome, "step": "x", "reason": "why"})
    stage_job = job_from_status(job.name, {"outcome": outcome, "step": "x", "reason": "why"}, job)
    assert stage_job.state is state
    assert stage_job.outcome is None


def test_web_base_follows_the_api_base() -> None:
    assert web_base_for("https://api.github.com") == "https://github.com"
    assert web_base_for("https://ghe.example.com/api/v3") == "https://ghe.example.com"


# -- submitting and polling ------------------------------------------------------------------


class _Compare(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"diff --git a/app.txt b/app.txt\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        del args


@pytest.fixture
def compare_api() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Compare)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _access(api: str, *, fail: bool = False) -> GitHubAccess:
    async def token(repository: str) -> str:
        if fail:
            raise RuntimeError("no installation")
        return TOKEN

    return GitHubAccess(token, lambda repository: api)


def _request(base: str, head: str) -> StageRequest:
    return StageRequest(
        card_id="PVTI_card", stage="qa", repository="acme/widgets", pr_number=7, pr_node_id="PR_7",
        head_sha=head, base_sha=base, base_ref="main", issue_number=5, issue_node_id="I_5",
        criteria=tuple(StageCriterion(**c) for c in CRITERIA),
    )


async def _wait(provider: QAreProvider, ref: str) -> Any:
    for _ in range(300):
        job = await provider.poll(ref)
        if job.state is not JobState.PENDING:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("the run did not finish")


def _point_at(monkeypatch: pytest.MonkeyPatch, web: str) -> None:
    # The runner builds the clone URL from the API's web base; the test repository is local.
    monkeypatch.setattr("coordinare.services.qare.web_base_for", lambda api: web)


@pytest.mark.asyncio
async def test_a_submitted_job_runs_detached_and_is_polled_to_its_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compare_api: str,
) -> None:
    web, base, head = make_remote(tmp_path)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(scenario(tmp_path, verdict="failed", outcomes={CRITERIA[1]["id"]: "failed"})))
    _point_at(monkeypatch, web)
    provider = QAreProvider("qare", _config(), work_dir=tmp_path / "runs", github=_access(compare_api))
    request = _request(base, head)
    job = await provider.submit(request, request.request_id(1))
    assert job.state is JobState.PENDING
    done = await _wait(provider, job.ref)
    assert done.state is JobState.DONE, done.reason
    assert done.outcome is not None
    assert done.outcome.verdict is StageVerdict.CHANGES_REQUESTED
    assert done.outcome.reviewed_head == head
    assert [c.outcome for c in done.outcome.criteria] == ["proven", "failed"]
    spec = provider.request_spec(job.ref)
    assert TOKEN not in json.dumps(spec)


@pytest.mark.asyncio
async def test_submitting_the_same_request_id_finds_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compare_api: str,
) -> None:
    web, base, head = make_remote(tmp_path)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(scenario(tmp_path)))
    _point_at(monkeypatch, web)
    provider = QAreProvider("qare", _config(), work_dir=tmp_path / "runs", github=_access(compare_api))
    request = _request(base, head)
    first = await provider.submit(request, request.request_id(1))
    await _wait(provider, first.ref)
    launched = (tmp_path / "runs" / first.ref / runner.LAUNCH_FILE).read_text()
    again = await provider.submit(request, request.request_id(1))
    assert again.ref == first.ref
    assert again.state is JobState.DONE
    assert (tmp_path / "runs" / first.ref / runner.LAUNCH_FILE).read_text() == launched


@pytest.mark.asyncio
async def test_a_restarted_daemon_polls_the_same_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compare_api: str,
) -> None:
    web, base, head = make_remote(tmp_path)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(scenario(tmp_path)))
    _point_at(monkeypatch, web)
    first = QAreProvider("qare", _config(), work_dir=tmp_path / "runs", github=_access(compare_api))
    request = _request(base, head)
    job = await first.submit(request, request.request_id(1))
    await _wait(first, job.ref)
    restarted = QAreProvider("qare", _config(), work_dir=tmp_path / "runs", github=_access(compare_api))
    found = await restarted.poll(job.ref)
    assert found.state is JobState.DONE
    assert found.outcome is not None
    assert found.outcome.verdict is StageVerdict.PASSED


@pytest.mark.asyncio
async def test_a_runner_that_died_without_a_status_is_retried(tmp_path: Path) -> None:
    provider = QAreProvider("qare", _config(), work_dir=tmp_path)
    job = tmp_path / "coordinare.qa.abc.1"
    request_spec(job, web_base="file:///x", base="a" * 40, head=HEAD)
    (job / runner.LAUNCH_FILE).write_text(json.dumps({"pid": 2**22 + 12345}))
    assert (await provider.poll(job.name)).state is JobState.RETRYABLE


@pytest.mark.asyncio
async def test_a_running_runner_is_pending_until_it_stops(tmp_path: Path) -> None:
    provider = QAreProvider("qare", _config(), work_dir=tmp_path)
    job = tmp_path / "coordinare.qa.abc.1"
    request_spec(job, web_base="file:///x", base="a" * 40, head=HEAD, created_at=time.time())
    # A process standing in for the detached run, with its job directory on its command line.
    run = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(job)])
    try:
        (job / runner.LAUNCH_FILE).write_text(json.dumps({"pid": run.pid}))
        assert (await provider.poll(job.name)).state is JobState.PENDING
    finally:
        run.kill()
        run.wait()
    assert (await provider.poll(job.name)).state is JobState.RETRYABLE


@pytest.mark.asyncio
async def test_a_run_past_its_timeout_is_not_waited_on_forever(tmp_path: Path) -> None:
    provider = QAreProvider("qare", _config(), work_dir=tmp_path)
    job = tmp_path / "coordinare.qa.abc.1"
    request_spec(job, web_base="file:///x", base="a" * 40, head=HEAD, created_at=time.time() - 10_000,
                 timeout_seconds=60)
    # Even a live pid (here this test's own) is not the run once its timeout is long past.
    (job / runner.LAUNCH_FILE).write_text(json.dumps({"pid": os.getpid()}))
    stage_job = await provider.poll(job.name)
    assert stage_job.state is JobState.RETRYABLE
    assert "timeout" in stage_job.reason


@pytest.mark.asyncio
async def test_a_runner_that_does_not_start_leaves_no_job(tmp_path: Path) -> None:
    provider = QAreProvider(
        "qare", _config(), work_dir=tmp_path, github=_access("http://127.0.0.1:9"),
        runner_argv=[sys.executable, "-c", "raise SystemExit(3)"],
    )
    with pytest.raises(TransportError, match="did not start"):
        await provider.submit(_request("a" * 40, HEAD), "coordinare.qa.abc.1")
    assert not (tmp_path / "coordinare.qa.abc.1").exists()


@pytest.mark.asyncio
async def test_a_relative_work_dir_is_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    provider = QAreProvider("qare", _config(), work_dir=Path("stage-providers/qare"))
    assert provider.work_dir == tmp_path.resolve() / "stage-providers" / "qare"


@pytest.mark.asyncio
async def test_no_token_means_no_job(tmp_path: Path) -> None:
    provider = QAreProvider("qare", _config(), work_dir=tmp_path, github=_access("http://x", fail=True))
    with pytest.raises(TransportError, match="GitHub token"):
        await provider.submit(_request("a" * 40, HEAD), "coordinare.qa.abc.1")
    assert not (tmp_path / "coordinare.qa.abc.1").exists()


@pytest.mark.asyncio
async def test_a_job_whose_directory_is_gone_is_retried(tmp_path: Path) -> None:
    provider = QAreProvider("qare", _config(), work_dir=tmp_path)
    assert (await provider.poll("coordinare.qa.gone.1")).state is JobState.RETRYABLE


@pytest.mark.asyncio
async def test_a_ref_outside_the_work_dir_is_refused(tmp_path: Path) -> None:
    provider = QAreProvider("qare", _config(), work_dir=tmp_path)
    with pytest.raises(TransportError):
        await provider.poll("../escape")


# -- health ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_requires_the_pinned_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ok, detail = await QAreProvider("qare", _config(), work_dir=tmp_path).health()
    assert ok, detail
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(scenario(tmp_path, version="2026.1.1")))
    ok, detail = await QAreProvider("qare", _config(), work_dir=tmp_path).health()
    assert not ok
    assert "2026.1.1" in detail


@pytest.mark.asyncio
async def test_health_names_a_missing_qare(tmp_path: Path) -> None:
    ok, detail = await QAreProvider("qare", _config(command=["/no/such/qare"]), work_dir=tmp_path).health()
    assert not ok
    assert "could not be started" in detail


@pytest.mark.asyncio
async def test_health_names_a_qare_that_cannot_be_executed(tmp_path: Path) -> None:
    script = tmp_path / "qare.sh"
    script.write_text("echo no shebang\n")
    script.chmod(0o644)
    ok, detail = await QAreProvider("qare", _config(command=[str(script)]), work_dir=tmp_path).health()
    assert not ok
    assert "could not be started" in detail
