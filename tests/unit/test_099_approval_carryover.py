from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from performer.workflows.documenter.models import DOC_PATH_PREFIXES as PERFORMER_DOC_PATH_PREFIXES
from structlog.testing import capture_logs

from coordinare.dashboard import DashboardStore
from coordinare.graph.nodes.monitor.verdict import _record_stage_verdict
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services.activity_log import ActivityLog
from coordinare.services.approval_carryover import CARRYOVER_CACHE_SECONDS
from coordinare.services.documentation_paths import DOC_PATH_PREFIXES
from coordinare.services.documenting_side import poll_to_completion
from coordinare.services.github import GitHubService
from coordinare.session import session_to_state, state_to_session
from coordinare.state_store import PersistedSession, StageVerdict

_APPROVED = "a" * 40
_MIDDLE = "b" * 40
_HEAD = "c" * 40
_OTHER = "d" * 40
_BOT = "coordinare[bot]"
_PR_URL = "https://github.com/acme/repo/pull/99"
_PAGE_SIZE = 100
_FILE_LIMIT = 300
_REPEATED_CYCLES = 3
_DISMISSED_REASON = "approval_dismissed_by_daemon_docs_push"
_DISMISSED_MESSAGE = (
    "The documenter's docs-only commit dismissed the approval; re-approve to merge, "
    "or disable 'dismiss stale reviews' for this repo"
)


def _commit(sha: str, parent: str, login: str | None = _BOT) -> dict[str, Any]:
    return {"sha": sha, "author": {"login": login} if login else None,
            "committer": {"login": _BOT}, "parents": [{"sha": parent}]}


def _state(commits: list[dict[str, Any]]) -> dict[str, Any]:
    state = initial_state()
    state["current_card"] = {"id": "CARD_99", "pr_node_id": "PR_99", "pr_url": _PR_URL}
    state["human_reviewers"] = ["alice"]
    state["stage_verdicts"] = {"documenting": {
        "head_sha": _HEAD, "head_before": _APPROVED, "verdict": "docs_committed",
        "recorded_at": "2026-10-15T10:05:00Z",
    }}
    github = AsyncMock(spec=GitHubService)
    github.get_pr_review_context.return_value = {
        "reviews": [{"id": "R_99", "author_login": "alice", "state": "APPROVED",
                     "commit_oid": _APPROVED, "submitted_at": "2026-10-15T10:00:00Z"}],
        "head_oid": _HEAD, "review_threads": [], "review_decision": "APPROVED", "state": "OPEN",
    }
    github.self_logins.return_value = frozenset({_BOT})
    github.compare_commits = AsyncMock(return_value=commits)
    github.compare_changed_files.return_value = ["docs/guide.md"]
    state["github_service"] = github
    return state


@pytest.fixture(autouse=True)
def _disable_unrelated_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate",
                        AsyncMock(return_value=({}, False)))
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_baseline_prevention_gate",
                        AsyncMock(return_value=({}, False)))
    monkeypatch.setattr("coordinare.graph.nodes.monitor_pr.poll_pr_conversation_feedback",
                        AsyncMock(return_value=[]))


@pytest.mark.asyncio
async def test_daemon_only_commits_merge_with_explicit_reason() -> None:
    state = _state([_commit(_MIDDLE, _APPROVED), _commit(_HEAD, _MIDDLE)])
    with capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "merging"
    assert result["pending_reviews"] == []
    decisions = [entry for entry in logs if entry["event"] == "monitor_pr.merge_decision"]
    assert decisions[-1]["reason"] == "approval_carried_over_daemon_commits"
    state["github_service"].compare_commits.assert_awaited_once_with(_PR_URL, _APPROVED, _HEAD)


@pytest.mark.asyncio
@pytest.mark.parametrize("commits", [
    [_commit(_HEAD, _APPROVED, "alice")],
    [_commit(_MIDDLE, _APPROVED, "alice"), _commit(_HEAD, _MIDDLE)],
    [_commit(_MIDDLE, _APPROVED), _commit(_HEAD, _MIDDLE, "another[bot]")],
    [_commit(_HEAD, _APPROVED, None)],
])
async def test_human_mixed_other_and_unknown_authors_wait(commits: list[dict[str, Any]]) -> None:
    state = _state(commits)
    with capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    decisions = [entry for entry in logs if entry["event"] == "monitor_pr.merge_decision"]
    assert decisions[-1]["reason"] == "approval_predates_head"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["compare_commits", "self_logins"])
