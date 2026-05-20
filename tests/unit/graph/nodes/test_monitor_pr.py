from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services.github import TransientGitHubError


class _GitHub:
    async def get_pr_reviews(self, pr_id: str):
        _ = pr_id
        return [
            {"author_login": "copilot[bot]", "state": "COMMENTED", "body": "bot"},
            {"author_login": "alice", "state": "APPROVED", "body": "ship"},
        ]


@pytest.mark.asyncio
async def test_monitor_pr_routes_to_merge_on_human_approval() -> None:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _GitHub()

    result = await monitor_pr(state)

    assert result["phase"] == "merging"


@pytest.mark.asyncio
async def test_monitor_pr_routes_to_idle_when_pr_node_id_missing() -> None:
    """Guard: missing pr_node_id must not propagate an empty string to the GitHub API."""
    state = initial_state()
    state["current_card"] = {}  # no pr_node_id key
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _GitHub()

    result = await monitor_pr(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_monitor_pr_routes_to_idle_when_pr_node_id_is_none() -> None:
    state = initial_state()
    state["current_card"] = {"pr_node_id": None}
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _GitHub()

    result = await monitor_pr(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_monitor_pr_idle_when_no_github() -> None:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}

    result = await monitor_pr(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_monitor_pr_idle_when_no_card() -> None:
    state = initial_state()
    state["github_service"] = _GitHub()

    result = await monitor_pr(state)

    assert result["phase"] == "idle"


class _GitHubChangesRequested:
    async def get_pr_reviews(self, pr_id: str):
        return [
            {"author_login": "alice", "state": "CHANGES_REQUESTED", "body": "fix tests"},
        ]


@pytest.mark.asyncio
async def test_monitor_pr_routes_to_relay_feedback_on_changes_requested() -> None:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _GitHubChangesRequested()

    result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"
    assert len(result["pending_reviews"]) == 1


class _GitHubNoReviews:
    async def get_pr_reviews(self, pr_id: str):
        return []


@pytest.mark.asyncio
async def test_monitor_pr_stays_monitoring_when_no_reviews() -> None:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _GitHubNoReviews()

    result = await monitor_pr(state)

    assert result["phase"] == "monitoring_pr"


class _GitHubTransientReviews:
    def __init__(self) -> None:
        self.calls = 0

    async def get_pr_reviews(self, pr_id: str):
        self.calls += 1
        raise TransientGitHubError("dns lookup failed")


@pytest.mark.asyncio
async def test_monitor_pr_defers_transient_get_reviews_failures() -> None:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}
    state["human_reviewers"] = ["alice"]
    gh = _GitHubTransientReviews()
    state["github_service"] = gh

    result = await monitor_pr(state)

    assert result["phase"] == "monitoring_pr"
    queue = result.get("github_retry_queue") or []
    assert any(entry.get("operation") == "monitor_pr" for entry in queue if isinstance(entry, dict))
    assert gh.calls == 1


@pytest.mark.asyncio
async def test_monitor_pr_skips_get_reviews_until_deferred_retry_is_due() -> None:
    from datetime import UTC, datetime, timedelta

    class _GitHubShouldNotCallReviews:
        async def get_pr_reviews(self, pr_id: str):
            raise AssertionError("get_pr_reviews should be deferred")

    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _GitHubShouldNotCallReviews()
    state["github_retry_queue"] = [
        {
            "operation": "monitor_pr",
            "attempt": 2,
            "retry_at": datetime.now(UTC) + timedelta(seconds=180),
            "error": "dns outage",
            "deferred_at": datetime.now(UTC),
        },
    ]

    result = await monitor_pr(state)

    assert result["phase"] == "monitoring_pr"


# ---------------------------------------------------------------------------
# Processed review ID filtering
# ---------------------------------------------------------------------------


class _GitHubTwoReviews:
    async def get_pr_reviews(self, pr_id: str):
        return [
            {"id": "R1", "author_login": "Jason733i", "state": "CHANGES_REQUESTED", "body": "fix", "submitted_at": "2026-04-11T00:00:00Z"},
            {"id": "R2", "author_login": "Jason733i", "state": "CHANGES_REQUESTED", "body": "more fixes", "submitted_at": "2026-04-11T00:00:00Z"},
        ]


@pytest.mark.asyncio
async def test_monitor_pr_filters_processed_review_ids() -> None:
    """Reviews already in processed_review_ids must not appear as actionable."""
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_123"}
    state["human_reviewers"] = ["Jason733i"]
    state["github_service"] = _GitHubTwoReviews()
    state["processed_review_ids"] = {"R1"}

    result = await monitor_pr(state)

    # Only R2 should be actionable
    assert result["phase"] == "relay_feedback"
    actionable_ids = {str(r.get("id")) for r in result["pending_reviews"]}
    assert "R1" not in actionable_ids
    assert "R2" in actionable_ids


class _GitHubOneActionableReview:
    async def get_pr_reviews(self, pr_id: str):
        return [
            {"id": "R_new", "author_login": "Jason733i", "state": "COMMENTED", "body": "please fix", "submitted_at": "2026-04-11T00:00:00Z"},
        ]


@pytest.mark.asyncio
async def test_monitor_pr_does_not_mark_processed_before_relay() -> None:
    """monitor_pr sets relay_feedback phase but does NOT mark IDs as processed.

    IDs are marked processed in classify_human_feedback (after relay succeeds)
    to avoid permanently skipping reviews if a crash happens between monitor_pr
    and the actual dispatch.
    """
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_123"}
    state["human_reviewers"] = ["Jason733i"]
    state["github_service"] = _GitHubOneActionableReview()
    state["processed_review_ids"] = set()

    result = await monitor_pr(state)

    assert result["phase"] == "relay_feedback"
    # IDs should NOT be in processed_review_ids yet — that happens in classify_human_feedback
    assert result.get("processed_review_ids") == set()


# ---------------------------------------------------------------------------
# Trusted bot approval must NOT trigger merge
# ---------------------------------------------------------------------------


class _GitHubTrustedBotApproval:
    async def get_pr_reviews(self, pr_id: str):
        return [
            {
                "id": "R1",
                "author_login": "copilot-pull-request-reviewer[bot]",
                "state": "APPROVED",
                "body": "Looks good",
                "submitted_at": "2026-04-12T00:00:00Z",
            },
        ]


@pytest.mark.asyncio
async def test_trusted_bot_approval_does_not_trigger_merge() -> None:
    """A trusted bot APPROVED review must not trigger the merge phase.

    Only HUMAN approvals trigger merge.  Trusted bots can provide feedback
    (COMMENTED / CHANGES_REQUESTED) but their APPROVED state is ignored for
    the merge decision.
    """
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_123"}
    state["human_reviewers"] = ["Jason733i"]
    state["trusted_bot_reviewers"] = ["copilot-pull-request-reviewer[bot]"]
    state["github_service"] = _GitHubTrustedBotApproval()

    result = await monitor_pr(state)

    assert result["phase"] == "monitoring_pr"


# ---------------------------------------------------------------------------
# 042 — pr_node_id recovery from issue→PR linkage
# ---------------------------------------------------------------------------


class _GitHubWithRecovery:
    """GitHub mock that supports find_pr_for_issue + records calls."""

    def __init__(self, recovered: dict[str, str] | None = None):
        self._recovered = recovered
        self.find_called_with: list[str] = []
        self.get_reviews_called_with: list[str] = []

    async def find_pr_for_issue(self, issue_node_id: str):
        self.find_called_with.append(issue_node_id)
        return self._recovered

    async def get_pr_reviews(self, pr_id: str):
        self.get_reviews_called_with.append(pr_id)
        return []


@pytest.mark.asyncio
async def test_monitor_pr_recovers_pr_node_id_from_issue_linkage() -> None:
    """042 regression: when state lacks pr_node_id but the card has an
    issue_id, monitor_pr must recover the PR via the issue→PR linkage and
    rehydrate the card so monitoring continues.  Without recovery, every
    poll fails with "Could not resolve to a node with the global id of 'None'"
    and trips the GitHub circuit breaker (as happened on PR #88)."""
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "issue_id": "I_kwDO_issue",
        "pr_node_id": None,  # missing — must be recovered
    }
    state["human_reviewers"] = ["alice"]
    gh = _GitHubWithRecovery(recovered={
        "pr_node_id": "PR_kwDO_recovered",
        "pr_url": "https://github.com/org/repo/pull/42",
    })
    state["github_service"] = gh

    result = await monitor_pr(state)

    assert gh.find_called_with == ["I_kwDO_issue"]
    # Recovery populated the card with both fields
    card = result["current_card"]
    assert card["pr_node_id"] == "PR_kwDO_recovered"
    assert card["pr_url"] == "https://github.com/org/repo/pull/42"
    # And then get_pr_reviews ran with the recovered ID, not "None"
    assert gh.get_reviews_called_with == ["PR_kwDO_recovered"]


