from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.nodes.github_retry import (
    clear_deferred_github_operation,
    defer_github_operation,
    github_operation_ready,
)
from coordinare.graph.nodes.monitor_pr import _record_review_wait
from coordinare.graph.state import _set_current_card
from coordinare.lib.redaction import redact_state_value
from coordinare.metrics import METRICS
from coordinare.services.attempt_telemetry import close_attempt
from coordinare.services.board_provider import board_of, move_card_or_warn
from coordinare.services.card_lifecycle import card_id_of, record_lifecycle, stage_of
from coordinare.services.defunct_branch import (
    configured_repo_of,
    delete_defunct_branch,
    resolve_branch,
)
from coordinare.services.github import PermanentGitHubError
from coordinare.services.orphan_pr import close_guard, reconcile_completed_card
from coordinare.services.pipeline_budget import REVIEW_WAIT_AUTOMATION
from coordinare.services.rebase import (
    _live_pr_allows_rebase,
    fetch_main_sha,
    implementer_rebase_enabled,
    request_implementer_rebase,
    should_attempt_rebase,
    side_writer_active,
)

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def _wait_for_base_update(
    state: CoordinareState, github: Any, head: str, branch: str,
) -> None:
    """Back off rejected merges and hand eligible branches to the 555 implementer."""
    card = state.get("current_card") or {}
    card_id = str(card.get("id") or "")
    detail = "PR head is behind the base branch; waiting for an implementer rebase or a branch update."
    defer_github_operation(
        state, operation=f"merge_pr:{card.get('pr_node_id', '')}:{head}", error=RuntimeError(detail),
    )
    state["phase"] = "monitoring_pr"
    _record_review_wait(state, card_id, REVIEW_WAIT_AUTOMATION, reason="head_behind_base", head=head)
    state["review_wait_detail"] = detail
    if not branch or not implementer_rebase_enabled(state.get("config")) or side_writer_active(state):
        return
    main_sha = await fetch_main_sha(github)
    if not main_sha or not should_attempt_rebase(state, main_sha, head):
        return
    if await _live_pr_allows_rebase(state, github, card_id):
        request_implementer_rebase(
            state, card_id=card_id, branch=branch, main_sha=main_sha, head_sha=head,
            conflicting=False, reason="head_behind_base",
        )