async def test_api_failure_waits(failure: str) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    getattr(state["github_service"], failure).side_effect = RuntimeError("unavailable")
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["implementing", "reviewing", "qa", "closing_review", "unknown"])
async def test_daemon_identity_requires_allowed_stage(stage: str) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["stage_verdicts"] = {stage: {"head_sha": _HEAD, "verdict": "approved"}}
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_unrecorded_daemon_commit_before_documenter_waits() -> None:
    state = _state([_commit(_MIDDLE, _APPROVED), _commit(_HEAD, _MIDDLE)])
    del state["stage_verdicts"]["documenting"]["head_before"]
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_side_run_and_final_documenter_commits_merge() -> None:
    state = _state([_commit(_MIDDLE, _APPROVED), _commit(_HEAD, _MIDDLE)])
    del state["stage_verdicts"]["documenting"]["head_before"]
    state["documenting_side"] = {"status": "done", "head_sha": _MIDDLE}
    assert (await monitor_pr(state))["phase"] == "merging"


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [None, {"status": "failed", "head_sha": _HEAD},
                                   {"status": "running", "head_sha": _HEAD}])
async def test_missing_or_unsuccessful_stage_record_waits(record: dict[str, Any] | None) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["stage_verdicts"] = {}
    state["documenting_side"] = record
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_unresolved_daemon_identity_waits() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["github_service"].self_logins.return_value = frozenset()
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_current_head_approval_uses_no_compare() -> None:
    state = _state([])
    state["github_service"].get_pr_review_context.return_value["reviews"][0]["commit_oid"] = _HEAD
    assert (await monitor_pr(state))["phase"] == "merging"
    state["github_service"].compare_commits.assert_not_awaited()


def test_stage_commit_range_is_recorded_and_persisted() -> None:
    state = initial_state()
    state["performer_stage"] = "documenting"
    _record_stage_verdict(state, "docs_committed", {"head_before": _APPROVED, "head_after": _HEAD})
    record = StageVerdict.model_validate(state["stage_verdicts"]["documenting"])
    assert record.head_before == _APPROVED
    session = PersistedSession(card_id="CARD_99", stage_verdicts={"documenting": record})
    restored = PersistedSession.model_validate_json(session.model_dump_json())
    assert restored.stage_verdicts["documenting"].head_before == _APPROVED


def test_workflow_documenter_commit_is_recorded() -> None:
    state = initial_state()
    state["performer_stage"] = "documenting"
    _record_stage_verdict(state, "docs_committed", {"report": {"docs": {"commit_sha": _HEAD}}})
    assert state["stage_verdicts"]["documenting"]["head_sha"] == _HEAD


def _comparison(entries: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    return {"status": "ahead", "merge_base_commit": {"sha": _APPROVED},
            "total_commits": len(entries), "commits": entries, **overrides}


def _mock_http(monkeypatch: pytest.MonkeyPatch, pages: list[Any]) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.side_effect = [httpx.Response(200, json=page) for page in pages]
    monkeypatch.setattr("coordinare.services.github.httpx.AsyncClient", lambda **kwargs: client)
    return client


@pytest.mark.asyncio
async def test_compare_commits_uses_sha_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    commits = [_commit(_HEAD, _APPROVED)]
    client = _mock_http(monkeypatch, [_comparison(commits)])
    github = GitHubService(token="test-token", org="acme", project_number=1)
    assert await github.compare_commits(_PR_URL, _APPROVED, _HEAD) == commits
    assert client.get.call_args.args[0].endswith(f"/repos/acme/repo/compare/{_APPROVED}...{_HEAD}")


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"status": "diverged", "merge_base_commit": {"sha": _OTHER}},
    {"status": "behind"}, {"status": "identical"},
    {"merge_base_commit": {"sha": _OTHER}}, {"merge_base_commit": None},
    {"total_commits": 2}, {"commits": []}, {"commits": None},
    {"commits": [_commit(_MIDDLE, _APPROVED)]},
])
async def test_rewritten_or_incomplete_comparison_waits(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any],
) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    github = GitHubService(token="test-token", org="acme", project_number=1)
    _mock_http(monkeypatch, [_comparison([_commit(_HEAD, _APPROVED)], **overrides)])
    state["github_service"].compare_commits.side_effect = github.compare_commits
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_compare_commits_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    first = [_commit(f"{index:040x}", _APPROVED) for index in range(_PAGE_SIZE)]
    last = [_commit(_HEAD, first[-1]["sha"])]
    total = len(first) + len(last)
    client = _mock_http(monkeypatch, [_comparison(first, total_commits=total),
                                     _comparison(last, total_commits=total)])
    github = GitHubService(token="test-token", org="acme", project_number=1)
    assert await github.compare_commits(_PR_URL, _APPROVED, _HEAD) == first + last
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_single_documenter_commit_with_legacy_record_merges() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    del state["stage_verdicts"]["documenting"]["head_before"]
    assert (await monitor_pr(state))["phase"] == "merging"