@pytest.mark.asyncio
async def test_monitor_pr_recovers_when_pr_node_id_is_literal_string_none() -> None:
    """042: Snapshots written by the buggy serialiser may carry the literal
    string "None" for pr_node_id.  monitor_pr must treat that as missing
    (not as a valid node ID) and trigger recovery, otherwise we'd query
    GitHub with the string "None" and fail every cycle."""
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "issue_id": "I_kwDO_issue",
        "pr_node_id": "None",  # literal "None" from buggy snapshot
        "pr_url": "None",
    }
    state["human_reviewers"] = ["alice"]
    gh = _GitHubWithRecovery(recovered={
        "pr_node_id": "PR_kwDO_recovered",
        "pr_url": "https://github.com/org/repo/pull/42",
    })
    state["github_service"] = gh

    result = await monitor_pr(state)

    assert gh.find_called_with == ["I_kwDO_issue"]
    card = result["current_card"]
    assert card["pr_node_id"] == "PR_kwDO_recovered"
    # Crucially, get_pr_reviews must NOT have been called with "None"
    assert "None" not in gh.get_reviews_called_with


@pytest.mark.asyncio
async def test_monitor_pr_falls_back_to_idle_when_recovery_finds_no_pr() -> None:
    """042: If recovery can't find an OPEN PR for the issue, monitor_pr
    must go idle gracefully — never call get_pr_reviews with a bad ID."""
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "issue_id": "I_kwDO_issue",
        "pr_node_id": None,
    }
    state["human_reviewers"] = ["alice"]
    gh = _GitHubWithRecovery(recovered=None)  # nothing found
    state["github_service"] = gh

    result = await monitor_pr(state)

    assert gh.find_called_with == ["I_kwDO_issue"]
    assert gh.get_reviews_called_with == []
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_monitor_pr_skips_recovery_when_no_issue_id() -> None:
    """042: Without an issue_id we have nothing to look the PR up by — go
    idle without attempting recovery (don't call find_pr_for_issue with empty)."""
    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "pr_node_id": None,
        # no issue_id
    }
    state["human_reviewers"] = ["alice"]
    gh = _GitHubWithRecovery(recovered={"pr_node_id": "PR_x", "pr_url": "u"})
    state["github_service"] = gh

    result = await monitor_pr(state)

    assert gh.find_called_with == []  # never called
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_monitor_pr_recovery_swallows_github_errors() -> None:
    """042: If find_pr_for_issue raises, we must log and go idle — not
    crash the daemon."""
    class _Failing:
        async def find_pr_for_issue(self, issue_node_id: str):
            raise RuntimeError("github transient")
        async def get_pr_reviews(self, pr_id: str):
            raise AssertionError("must not be called when recovery failed")

    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "issue_id": "I_kwDO_issue",
        "pr_node_id": None,
    }
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _Failing()

    result = await monitor_pr(state)

    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_monitor_pr_recovery_defers_transient_errors() -> None:
    class _TransientRecovery:
        async def find_pr_for_issue(self, issue_node_id: str):
            raise TransientGitHubError("temporary failure in name resolution")

        async def get_pr_reviews(self, pr_id: str):
            raise AssertionError("must not be called when recovery deferred")

    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "issue_id": "I_kwDO_issue",
        "pr_node_id": None,
    }
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _TransientRecovery()

    result = await monitor_pr(state)

    assert result["phase"] == "monitoring_pr"
    queue = result.get("github_retry_queue") or []
    assert any(entry.get("operation") == "monitor_pr" for entry in queue if isinstance(entry, dict))


