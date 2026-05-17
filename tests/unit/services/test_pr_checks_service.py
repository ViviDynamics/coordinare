"""Unit tests for pr_checks_service.parse_rollup (spec 064)."""

from __future__ import annotations

import pytest

from coordinare.services.pr_checks_service import parse_rollup


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
