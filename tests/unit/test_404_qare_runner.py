"""404: the detached QAre run: checkout, profile, plan, run, and how it reports its end."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coordinare.services import qare_runner as runner
from tests.unit.qare_fixtures import CRITERIA, calls, make_remote, request_spec, scenario

TOKEN = "ghs_checkouttokenvalue000000000000000000"


@pytest.fixture(autouse=True)
def _no_network_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    def diff(request: dict, *, base: str, head: str, token: str, deadline: float) -> str:
        assert token == TOKEN
        return f"diff --git a/app.txt b/app.txt\n# {base[:7]}..{head[:7]}\n"

    monkeypatch.setattr(runner, "_change_diff", diff)


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, remote: dict | None = None, **scene: object):
    web, base, head = make_remote(tmp_path, **(remote or {}))
    path = scenario(tmp_path, **scene)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(path))
    monkeypatch.setenv("COORDINARE_SECRET_THING", "do-not-pass")
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=web, base=base, head=head)
    status = runner.run_job(job, token=TOKEN)
    return job, status, calls(path), base, head


def test_a_run_plans_then_runs_and_records_how_it_ended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job, status, seen, base, head = _run(tmp_path, monkeypatch)
    assert status["outcome"] == runner.RAN
    assert status["exit_code"] == 0
    assert json.loads((job / runner.STATUS_FILE).read_text())["outcome"] == runner.RAN
    result = json.loads((job / "evidence" / "result.json").read_text())
    assert result["job"]["id"] == job.name
    commands = [c["argv"][0] for c in seen]
    assert commands == ["--version", "plan", "run"]
    plan_argv, run_argv = seen[1]["argv"], seen[2]["argv"]
    assert json.loads(Path(plan_argv[plan_argv.index("--criteria") + 1]).read_text()) == CRITERIA
    assert run_argv[run_argv.index("--head") + 1] == head
    assert run_argv[run_argv.index("--base") + 1] == base
    assert run_argv[run_argv.index("--id") + 1] == job.name
    # The base revision's profile runs, and the base checkout is handed to QAre.
    assert run_argv[run_argv.index("--profile") + 1] == str(job / "base" / ".qa")
    assert run_argv[run_argv.index("--base-repo") + 1] == str(job / "base")
    # The checkouts go once the run ends; the evidence stays.
    assert not (job / "repo").exists()
    assert not (job / "base").exists()
    assert (job / "evidence" / "result.json").exists()


def test_qare_sees_only_the_passthrough_and_base_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _job, _status, seen, _b, _h = _run(tmp_path, monkeypatch)
    for call in seen:
        assert "FAKE_QARE_SCENARIO" in call["env"]
        assert "COORDINARE_SECRET_THING" not in call["env"]
        assert runner.CREDENTIAL_ENV not in call["env"]
        assert not any(name.startswith("GIT_CONFIG") for name in call["env"])


def test_the_token_is_never_written_to_the_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job, _status, _seen, _b, _h = _run(tmp_path, monkeypatch)
    for path in job.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            assert TOKEN.encode() not in path.read_bytes(), path


def test_the_head_profile_is_used_when_the_base_has_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job, status, seen, _b, _h = _run(
        tmp_path, monkeypatch, remote={"profile_on_base": False, "profile_on_head": True},
    )
    assert status["outcome"] == runner.RAN
    run_argv = seen[-1]["argv"]
    assert run_argv[run_argv.index("--profile") + 1] == str(job / "repo" / ".qa")


def test_no_profile_stops_with_how_to_add_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _job, status, seen, _b, _h = _run(tmp_path, monkeypatch, remote={"profile_on_base": False})
    assert status["outcome"] == runner.NO_PROFILE
    assert "qare init" in status["reason"]
    assert [c["argv"][0] for c in seen] == ["--version"]


def test_a_version_other_than_the_pin_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _job, status, seen, _b, _h = _run(tmp_path, monkeypatch, version="2026.10.1")
    assert status["outcome"] == runner.MISCONFIGURED
    assert "2026.10.1" in status["reason"]
    assert len(seen) == 1


def test_a_planner_failure_ends_before_a_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _job, status, seen, _b, _h = _run(tmp_path, monkeypatch, plan_exit=4)
    assert status["outcome"] == runner.ERROR
    assert status["step"] == "plan"
    assert [c["argv"][0] for c in seen] == ["--version", "plan"]


def test_qare_that_cannot_start_is_unavailable(tmp_path: Path) -> None:
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base="file:///nowhere", base="a" * 40, head="b" * 40, command=["/no/such/qare"])
    status = runner.run_job(job, token=TOKEN)
    assert status["outcome"] == runner.UNAVAILABLE


def test_a_checkout_that_fails_ends_before_a_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(scenario(tmp_path)))
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=f"file://{tmp_path}/missing", base="a" * 40, head="b" * 40)
    status = runner.run_job(job, token=TOKEN)
    assert status["outcome"] == runner.ERROR
    assert status["step"] == "checkout"


def test_no_criteria_stops_before_planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web, base, head = make_remote(tmp_path)
    path = scenario(tmp_path)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(path))
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=web, base=base, head=head, criteria=[])
    status = runner.run_job(job, token=TOKEN)
    assert status["outcome"] == runner.NO_CRITERIA
    assert "plan" not in [c["argv"][0] for c in calls(path)]


def test_main_takes_the_token_out_of_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(job_dir: Path, *, token: str = "", env: dict | None = None) -> dict:
        import os

        seen["token"] = token
        seen["left"] = runner.CREDENTIAL_ENV in os.environ
        return {}

    monkeypatch.setenv(runner.CREDENTIAL_ENV, TOKEN)
    monkeypatch.setattr(runner, "run_job", fake_run)
    assert runner.main([str(tmp_path)]) == 0
    assert seen == {"token": TOKEN, "left": False}


def test_a_pr_whose_base_is_its_head_runs_one_sided(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web, _base, head = make_remote(tmp_path, profile_on_head=True)
    path = scenario(tmp_path)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(path))
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=web, base=head, head=head)
    status = runner.run_job(job, token=TOKEN)
    assert status["outcome"] == runner.RAN, status
    run_argv = calls(path)[-1]["argv"]
    assert "--base-repo" not in run_argv
    assert not (job / "base").exists()


def test_a_diff_github_refuses_comes_from_the_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def too_large(request: dict, *, base: str, head: str, token: str, deadline: float) -> str:
        raise runner._StopError({"outcome": runner.ERROR, "step": "diff", "http_status": 406,
                                 "reason": "GitHub compare answered 406"})

    web, base, head = make_remote(tmp_path)
    monkeypatch.setattr(runner, "_change_diff", too_large)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(scenario(tmp_path)))
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=web, base=base, head=head)
    status = runner.run_job(job, token=TOKEN)
    assert status["outcome"] == runner.RAN, status
    diff = (job / runner.DIFF_FILE).read_text()
    assert "-v1" in diff
    assert "+v2" in diff
    assert "406" in (job / runner.LOG_FILE).read_text()


def test_a_relative_job_dir_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web, base, head = make_remote(tmp_path)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(scenario(tmp_path)))
    request_spec(tmp_path / "jobs" / "coordinare.qa.abc.1", web_base=web, base=base, head=head)
    monkeypatch.chdir(tmp_path)
    status = runner.run_job(Path("jobs/coordinare.qa.abc.1"), token=TOKEN)
    assert status["outcome"] == runner.RAN, status
    assert (tmp_path / "jobs" / "coordinare.qa.abc.1" / "evidence" / "result.json").exists()


def test_a_timeout_stops_what_the_command_started(tmp_path: Path) -> None:
    import os
    import time as _time

    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base="file:///x", base="a" * 40, head="b" * 40)
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time; "
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(pid_file)!r}, 'w').write(str(c.pid)); time.sleep(60)"
    )
    run = runner._Run(job, {**json.loads((job / "request.json").read_text()), "timeout_seconds": 2},
                      token="", env=dict(os.environ), started=_time.monotonic())
    import asyncio

    with pytest.raises(runner._StopError) as stopped:
        asyncio.run(run._call([sys.executable, "-c", script], cwd=job, env=dict(os.environ), step="run"))
    assert stopped.value.status["outcome"] == runner.TIMEOUT
    child = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.1)
    else:
        os.kill(child, 9)
        raise AssertionError("the command's child outlived the timeout")


def test_a_compare_that_fails_otherwise_is_retried_not_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(request: dict, *, base: str, head: str, token: str, deadline: float) -> str:
        raise runner._StopError({"outcome": runner.ERROR, "step": "diff", "http_status": 502,
                                 "reason": "GitHub compare answered 502"})

    web, base, head = make_remote(tmp_path)
    monkeypatch.setattr(runner, "_change_diff", down)
    monkeypatch.setenv("FAKE_QARE_SCENARIO", str(scenario(tmp_path)))
    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base=web, base=base, head=head)
    status = runner.run_job(job, token=TOKEN)
    assert status["outcome"] == runner.ERROR
    assert status["step"] == "diff"


def test_the_name_placeholder_names_the_step(tmp_path: Path) -> None:
    import asyncio
    import os
    import time as _time

    job = tmp_path / "jobs" / "coordinare.qa.abc.1"
    request_spec(job, web_base="file:///x", base="a" * 40, head="b" * 40)
    run = runner._Run(job, json.loads((job / "request.json").read_text()), token="", env=dict(os.environ),
                      started=_time.monotonic())
    code, out, _err = asyncio.run(run._call(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "--name={name}"], cwd=job, env=dict(os.environ), step="plan",
    ))
    assert code == 0
    assert out.strip().startswith("--name=coordinare-qare-")
    assert out.strip().endswith("-plan")