@pytest.mark.asyncio
async def test_monitor_pr_deferred_wait_when_operation_not_ready() -> None:
    """When monitor_pr github op is deferred and not yet due, node stays in monitoring_pr."""
    from datetime import UTC, datetime, timedelta


    class _NeverCalled:
        async def find_pr_for_issue(self, issue_node_id: str):
            raise AssertionError("must not be called when deferred")

        async def get_pr_reviews(self, pr_id: str):
            raise AssertionError("must not be called when deferred")

    state = initial_state()
    state["current_card"] = {
        "id": "card-1",
        "issue_id": "I_kwDO_issue",
        "pr_node_id": None,
    }
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _NeverCalled()
    # Pre-load a deferred entry that is not yet due
    state["github_retry_queue"] = [{
        "operation": "monitor_pr",
        "attempt": 1,
        "retry_at": datetime.now(UTC) + timedelta(seconds=300),
        "error": "dns",
        "deferred_at": datetime.now(UTC),
    }]

    result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_monitor_pr_non_transient_get_reviews_error_stays_monitoring() -> None:
    """Non-transient get_pr_reviews error: log and stay in monitoring_pr."""

    class _FailingReviews:
        async def get_pr_reviews(self, pr_id: str):
            raise RuntimeError("graphql validation error")

    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _FailingReviews()

    result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_monitor_pr_lifecycle_completed_at_as_string_sets_cutoff() -> None:
    """lifecycle_completed_at stored as ISO string is parsed into a cutoff datetime."""
    from datetime import UTC, datetime, timedelta

    cutoff_dt = datetime.now(UTC) - timedelta(hours=1)
    cutoff_str = cutoff_dt.isoformat().replace("+00:00", "Z")

    class _Reviews:
        async def get_pr_reviews(self, pr_id: str):
            return [
                {
                    "id": "R_old",
                    "author_login": "alice",
                    "state": "APPROVED",
                    "submitted_at": (cutoff_dt - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                },
                {
                    "id": "R_new",
                    "author_login": "alice",
                    "state": "COMMENTED",
                    "submitted_at": (cutoff_dt + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                },
            ]

    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _Reviews()
    state["lifecycle_completed_at"] = cutoff_str

    result = await monitor_pr(state)
    # Old APPROVED review is filtered out by cutoff, so no merge should happen
    assert result["phase"] != "merging"


@pytest.mark.asyncio
async def test_monitor_pr_cutoff_filters_old_reviews() -> None:
    """Reviews with submitted_at before the lifecycle cutoff are skipped."""
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(hours=1)

    class _OldApproval:
        async def get_pr_reviews(self, pr_id: str):
            return [{
                "id": "R1",
                "author_login": "alice",
                "state": "APPROVED",
                "submitted_at": (cutoff - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            }]

    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1"}
    state["human_reviewers"] = ["alice"]
    state["github_service"] = _OldApproval()
    state["lifecycle_completed_at"] = cutoff  # datetime directly (line 141 path)

    result = await monitor_pr(state)
    # Old approval filtered out — no merge
    assert result["phase"] != "merging"


# ---------------------------------------------------------------------------
# 065 Fix 8 — Re-gate PR checks while in monitoring_pr.
# ---------------------------------------------------------------------------


def _fix8_rollup_payload(*, contexts: list[dict], bpr_nodes: list[dict]) -> dict:
    from datetime import UTC, datetime

    pushed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "repository": {
            "pullRequest": {
                "number": 42,
                "baseRefName": "main",
                "headRefOid": "deadbeef",
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": "deadbeef",
                                "pushedDate": pushed,
                                "statusCheckRollup": {
                                    "state": "PENDING",
                                    "contexts": {"nodes": contexts},
                                },
                            }
                        }
                    ]
                },
            },
            "branchProtectionRules": {"nodes": bpr_nodes},
        }
    }