async def merge_pr(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    board_provider = board_of(state)
    card = state.get("current_card")
    if github is None or not isinstance(card, dict):
        state["phase"] = "idle"
        return state

    pr_node_id = str(card.get("pr_node_id", ""))
    try:
        mergeability = await github.check_mergeability(pr_node_id)
    except PermanentGitHubError as exc:
        # 042: Permanent error means GitHub will reject this call identically
        # on every retry (e.g., bad node id, missing permission).  Stop the
        # retry loop and surface to the operator via blocked phase.
        logger.warning("merge_pr.check_mergeability_permanent_error", error=str(exc))
        state["phase"] = "blocked"
        state["open_questions"] = redact_state_value([
            f"Cannot check PR mergeability — GitHub rejected the request: {exc}",
        ])
        return state
    except Exception as exc:
        logger.warning("merge_pr.check_mergeability_failed", error=str(exc))
        state["phase"] = "merging"  # retry next cycle
        return state
    head = str(mergeability.get("head_ref_oid") or "")
    decision_head = str(state.get("review_wait_head") or head)
    if head and decision_head and head != decision_head:
        logger.info("merge_pr.head_changed", expected_head=decision_head, head=head)
        state["phase"] = "monitoring_pr"
        return state
    if not mergeability.get("mergeable", False):
        state["phase"] = "blocked"
        # 495: check_mergeability collapses conflicts and the review gate into
        # one boolean. Distinguish them so the operator sees the real cause.
        mergeable_raw = str(mergeability.get("mergeable_raw", "")).upper()
        review_decision = str(mergeability.get("review_decision", ""))
        if mergeable_raw != "MERGEABLE":
            state["open_questions"] = redact_state_value(["PR has merge conflicts requiring human intervention."])
        elif review_decision:
            state["open_questions"] = redact_state_value([
                f"PR is mergeable but GitHub's review decision is {review_decision}, "
                "not APPROVED — the review gate is blocking the merge.",
            ])
        else:
            state["open_questions"] = redact_state_value([
                "PR is mergeable but GitHub reports no review decision (reviewDecision "
                "is null). A null reviewDecision with an APPROVED review on the head "
                "usually means branch protection requires 0 approving reviews, so the "
                "approval cannot be counted.",
            ])
        return state

    if not decision_head:
        logger.info("merge_pr.head_unreadable")
        state["phase"] = "monitoring_pr"
        return state
    operation = f"merge_pr:{pr_node_id}:{decision_head}"
    ready, _ = github_operation_ready(state, operation)
    if not ready:
        state["phase"] = "monitoring_pr"
        return state
    try:
        parameters = inspect.signature(github.squash_merge).parameters
        if "expected_head_oid" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        ):
            merge_result = await github.squash_merge(pr_node_id, expected_head_oid=decision_head)
        else:
            merge_result = await github.squash_merge(pr_node_id)
    except PermanentGitHubError as exc:
        # 042: Most common case is the GitHub App lacking ruleset bypass
        # permission — "You're not authorized to push to this branch".
        # Looping forever spams the merge endpoint and produces no merge.
        # Mark blocked with the actual GitHub error so the operator can
        # add the App to the ruleset bypass list.
        logger.warning("merge_pr.squash_merge_permanent_error", error=str(exc))
        state["phase"] = "blocked"
        state["open_questions"] = redact_state_value([
            f"Cannot merge PR — GitHub rejected the merge request: {exc}",
        ])
        return state
    except Exception as exc:
        logger.warning("merge_pr.squash_merge_failed", error=str(exc))
        state["phase"] = "merging"  # retry next cycle
        return state
    if merge_result.get("merged") is not True:
        logger.info("merge_pr.merge_unconfirmed", reason=merge_result.get("reason", "not_merged"))
        state["phase"] = "monitoring_pr"
        if merge_result.get("reason") == "head_behind_base":
            await _wait_for_base_update(
                state, github, decision_head,
                resolve_branch(mergeability.get("head_ref_name"), state.get("workspace_branch"), card.get("pushed_branch")),
            )
        return state
    clear_deferred_github_operation(state, operation)
    merge_commit = merge_result.get("merge_commit")
    if isinstance(merge_commit, dict):
        oid = str(merge_commit.get("oid", ""))
        headline = str(merge_commit.get("messageHeadline", ""))
        state["commit_summary"] = f"{oid[:7]} {headline}".strip() if oid else (headline or None)
    else:
        state["commit_summary"] = None

    move_ok = True
    try:
        move_ok = await move_card_or_warn(board_provider, str(card.get("id", "")), "DONE")
    except Exception as exc:
        logger.warning("merge_pr.move_card_failed", error=str(exc))
        move_ok = False
    card["previous_status"] = card.get("status", "IN_REVIEW")
    card["status"] = "DONE"
    _set_current_card(state, card)

    # 553 — opt-in completion reconciliation: close other open PRs that
    # still point at this card. Only meaningful when the merge actually
    # landed (squash_merge can report merged=False without raising) and the
    # board transition succeeded — otherwise the card is not really terminal
    # and its PRs stay protected (FR-006). The disabled path performs no
    # GitHub reads. The close guard serializes against the concurrent
    # per-session sweeps.
    if move_ok:
        # 89 — the merged PR's head branch is defunct now. Delete it when the
        # operator opted in; resolution prefers the live head_ref_name from
        # check_mergeability and falls back to the session's pushed_branch
        # artefact. Non-raising, decision-logged (defunct_branch.deleted).
        # The head-repo identity check refuses fork PRs; branch_has_open_pr
        # preserves a branch that still backs another live PR.
        if bool(getattr(state.get("config"), "delete_defunct_branches", False)):
            await delete_defunct_branch(
                github,
                resolve_branch(
                    mergeability.get("head_ref_name"),
                    card.get("pushed_branch"),
                ),
                card_id=str(card.get("id", "")),
                source="merge",
                enabled=True,
                head_repo=str(mergeability.get("head_repo_name_with_owner") or ""),
                expected_repo=configured_repo_of(github),
            )
        async with close_guard():
            await reconcile_completed_card(github, state.get("config"), card)

    # 047/555: the new main HEAD is the merge commit OID we just received.
    # Record it; coordinare never rebases the other in-flight branches. The
    # check_board divergence sweep hands any that now conflict to the implementer.
    if isinstance(merge_commit, dict) and merge_commit.get("oid"):
        state["last_known_main_sha"] = str(merge_commit["oid"])

    symphony = state.get("symphony_name", "__default__")
    METRICS.cards_outcome_total.labels(symphony=symphony, outcome="merged").inc()

    close_attempt(state, "pass", "human", "merged")
    # 147: the merge ends the card's timeline. Recorded once, from the card the merge closed.
    record_lifecycle(state, "merged", card_id=card_id_of(state), stage=stage_of(state), key="merged")

    state["phase"] = "idle"
    return state