@pytest.mark.asyncio
async def test_withdrawn_approval_cannot_be_carried_over() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["github_service"].get_pr_review_context.return_value["reviews"].append({
        "id": "R_CHANGE", "author_login": "alice", "state": "CHANGES_REQUESTED",
        "commit_oid": _HEAD, "submitted_at": "2026-10-15T10:06:00Z",
    })
    state["processed_review_ids"] = {"R_CHANGE"}
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"
    state["github_service"].compare_commits.assert_not_awaited()


@pytest.mark.asyncio
async def test_actionable_feedback_still_precedes_carried_approval() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["human_reviewers"].append("bob")
    state["github_service"].get_pr_review_context.return_value["reviews"].append({
        "id": "R_COMMENT", "author_login": "bob", "state": "COMMENTED", "body": "Please clarify",
        "commit_oid": _HEAD, "submitted_at": "2026-10-15T10:06:00Z",
    })
    result = await monitor_pr(state)
    assert result["phase"] == "relay_feedback"
    assert [review["id"] for review in result["pending_reviews"]] == ["R_COMMENT"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parents", [[], [{"sha": _MIDDLE}, {"sha": _OTHER}], [{"sha": None}]])
async def test_ambiguous_stage_range_waits(parents: list[dict[str, Any]]) -> None:
    head = _commit(_HEAD, _MIDDLE)
    head["parents"] = parents
    state = _state([_commit(_MIDDLE, _APPROVED), head])
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_unsuccessful_documenting_verdict_waits() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["stage_verdicts"]["documenting"]["verdict"] = "error"
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_compare_http_error_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    github = GitHubService(token="test-token", org="acme", project_number=1)
    client = _mock_http(monkeypatch, [])
    client.get.side_effect = None
    client.get.return_value = httpx.Response(503)
    state["github_service"].compare_commits.side_effect = github.compare_commits
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_compare_duplicate_commit_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    github = GitHubService(token="test-token", org="acme", project_number=1)
    _mock_http(monkeypatch, [_comparison([_commit(_HEAD, _APPROVED)] * 2)])
    state["github_service"].compare_commits.side_effect = github.compare_commits
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_compare_page_cap_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    github = GitHubService(token="test-token", org="acme", project_number=1)
    page_count = 3
    total = _PAGE_SIZE * page_count + 1
    pages = [
        _comparison([_commit(f"{offset + index:040x}", _APPROVED) for index in range(_PAGE_SIZE)],
                    total_commits=total)
        for offset in range(0, _PAGE_SIZE * page_count, _PAGE_SIZE)
    ]
    _mock_http(monkeypatch, pages)
    state["github_service"].compare_commits.side_effect = github.compare_commits
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_documenter_record_without_head_movement_cannot_claim_another_push() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["stage_verdicts"]["documenting"]["head_before"] = _HEAD
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [["docs/guide.md", "src/app.py"], ["README.md"],
                                  ["docs/../src/app.py"], [], None])
async def test_099_non_documentation_or_unknown_paths_wait(paths: list[str] | None) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["github_service"].compare_changed_files.return_value = paths
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [["docs/guide.md"], ["doc/guide.md", "docs/wiki/index.md"]])
async def test_099_documentation_paths_carry(paths: list[str]) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["github_service"].compare_changed_files.return_value = paths
    assert (await monitor_pr(state))["phase"] == "merging"
    state["github_service"].compare_changed_files.assert_awaited_once_with(_PR_URL, _APPROVED, _HEAD)


