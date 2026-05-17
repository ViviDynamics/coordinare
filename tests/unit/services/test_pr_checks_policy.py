"""Unit tests for pr_checks_policy.decide (spec 064)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coordinare.services.pr_checks_policy import decide
from coordinare.services.pr_checks_service import CheckEntry, CheckRollup

NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)


def _rollup(checks: list[CheckEntry], *, head_age_seconds: float = 60.0, bp_readable: bool = True) -> CheckRollup:
    return CheckRollup(
        pr_number=1,
        head_sha="abc1234",
        head_pushed_at=NOW - timedelta(seconds=head_age_seconds),
        branch_protection_readable=bp_readable,
        checks=checks,
    )


def _req(name: str, status: str, conclusion: str | None = None) -> CheckEntry:
    return CheckEntry(
        name=name,
        status=status,  # type: ignore[arg-type]
        conclusion=conclusion,  # type: ignore[arg-type]
        is_required=True,
    )


def test_all_passing_required_checks_forwards() -> None:
    rollup = _rollup([_req("ci/test", "completed", "success"), _req("build", "completed", "success")])
    d = decide(rollup, now=NOW)
    assert d.action == "FORWARD"


@pytest.mark.parametrize("conclusion", ["neutral", "skipped"])
def test_neutral_and_skipped_count_as_pass(conclusion: str) -> None:
    rollup = _rollup([_req("docs", "completed", conclusion)])
    d = decide(rollup, now=NOW)
    assert d.action == "FORWARD"


@pytest.mark.parametrize(
    "conclusion", ["failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"]
)
def test_terminal_failure_conclusions_bounce(conclusion: str) -> None:
    rollup = _rollup([_req("ci/test", "completed", conclusion)])
    d = decide(rollup, now=NOW)
    assert d.action == "BOUNCE"
    assert d.reason == "required_check_failed"
    assert "ci/test" in d.failed


def test_pending_check_holds() -> None:
    rollup = _rollup([_req("ci/test", "in_progress")])
    d = decide(rollup, now=NOW)
    assert d.action == "HOLD"
    assert "ci/test" in d.pending


def test_pending_timeout_bounces() -> None:
    rollup = _rollup([_req("ci/test", "in_progress")], head_age_seconds=1000)
    d = decide(rollup, pending_timeout_seconds=900, now=NOW)
    assert d.action == "BOUNCE"
    assert d.reason == "pending_timeout"


def test_failure_takes_priority_over_pending() -> None:
    rollup = _rollup(
        [_req("ci/test", "completed", "failure"), _req("build", "in_progress")],
    )
    d = decide(rollup, now=NOW)
    assert d.action == "BOUNCE"
    assert d.failed == ["ci/test"]


def test_non_required_checks_ignored_when_bp_readable() -> None:
    not_req = CheckEntry(name="optional", status="completed", conclusion="failure", is_required=False)
    rollup = _rollup([_req("ci/test", "completed", "success"), not_req])
    d = decide(rollup, now=NOW)
    assert d.action == "FORWARD"


def test_unreadable_bp_pass_mode_forwards_when_all_complete_pass() -> None:
    entries = [
        CheckEntry(name="ci/test", status="completed", conclusion="success", is_required=False),
    ]
    rollup = _rollup(entries, bp_readable=False)
    d = decide(rollup, treat_unknown_required_as="pass", now=NOW)
    assert d.action == "FORWARD"


def test_unreadable_bp_pass_mode_ignores_non_required_failure() -> None:
    # Regression for review issue #2: with BP unreadable and mode='pass', a
    # non-required check failure must NOT bounce — the gate has no signal about
    # which checks are required, so it should fall through to FORWARD.
    entries = [
        CheckEntry(name="flaky", status="completed", conclusion="failure", is_required=False),
        CheckEntry(name="ci/test", status="completed", conclusion="success", is_required=False),
    ]
    rollup = _rollup(entries, bp_readable=False)
    d = decide(rollup, treat_unknown_required_as="pass", now=NOW)
    assert d.action == "FORWARD"


def test_unreadable_bp_block_mode_bounces() -> None:
    rollup = _rollup([], bp_readable=False)
    d = decide(rollup, treat_unknown_required_as="block", now=NOW)
    assert d.action == "BOUNCE"
    assert d.reason == "branch_protection_unreadable"


def test_no_required_checks_forwards() -> None:
    rollup = _rollup([])
    d = decide(rollup, now=NOW)
    assert d.action == "FORWARD"
