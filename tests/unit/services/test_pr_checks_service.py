"""Unit tests for pr_checks_service.parse_rollup (spec 064)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import structlog
from pydantic import ValidationError

from coordinare.services.pr_checks_service import (
    CheckEntry,
    CheckRollup,
    PrChecksService,
    _BaselineFetchFailureTracker,
    parse_base_rollup,
    parse_rollup,
)


def _payload(
    *,
    base_ref: str = "main",
    pushed_date: str = "2026-05-16T11:00:00Z",
    contexts: list[dict] | None = None,
    bpr_nodes: list[dict] | None = None,
) -> dict:
    return {
        "repository": {
            "pullRequest": {
                "number": 1,
                "baseRefName": base_ref,
                "headRefOid": "abc1234",
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": "abc1234",
                                "pushedDate": pushed_date,
                                "statusCheckRollup": {
                                    "state": "PENDING",
                                    "contexts": {"nodes": contexts or []},
                                },
                            }
                        }
                    ]
                },
            },
            "branchProtectionRules": {"nodes": bpr_nodes} if bpr_nodes is not None else {"nodes": []},
        }
    }


def test_parses_check_run_node() -> None:
    payload = _payload(
        contexts=[
            {
                "__typename": "CheckRun",
                "name": "ci/test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "detailsUrl": "https://example/check/1",
            }
        ],
    )
    rollup = parse_rollup(payload, pr_number=1)
    assert len(rollup.checks) == 1
    entry = rollup.checks[0]
    assert entry.name == "ci/test"
    assert entry.status == "completed"
    assert entry.conclusion == "success"
    assert entry.details_url == "https://example/check/1"


# --- F1 (spec-090): CheckEntry failure text + GraphQL output{} ----------------


def test_check_entry_title_summary_default_to_none() -> None:
    """CheckEntry gains optional failure-text fields defaulting to None; the
    model stays frozen (spec-090 F1)."""
    entry = CheckEntry(name="ci/test", status="completed", conclusion="failure")
    assert entry.title is None
    assert entry.summary is None
    # frozen preserved
    with pytest.raises(ValidationError):
        entry.title = "x"  # type: ignore[misc]


def test_parse_rollup_populates_title_summary_from_check_run_output() -> None:
    """parse_rollup lifts title/summary from a CheckRun output{} block so the
    classifier (L2) and repair mandate (L3) can read the failure reason."""
    payload = _payload(
        contexts=[
            {
                "__typename": "CheckRun",
                "name": "ci/test",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "https://example/check/1",
                "output": {
                    "title": "3 tests failed",
                    "summary": "test_foo AssertionError: expected 1 got 2",
                },
            }
        ],
    )
    rollup = parse_rollup(payload, pr_number=1)
    entry = rollup.checks[0]
    assert entry.title == "3 tests failed"
    assert entry.summary == "test_foo AssertionError: expected 1 got 2"


def test_parse_rollup_check_run_without_output_yields_none() -> None:
    """A CheckRun lacking an output{} block (or with null fields) yields None
    for both title and summary — no crash."""
    payload = _payload(
        contexts=[
            {
                "__typename": "CheckRun",
                "name": "ci/test",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "__typename": "CheckRun",
                "name": "ci/other",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "output": {"title": None, "summary": None},
            },
        ],
    )
    rollup = parse_rollup(payload, pr_number=1)
    by_name = {c.name: c for c in rollup.checks}
    assert by_name["ci/test"].title is None
    assert by_name["ci/test"].summary is None
    assert by_name["ci/other"].title is None
    assert by_name["ci/other"].summary is None


def test_parse_rollup_status_context_has_no_title_summary() -> None:
    """Legacy StatusContext rows carry no output{} — title/summary stay None."""
    payload = _payload(
        contexts=[
            {
                "__typename": "StatusContext",
                "context": "buildkite/build",
                "state": "FAILURE",
                "targetUrl": "https://example/build/2",
            }
        ],
    )
    rollup = parse_rollup(payload, pr_number=1)
    assert rollup.checks[0].title is None
    assert rollup.checks[0].summary is None


# --- F2 (spec-090): base-branch rollup + rollup_origin + FR-027 tracker -------


def _base_payload(
    *,
    base_ref: str = "main",
    oid: str = "base123",
    contexts: list[dict] | None = None,
    bpr_nodes: list[dict] | None = None,
    include_target: bool = True,
) -> dict:
    """Build a Ref→target→Commit shaped payload for the base-branch query.

    This is intentionally a different shape from `_payload` (which is
    PR→commits→commit): the base rollup is fetched off a branch ref, not a PR.
    """
    target = (
        {
            "oid": oid,
            "pushedDate": "2026-05-10T09:00:00Z",
            "committedDate": "2026-05-10T09:00:00Z",
            "statusCheckRollup": {
                "state": "FAILURE",
                "contexts": {"nodes": contexts or []},
            },
        }
        if include_target
        else None
    )
    return {
        "repository": {
            "ref": {"target": target},
            "branchProtectionRules": {"nodes": bpr_nodes} if bpr_nodes is not None else {"nodes": []},
        }
    }


def test_check_rollup_rollup_origin_defaults_to_head() -> None:
    """CheckRollup gains rollup_origin defaulting to 'head'; frozen preserved."""
    rollup = CheckRollup(
        pr_number=1,
        head_sha="abc",
        head_pushed_at=datetime(2026, 5, 16, tzinfo=UTC),
        branch_protection_readable=True,
        checks=[],
    )
    assert rollup.rollup_origin == "head"
    with pytest.raises(ValidationError):
        rollup.rollup_origin = "base"  # type: ignore[misc]


def test_parse_base_rollup_stamps_base_origin() -> None:
    """parse_base_rollup stamps rollup_origin='base' with neutral pr_number=0 and
    head_pushed_at=None (the base is a long-lived branch — no push-age timeout
    applies), and resolves the base's OWN required set from branch protection."""
    payload = _base_payload(
        contexts=[
            {
                "__typename": "CheckRun",
                "name": "ci/test",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "https://example/check/9",
                "output": {"title": "boom", "summary": "test_x failed"},
            }
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    rollup = parse_base_rollup(payload, base_ref="main")
    assert rollup.rollup_origin == "base"
    assert rollup.pr_number == 0
    assert rollup.head_pushed_at is None
    assert rollup.base_ref == "main"
    assert rollup.head_sha == "base123"
    assert rollup.branch_protection_readable is True
    entry = rollup.checks[0]
    assert entry.name == "ci/test"
    assert entry.conclusion == "failure"
    assert entry.is_required is True  # base's own protection resolved
    assert entry.title == "boom"
    assert entry.summary == "test_x failed"


def test_parse_base_rollup_raises_on_missing_target() -> None:
    """A base ref with no target commit (deleted/ambiguous ref) raises ValueError
    so the service's fetch wrapper can fail-safe to None (FR-005)."""
    payload = _base_payload(include_target=False)
    with pytest.raises(ValueError, match="no target commit"):
        parse_base_rollup(payload, base_ref="main")


@pytest.mark.asyncio
async def test_get_base_branch_check_rollup_returns_none_on_error() -> None:
    """get_base_branch_check_rollup never raises — it fail-safes to None on fetch
    error, timeout, and empty/ambiguous base ref (FR-005)."""
    gh = type("GH", (), {})()
    svc = PrChecksService(gh, "o", "r")

    # Generic fetch error.
    gh._execute = AsyncMock(side_effect=Exception("boom"))
    assert await svc.get_base_branch_check_rollup("main") is None

    # Timeout.
    gh._execute = AsyncMock(side_effect=TimeoutError())
    assert await svc.get_base_branch_check_rollup("main") is None

    # Empty/ambiguous base ref — never even attempts a fetch.
    gh._execute = AsyncMock(side_effect=AssertionError("must not be called"))
    assert await svc.get_base_branch_check_rollup("") is None


@pytest.mark.asyncio
async def test_get_base_branch_check_rollup_parses_on_success() -> None:
    """Happy path: the service parses a base rollup into a base-origin CheckRollup."""
    gh = type("GH", (), {})()
    gh._execute = AsyncMock(
        return_value=_base_payload(
            contexts=[
                {
                    "__typename": "CheckRun",
                    "name": "ci/test",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                }
            ],
        )
    )
    svc = PrChecksService(gh, "o", "r")
    rollup = await svc.get_base_branch_check_rollup("main")
    assert rollup is not None
    assert rollup.rollup_origin == "base"
    assert rollup.head_pushed_at is None
    assert rollup.checks[0].conclusion == "failure"


def test_baseline_fetch_tracker_signals_only_at_threshold() -> None:
    """FR-027: the degraded signal fires only at ≥5 failures within the 1h window.
    Boundary: 4 → no signal, 5 → signal, 6 → signal."""
    clock = {"t": 0.0}
    tracker = _BaselineFetchFailureTracker("o", "r", clock=lambda: clock["t"])

    # 4 failures, all within the window → no signal.
    for _ in range(4):
        clock["t"] += 1.0
        with structlog.testing.capture_logs() as logs:
            degraded = tracker.record_failure()
        assert degraded is False
        assert not [r for r in logs if r["event"] == "baseline_fetch_degraded"]

    # 5th failure → signal.
    clock["t"] += 1.0
    with structlog.testing.capture_logs() as logs:
        degraded = tracker.record_failure()
    assert degraded is True
    signal = [r for r in logs if r["event"] == "baseline_fetch_degraded"]
    assert len(signal) == 1
    assert signal[0]["log_level"] == "error"
    assert signal[0]["owner"] == "o"
    assert signal[0]["repo"] == "r"

    # 6th failure → still signals.
    clock["t"] += 1.0
    with structlog.testing.capture_logs() as logs:
        degraded = tracker.record_failure()
    assert degraded is True
    assert len([r for r in logs if r["event"] == "baseline_fetch_degraded"]) == 1


def test_baseline_fetch_tracker_ages_out_old_failures() -> None:
    """Failures older than the 1-hour window stop counting, so a slow drip of
    failures never trips the degraded signal."""
    clock = {"t": 0.0}
    tracker = _BaselineFetchFailureTracker("o", "r", clock=lambda: clock["t"])

    # 4 failures at t=1..4.
    for _ in range(4):
        clock["t"] += 1.0
        assert tracker.record_failure() is False

    # Jump past the 1h window so the first 4 age out, then one fresh failure.
    clock["t"] += 3600.0 + 1.0
    with structlog.testing.capture_logs() as logs:
        degraded = tracker.record_failure()
    assert degraded is False  # only 1 failure remains inside the window
    assert not [r for r in logs if r["event"] == "baseline_fetch_degraded"]


def test_parses_status_context_success() -> None:
    payload = _payload(
        contexts=[
            {
                "__typename": "StatusContext",
                "context": "buildkite/build",
                "state": "SUCCESS",
                "targetUrl": "https://example/build/2",
            }
        ],
    )
    rollup = parse_rollup(payload, pr_number=1)
    assert rollup.checks[0].name == "buildkite/build"
    assert rollup.checks[0].status == "completed"
    assert rollup.checks[0].conclusion == "success"


def test_status_context_pending_maps_to_in_progress() -> None:
    payload = _payload(
        contexts=[{"__typename": "StatusContext", "context": "ci/legacy", "state": "PENDING"}],
    )
    rollup = parse_rollup(payload, pr_number=1)
    assert rollup.checks[0].status == "in_progress"
    assert rollup.checks[0].conclusion is None


def test_required_flag_derived_from_branch_protection() -> None:
    payload = _payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "optional", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    rollup = parse_rollup(payload, pr_number=1)
    required = {c.name: c.is_required for c in rollup.checks}
    assert required["ci/test"] is True
    assert required["optional"] is False
    assert rollup.branch_protection_readable is True


def test_branch_protection_unreadable_when_null() -> None:
    payload = _payload(contexts=[])
    payload["repository"]["branchProtectionRules"] = None
    rollup = parse_rollup(payload, pr_number=1)
    assert rollup.branch_protection_readable is False
    # All is_required should be False because we can't know which are required.
    assert all(c.is_required is False for c in rollup.checks)


def test_branch_protection_glob_pattern_matches() -> None:
    payload = _payload(
        base_ref="release/2026",
        contexts=[{"__typename": "CheckRun", "name": "ci/test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        bpr_nodes=[{"pattern": "release/*", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    rollup = parse_rollup(payload, pr_number=1)
    assert rollup.checks[0].is_required is True


def test_missing_pushed_date_raises() -> None:
    payload = _payload(pushed_date="")
    payload["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["pushedDate"] = None
    with pytest.raises(ValueError, match="pushedDate"):
        parse_rollup(payload, pr_number=1)


def test_missing_commits_raises() -> None:
    payload = _payload()
    payload["repository"]["pullRequest"]["commits"]["nodes"] = []
    with pytest.raises(ValueError, match="no HEAD commit"):
        parse_rollup(payload, pr_number=1)


def test_at_context_cap_flag_set_when_contexts_saturate() -> None:
    """When GraphQL returns the max 100 contexts, the cap flag must be set
    so the caller can surface the (potentially silent) truncation."""
    contexts = [
        {
            "__typename": "CheckRun",
            "name": f"ci/job-{i}",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }
        for i in range(100)
    ]
    rollup = parse_rollup(_payload(contexts=contexts), pr_number=1)
    assert rollup.at_context_cap is True
    assert len(rollup.checks) == 100


def test_at_context_cap_not_set_below_threshold() -> None:
    contexts = [
        {
            "__typename": "CheckRun",
            "name": f"ci/job-{i}",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }
        for i in range(5)
    ]
    rollup = parse_rollup(_payload(contexts=contexts), pr_number=1)
    assert rollup.at_context_cap is False


def test_naive_pushed_date_promoted_to_utc() -> None:
    """GitHub always returns offset-aware timestamps, but if a naive datetime
    ever sneaks through, parse_rollup must promote it to UTC to avoid
    TypeError on downstream tz-aware arithmetic in policy.decide()."""
    from datetime import UTC

    payload = _payload(pushed_date="2026-05-16T11:00:00")  # no Z, no offset
    rollup = parse_rollup(payload, pr_number=1)
    assert rollup.head_pushed_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_bpr_forbidden_resets_after_reprobe_interval(monkeypatch) -> None:
    """After the reprobe interval elapses, the service retries the full query
    so a token rotated to include admin:read is picked up without restart."""
    gh = type("GH", (), {})()
    gh._execute = AsyncMock(side_effect=Exception("FORBIDDEN: branchProtectionRules"))
    svc = PrChecksService(gh, "o", "r")

    fake_time = {"now": 1000.0}
    monkeypatch.setattr(
        "coordinare.services.pr_checks_service.time.monotonic",
        lambda: fake_time["now"],
    )

    # First call: full query raises FORBIDDEN, retry on _NO_BPR also raises.
    with pytest.raises(Exception, match="FORBIDDEN"):
        await svc.get_pr_check_rollup(1)
    assert svc._bpr_forbidden is True
    forbidden_at = svc._bpr_forbidden_at

    # Within the reprobe window — flag stays set.
    fake_time["now"] = forbidden_at + 60
    with pytest.raises(Exception, match="FORBIDDEN"):
        await svc.get_pr_check_rollup(2)
    assert svc._bpr_forbidden is True

    # Past the reprobe window — flag is cleared so the next call retries the
    # full query.
    fake_time["now"] = forbidden_at + PrChecksService._BPR_REPROBE_AFTER_SECONDS + 1
    with pytest.raises(Exception, match="FORBIDDEN"):
        await svc.get_pr_check_rollup(3)
    # It re-flipped back to True on this call because the underlying query
    # still raises FORBIDDEN, but the reset path was exercised: the flag was
    # cleared at the top of the call (driving us back to _ROLLUP_QUERY) and
    # only re-set when the exception handler ran.
    assert gh._execute.await_count >= 4  # at least one full-query retry post-reset