@pytest.mark.asyncio
async def test_099_unreadable_files_wait() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["github_service"].compare_changed_files.side_effect = RuntimeError("truncated files")
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
@pytest.mark.parametrize("paths", [[{}], [{"filename": None}], [{"filename": "docs/a.md"}] * _FILE_LIMIT])
async def test_099_compare_unknown_or_capped_files_wait(
    monkeypatch: pytest.MonkeyPatch, paths: list[dict[str, Any]],
) -> None:
    github = GitHubService(token="test-token", org="acme", project_number=1)
    _mock_http(monkeypatch, [{"files": paths}])
    with pytest.raises(RuntimeError):
        await github.compare_changed_files(_PR_URL, _APPROVED, _HEAD)


@pytest.mark.asyncio
async def test_099_compare_includes_rename_source(monkeypatch: pytest.MonkeyPatch) -> None:
    github = GitHubService(token="test-token", org="acme", project_number=1)
    _mock_http(monkeypatch, [{"files": [{"filename": "docs/app.py", "previous_filename": "src/app.py"}]}])
    assert set(await github.compare_changed_files(_PR_URL, _APPROVED, _HEAD)) == {"docs/app.py", "src/app.py"}


def test_099_documentation_prefixes_match_documenter() -> None:
    assert DOC_PATH_PREFIXES == PERFORMER_DOC_PATH_PREFIXES


@pytest.mark.asyncio
@pytest.mark.parametrize("review_state, rule", [
    ("DISMISSED", "dismiss_stale_reviews_on_push"),
    ("APPROVED", "require_extra_approval_for_unattributed_changes"),
])
async def test_099_docs_push_rule_wait_is_actionable_and_once_per_head(review_state: str, rule: str) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    github = state["github_service"]
    github.get_pr_review_context.return_value["reviews"][0]["state"] = review_state
    state["merge_review_rules"] = {rule: True}
    activity = ActivityLog()
    state["activity_log"] = activity
    notifier = AsyncMock()
    state["notification_service"] = notifier
    for _ in range(_REPEATED_CYCLES):
        result = await monitor_pr(state)
        assert result["phase"] == "monitoring_pr"
        assert result["review_wait_reason"] == _DISMISSED_REASON
        assert result["review_wait_detail"] == _DISMISSED_MESSAGE
    entries = [entry for entry in activity.snapshot() if entry["activity_type"] == _DISMISSED_REASON]
    assert len(entries) == 1
    assert entries[0]["text"] == _DISMISSED_MESSAGE
    notifier.dispatch.assert_awaited_once()
    assert notifier.dispatch.call_args.args[0].payload["message"] == _DISMISSED_MESSAGE
    github.get_branch_protection_sources.assert_not_awaited()
    github.compare_commits.assert_awaited_once()
    github.compare_changed_files.assert_awaited_once()
    github.self_logins.assert_awaited_once()
    github.get_pr_review_context.return_value["head_oid"] = _OTHER
    github.compare_commits.return_value = [_commit(_OTHER, _APPROVED)]
    state["stage_verdicts"]["documenting"]["head_sha"] = _OTHER
    await monitor_pr(state)
    assert notifier.dispatch.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("paths, recorded", [(["src/app.py"], True), (["docs/guide.md"], False)])
async def test_099_dismissed_review_without_proven_docs_push_is_not_misattributed(
    paths: list[str], recorded: bool,
) -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["merge_review_rules"] = {"dismiss_stale_reviews_on_push": True}
    state["github_service"].get_pr_review_context.return_value["reviews"][0]["state"] = "DISMISSED"
    state["github_service"].compare_changed_files.return_value = paths
    if not recorded:
        state["stage_verdicts"] = {}
    result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    assert result["review_wait_reason"] != _DISMISSED_REASON


@pytest.mark.asyncio
async def test_099_reapproval_clears_docs_push_wait() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["merge_review_rules"] = {"dismiss_stale_reviews_on_push": True}
    review = state["github_service"].get_pr_review_context.return_value["reviews"][0]
    review["state"] = "DISMISSED"
    assert (await monitor_pr(state))["review_wait_reason"] == _DISMISSED_REASON
    review["state"] = "APPROVED"
    review["commit_oid"] = _HEAD
    result = await monitor_pr(state)
    assert result["phase"] == "merging"
    assert result.get("review_wait_detail") is None


