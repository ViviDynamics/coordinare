"""#402: a suite that outlives its budget is not a service that never came up.

Card website#107 was held ENV_BLOCKED with "the test command did not finish
within 600s ... usually waiting on a service that never came up". Its own
container log had, fourteen minutes earlier, ``started postgres (pid 6557)``
and ``services-health: all ok``. Nothing hung. ``bundle exec rspec`` is the
whole unsharded website suite; CI shards it at ~17.8 minutes serial. Killed at
600s, still running. The timeout branch's prior ("a hang is usually a service")
is right when nothing is known, and wrong every time here, and the outcome was
an ENV_BLOCKED hold plus a forced env-cache regen (two bootstraps) for an
environment that was fine.

The evidence to tell the two apart was already in the job: the services start
and health check had logged success. This records that fact where the gate
can read it, without consuming anything.

MUTATIONS THAT MUST FAIL A TEST HERE:
  M1  ignore services_healthy_this_job in the timeout branch
      -> test_a_timeout_after_healthy_services_is_a_budget_finding_not_a_hang
  M2  never record health-check success
      -> test_services_healthy_is_recorded_by_the_health_check_and_reset_per_job
  M3  drop the report from the env_blocked response
      -> test_env_blocked_response_carries_the_budget_report
  M4  inline the response at the call site, or read the budget from the wrong
      place (review) -> test_handle_status_reports_the_budget_from_the_gate_config
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.test_main import _ci_run_result, _detection


class TestTimeoutClassification:
    @pytest.mark.asyncio
    async def test_a_timeout_after_healthy_services_is_a_budget_finding_not_a_hang(self) -> None:
        """M1: healthy services + timeout = the suite exceeded its budget. Still
        held (an operator has to raise the budget), but named honestly and,
        via budget_exceeded, without a needless env-cache regen."""
        from performer.main import _run_test_check

        with (
            patch("coordinare_ci_detection.detect", return_value=_detection("bundle exec rspec")),
            patch("performer.main.run_command", new=AsyncMock(return_value=_ci_run_result(
                False, stderr="Command timed out after 600s", timed_out=True, duration=600.5))),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
            patch("performer.workspace.services_healthy_this_job", return_value=True),
        ):
            result = await _run_test_check(Path("/tmp/x"), timeout_seconds=600)

        assert result.passed is False
        assert result.env_blocked is True, "still a hold: only an operator can raise the budget"
        assert result.budget_exceeded is True
        reason = result.env_reason or ""
        assert "health check" in reason and "budget" in reason and "local_test_gate.timeout_seconds" in reason, reason
        assert "waiting on a service" not in reason, "the hang prior must not be repeated when it is known false"

    @pytest.mark.asyncio
    async def test_a_timeout_without_healthy_services_stays_a_hang_hold(self) -> None:
        """Nothing known about services: the existing prior stands unchanged."""
        from performer.main import _run_test_check

        with (
            patch("coordinare_ci_detection.detect", return_value=_detection("pytest")),
            patch("performer.main.run_command", new=AsyncMock(return_value=_ci_run_result(
                False, stderr="Command timed out after 600s", timed_out=True))),
            patch("performer.workspace.consume_services_start_failure", return_value=None),
            patch("performer.workspace.consume_env_cache_health_failure", return_value=False),
            patch("performer.workspace.services_healthy_this_job", return_value=False),
        ):
            result = await _run_test_check(Path("/tmp/x"))

        assert result.env_blocked is True
        assert result.budget_exceeded is False
        assert "waiting on a service" in (result.env_reason or "")


class TestHealthIsRecorded:
    @pytest.mark.asyncio
    async def test_services_healthy_is_recorded_by_the_health_check_and_reset_per_job(
        self, tmp_path: Path
    ) -> None:
        """M2: a non-consuming record, set by the health check's success path
        and scoped to one job by the same reset the start-failure uses."""
        import performer.workspace as ws

        ws.reset_services_start_failure()
        assert ws.services_healthy_this_job() is False

        health = tmp_path / "services" / "services-health.sh"
        health.parent.mkdir()
        health.write_text("#!/bin/sh\necho ok\n")
        os.chmod(health, 0o755)
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"services-health: all ok\n", b""))
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc):
            await ws._run_env_cache_health_check(str(tmp_path), dict(os.environ))

        assert ws.services_healthy_this_job() is True
        assert ws.services_healthy_this_job() is True, "non-consuming: reading it must not clear it"
        ws.reset_services_start_failure()
        assert ws.services_healthy_this_job() is False, "must be scoped to the job, like the start failure"

    @pytest.mark.asyncio
    async def test_a_failing_health_check_does_not_record_healthy(self, tmp_path: Path) -> None:
        import performer.workspace as ws

        ws.reset_services_start_failure()
        health = tmp_path / "services" / "services-health.sh"
        health.parent.mkdir()
        health.write_text("#!/bin/sh\nexit 1\n")
        os.chmod(health, 0o755)
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"postgres: no response\n"))
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc):
            await ws._run_env_cache_health_check(str(tmp_path), dict(os.environ))
        assert ws.services_healthy_this_job() is False
        ws.consume_env_cache_health_failure()  # drain, so other tests start clean


class TestResponseReport:
    def test_env_blocked_response_carries_the_budget_report(self) -> None:
        """M3: coordinare decides whether to regenerate the env cache from the
        structured report, not by parsing prose. Without the report it must
        keep today's behaviour (regen), so the field is absent when the hold
        is a genuine environment problem."""
        from performer.main import LocalTestResult, _env_blocked_gate_response

        perf = SimpleNamespace(session_id="s1", state="running")
        budget = LocalTestResult(
            passed=False, command="bundle exec rspec", output="", duration_seconds=600.5,
            env_blocked=True, env_reason="suite exceeded the 600s budget", budget_exceeded=True,
            exit_code=-1,
        )
        resp = _env_blocked_gate_response(perf, budget, timeout_seconds=600)
        assert resp.status == "env_blocked" and resp.reason == "suite exceeded the 600s budget"
        assert resp.report == {"local_test_gate": {
            "budget_exceeded": True, "timeout_seconds": 600, "duration_seconds": 600.5,
        }}
        assert perf.state == "env_blocked"

        hang = LocalTestResult(
            passed=False, command="pytest", output="", duration_seconds=600.5,
            env_blocked=True, env_reason="services never came up", exit_code=-1,
        )
        assert _env_blocked_gate_response(perf, hang, timeout_seconds=600).report is None


class TestCallSite:
    @pytest.mark.asyncio
    async def test_handle_status_reports_the_budget_from_the_gate_config(self) -> None:
        """Review (M4): the helper was tested in isolation and the existing
        handle_status test never looked at resp.report, so inlining the response
        at the call site, or reading timeout_seconds from the wrong place, passed
        everything. This drives the real call site and asserts the report
        carries the gate's configured budget, not the helper's default."""
        from performer.main import LocalTestResult, handle_status
        from tests.unit.test_main import BackendStatus, _make_perf, _msg

        # Built the way the gate tests build an implementer, with the budget
        # raised so a default of 600 anywhere in the path would be caught.
        perf = _make_perf(session_id="sid")
        perf.score.role = "implementer"
        perf.score.local_test_gate = {"enabled": True, "timeout_seconds": 1800}
        perf.backend.get_status.return_value = BackendStatus(state="done")
        with (
            patch("performer.main._run_ci_check", new=AsyncMock(return_value=(True, ""))),
            patch("performer.main._run_test_check", new=AsyncMock(return_value=LocalTestResult(
                passed=False, command="bundle exec rspec", output="", duration_seconds=1800.4,
                env_blocked=True, env_reason="suite exceeded the budget", exit_code=-1,
                budget_exceeded=True))),
            patch("performer.main.push_branch", new=AsyncMock()),
            patch("performer.main.create_pull_request", new=AsyncMock()),
        ):
            resp = await handle_status(_msg("status", session_id="sid"), perf)

        assert resp.status == "env_blocked"
        assert resp.report == {"local_test_gate": {
            "budget_exceeded": True, "timeout_seconds": 1800, "duration_seconds": 1800.4,
        }}, resp.report