class _FixGitHub:
    """Mock github service for Fix 8 — supports gate query + reviews + move_card."""

    def __init__(self, rollup_payload: dict, reviews: list[dict] | None = None) -> None:
        self._payload = rollup_payload
        self._reviews = reviews or []
        self.move_calls: list[tuple[str, str]] = []

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))

    async def _execute(self, query: str, variables: dict) -> dict:
        return self._payload

    async def get_pr_reviews(self, pr_id: str):
        return self._reviews


def _fix8_state_with_gate(github: object, *, reviews_present: bool = False) -> dict:
    from coordinare.config import CloserPrChecksConfig

    state = initial_state()
    state["current_card"] = {
        "id": "ITEM_42",
        "pr_node_id": "PR_NODE_42",
        "pr_url": "https://github.com/org/repo/pull/42",
        "status": "IN_REVIEW",
    }
    state["github_service"] = github
    state["human_reviewers"] = ["alice"]

    class _Sym:
        closer_pr_checks = CloserPrChecksConfig()

    state["current_symphony"] = "default"
    state["symphony_configs"] = {"default": _Sym()}
    return state


@pytest.mark.asyncio
async def test_monitor_pr_fix8_bounces_to_implementer_on_failed_checks() -> None:
    """A red required check during IN_REVIEW bounces back to implementer
    instead of waiting for a human to review.
    """
    payload = _fix8_rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    gh = _FixGitHub(payload, reviews=[{"author_login": "alice", "state": "APPROVED"}])
    state = _fix8_state_with_gate(gh)

    result = await monitor_pr(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    relay = result.get("relay_feedback") or []
    assert relay and "ci/test" in relay[0]["body"]
    # Card moved back to IN_PROGRESS on the board
    assert ("ITEM_42", "IN_PROGRESS") in gh.move_calls
    assert result["current_card"]["status"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_monitor_pr_fix8_holds_when_required_checks_pending() -> None:
    """A pending required check during IN_REVIEW keeps the card in
    monitoring_pr (NOT monitoring_performer, which is the gate's native HOLD).
    """
    payload = _fix8_rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "IN_PROGRESS", "conclusion": None},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    gh = _FixGitHub(payload)
    state = _fix8_state_with_gate(gh)

    result = await monitor_pr(state)

    assert result["phase"] == "monitoring_pr"
    assert gh.move_calls == []


@pytest.mark.asyncio
async def test_monitor_pr_fix8_forwards_to_reviews_when_checks_pass() -> None:
    """Green required checks let the normal review-polling path run."""
    payload = _fix8_rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "ci/test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
        bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
    )
    gh = _FixGitHub(payload, reviews=[{"author_login": "alice", "state": "APPROVED"}])
    state = _fix8_state_with_gate(gh)

    result = await monitor_pr(state)

    # FORWARD → reviews evaluated → human approval routes to merging
    assert result["phase"] == "merging"
    assert gh.move_calls == []