@pytest.mark.asyncio
async def test_099_unverified_evaluation_cached_and_logged_once_per_head() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    state["github_service"].compare_commits.side_effect = RuntimeError("unavailable")
    with capture_logs() as logs:
        for _ in range(_REPEATED_CYCLES):
            assert (await monitor_pr(state))["phase"] == "monitoring_pr"
    assert len([entry for entry in logs if entry["event"] == "monitor_pr.approval_carryover_unverified"]) == 1
    state["github_service"].compare_commits.assert_awaited_once()
    state["github_service"].self_logins.assert_awaited_once()


@pytest.mark.asyncio
async def test_099_side_run_records_whole_commit_segment() -> None:
    state = _state([_commit(_MIDDLE, _APPROVED), _commit(_HEAD, _MIDDLE)])
    state["stage_verdicts"] = {}
    check = AsyncMock(return_value={"status": "docs_committed", "head_before": _APPROVED, "head_sha": _HEAD})
    await poll_to_completion(state, check, session_id="docs")
    assert state["documenting_side"]["head_before"] == _APPROVED
    assert (await monitor_pr(state))["phase"] == "merging"


def test_099_carryover_cache_and_notification_markers_follow_the_card_session() -> None:
    state = _state([])
    state["phase"] = "monitoring_pr"
    state["approval_carryover_cache"] = {(_APPROVED, _HEAD): (0.0, False)}
    state["approval_carryover_unverified_head"] = _HEAD
    state["approval_docs_notified_head"] = _HEAD
    state["review_wait_detail"] = _DISMISSED_MESSAGE
    session = state_to_session(state)
    fresh = initial_state()
    session_to_state(session, fresh)
    for key in ("approval_carryover_cache", "approval_carryover_unverified_head", "approval_docs_notified_head", "review_wait_detail"):
        assert fresh[key] == state[key]
    session_to_state({}, fresh)
    assert not fresh.get("approval_carryover_cache")
    assert not fresh.get("approval_docs_notified_head")


@pytest.mark.asyncio
async def test_099_cached_failure_expires_without_repeating_warning() -> None:
    state = _state([_commit(_HEAD, _APPROVED)])
    github = state["github_service"]
    github.compare_changed_files.side_effect = RuntimeError("unavailable")
    with capture_logs() as logs:
        await monitor_pr(state)
        first_at, verified = state["approval_carryover_cache"][(_APPROVED, _HEAD)]
        state["approval_carryover_cache"][(_APPROVED, _HEAD)] = (first_at - CARRYOVER_CACHE_SECONDS, verified)
        await monitor_pr(state)
    assert github.compare_changed_files.await_count == 2
    assert len([entry for entry in logs if entry["event"] == "monitor_pr.approval_carryover_unverified"]) == 1
    github.compare_changed_files.side_effect = None
    first_at, verified = state["approval_carryover_cache"][(_APPROVED, _HEAD)]
    state["approval_carryover_cache"][(_APPROVED, _HEAD)] = (first_at - CARRYOVER_CACHE_SECONDS, verified)
    assert (await monitor_pr(state))["phase"] == "merging"


def test_099_dashboard_session_exposes_docs_approval_wait() -> None:
    daemon = SimpleNamespace(state={})
    session = {"phase": "monitoring_pr", "review_wait_reason": _DISMISSED_REASON,
               "review_wait_detail": _DISMISSED_MESSAGE}
    summary = DashboardStore._session_summary(daemon, "CARD_99", session)
    assert summary["review_wait_reason"] == _DISMISSED_REASON
    assert summary["review_wait_detail"] == _DISMISSED_MESSAGE
    before = DashboardStore._active_sessions_fingerprint(SimpleNamespace(state={"active_sessions": {"CARD_99": {"phase": "monitoring_pr"}}}))
    after = DashboardStore._active_sessions_fingerprint(SimpleNamespace(state={"active_sessions": {"CARD_99": session}}))
    assert before != after


@pytest.mark.asyncio
async def test_099_compare_rename_without_known_source_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    github = GitHubService(token="test-token", org="acme", project_number=1)
    _mock_http(monkeypatch, [{"files": [{"filename": "docs/app.py", "status": "renamed"}]}])
    with pytest.raises(RuntimeError):
        await github.compare_changed_files(_PR_URL, _APPROVED, _HEAD)
