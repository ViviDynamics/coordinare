"""391: a QA run that found the bug must record it as a failure.

On the persona bench, Haiku 5.5 and Sonnet 5.5 both found the planted KeyError
and listed it in their verification steps, yet the parsed report had no
failures and no executed checks, so the run ended as an unsubstantiated pass.
Two drops explain it: ``failures`` entries that were not objects were discarded,
and executed checks spelled with other common keys were discarded. Both are
kept now. The evidence guard itself is unchanged: a check still needs a command
and an exit code.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends.base import BackendStatus
from performer.config import Settings
from performer.main import handle_status
from performer.models import Performance, Score, Stand
from performer.protocol import PerformerMessage
from performer.qa_postprocess import _qa_execution_evidence, _qa_reported_failures


def _perf() -> Performance:
    stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
    stand.git_env = {}
    score = Score(title="QA", repo_url="https://github.com/acme/repo", branch="feat/test")
    return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="qa")


async def _finalize(output: dict):
    perf = _perf()
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps(output))
    msg = PerformerMessage(action="status", session_id="sid", payload={})
    with patch("performer.main.commit_file", new=AsyncMock()):
        return await handle_status(msg, perf, Settings(AGENT_BACKEND="nare"))


class TestReportedFailures:
    def test_string_failure_is_kept_as_a_defect(self):
        out = _qa_reported_failures(["KeyError when the cart has no discount key"])
        assert out == [{
            "criterion": "QA reported failure",
            "actual": "KeyError when the cart has no discount key",
            "message": "KeyError when the cart has no discount key",
        }]

    def test_objects_pass_through_and_blanks_drop(self):
        obj = {"criterion": "c", "expected": "e", "actual": "a"}
        assert _qa_reported_failures([obj, "  ", None, 3]) == [obj]

    def test_not_a_list_is_nothing(self):
        assert _qa_reported_failures("KeyError") == []

    @pytest.mark.parametrize("placeholder", ["None", "n/a", "N/A", "none.", "No failures", "nothing", "-", "[]"])
    def test_a_placeholder_for_no_failures_is_not_a_defect(self, placeholder):
        assert _qa_reported_failures([placeholder]) == []


class TestExecutionEvidence:
    @pytest.mark.parametrize("entry", [
        {"cmd": "pytest -q", "exit_code": 1},
        {"command": "pytest -q", "returncode": 1},
        {"command": "pytest -q", "exitCode": 1},
        {"command": "pytest -q", "exit_status": "1"},
        {"command": "pytest -q", "return_code": 1},
        {"command": "", "cmd": "pytest -q", "exit_code": 1},
        {"command": "pytest -q", "exit_code": "", "returncode": 1},
    ])
    def test_common_key_spellings_are_evidence(self, entry):
        checks = _qa_execution_evidence({"executed_checks": [entry]})
        assert len(checks) == 1
        assert checks[0]["command"] == "pytest -q"
        assert checks[0]["exit_code"] == 1

    @pytest.mark.parametrize(("value", "expected"), [
        (0.0, 0), ("0 (passed)", "0 (passed)"), ("exit 0", "exit 0"), (2, 2), ("3", 3),
    ])
    def test_exit_code_shapes_accepted_before_still_count(self, value, expected):
        checks = _qa_execution_evidence({"executed_checks": [{"command": "pytest -q", "exit_code": value}]})
        assert [c["exit_code"] for c in checks] == [expected]

    @pytest.mark.parametrize("entry", [
        {"command": "pytest -q"},
        {"command": "pytest -q", "exit_code": None},
        {"command": "", "exit_code": 0},
        "pytest -q",
    ])
    def test_a_check_without_command_and_exit_code_is_still_not_evidence(self, entry):
        assert _qa_execution_evidence({"executed_checks": [entry]}) == []


class TestFinalize:
    @pytest.mark.asyncio
    async def test_found_bug_as_strings_reaches_qa_failed_with_the_bug(self):
        resp = await _finalize({
            "passed": False,
            "criteria_checked": 1,
            "criteria_passed": 0,
            "failures": ["checkout_total raises KeyError when the cart has no 'discount' key"],
            "executed_checks": [{
                "cmd": "python -c \"from bench.cart import checkout_total; checkout_total({'items': []})\"",
                "returncode": 1, "output": "KeyError: 'discount'",
            }],
            "verification_steps": ["FAIL: checkout_total({'items': []}) raises KeyError"],
        })
        assert resp.status == "qa_failed"
        assert any("KeyError" in str(f.get("actual")) for f in resp.failures)
        assert not any(f.get("type") == "unsubstantiated_pass" for f in resp.failures)

    @pytest.mark.asyncio
    async def test_zero_evidence_pass_is_still_refused(self):
        resp = await _finalize({
            "passed": True, "criteria_checked": 1, "criteria_passed": 1, "failures": [],
            "executed_checks": [{"command": "pytest -q"}],
            "verification_steps": ["looked at it"],
        })
        assert resp.status != "qa_passed"
        assert any(f.get("type") == "unsubstantiated_pass" for f in resp.failures)


class TestContractReminder:
    def test_qa_reminder_states_the_failure_and_check_shapes(self):
        from types import SimpleNamespace

        from performer.backends._role_contract import role_closing_section

        text = "\n".join(role_closing_section(SimpleNamespace(role="qa")))
        assert '"failures": [{"criterion"' in text
        assert '"executed_checks": [{"command"' in text
        assert "every command you ran" in text
        assert "including the ones that failed" in text
