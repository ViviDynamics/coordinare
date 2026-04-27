from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from coordinare.services.github import PermanentGitHubError
from coordinare.services.rebase import repo_url_from_config

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def merge_pr(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
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
        state["open_questions"] = [
            f"Cannot check PR mergeability — GitHub rejected the request: {exc}",
        ]
        return state
    except Exception as exc:
        logger.warning("merge_pr.check_mergeability_failed", error=str(exc))
        state["phase"] = "merging"  # retry next cycle
        return state
    if not mergeability.get("mergeable", False):
        state["phase"] = "blocked"
        state["open_questions"] = ["PR has merge conflicts requiring human intervention."]
        return state

    try:
        merge_result = await github.squash_merge(pr_node_id)
    except PermanentGitHubError as exc:
        # 042: Most common case is the GitHub App lacking ruleset bypass
        # permission — "You're not authorized to push to this branch".
        # Looping forever spams the merge endpoint and produces no merge.
        # Mark blocked with the actual GitHub error so the operator can
        # add the App to the ruleset bypass list.
        logger.warning("merge_pr.squash_merge_permanent_error", error=str(exc))
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Cannot merge PR — GitHub rejected the merge request: {exc}",
        ]
        return state
    except Exception as exc:
        logger.warning("merge_pr.squash_merge_failed", error=str(exc))
        state["phase"] = "merging"  # retry next cycle
        return state
    merge_commit = merge_result.get("merge_commit")
    if isinstance(merge_commit, dict):
        oid = str(merge_commit.get("oid", ""))
        headline = str(merge_commit.get("messageHeadline", ""))
        state["commit_summary"] = f"{oid[:7]} {headline}".strip() if oid else (headline or None)
    else:
        state["commit_summary"] = None

    try:
        await github.move_card(str(card.get("id", "")), "DONE")
    except Exception as exc:
        logger.warning("merge_pr.move_card_failed", error=str(exc))
    card["previous_status"] = card.get("status", "IN_REVIEW")
    card["status"] = "DONE"
    state["current_card"] = card

    # 047: Trigger rebase round for all other in-flight branches after merge.
    # The new main HEAD is the merge commit OID we just received.
    merge_sha = ""
    if isinstance(merge_commit, dict):
        merge_sha = str(merge_commit.get("oid", ""))
    if merge_sha:
        state["last_known_main_sha"] = merge_sha
        all_sessions = state.get("active_sessions") or {}
        # Exclude the just-merged card so we don't rebase/force-push its
        # (now-merged) branch or fail on a deleted branch ref.
        merged_card_id = str(card.get("id", ""))
        active_sessions = {k: v for k, v in all_sessions.items() if k != merged_card_id}
        config = state.get("config")
        if active_sessions and config is not None:
            try:
                from coordinare.services.rebase import run_rebase_round
                repo_url = repo_url_from_config(config)
                # Get token from github_service (which owns the auth)
                import contextlib
                token = ""
                if github is not None and hasattr(github, "_current_token"):
                    with contextlib.suppress(Exception):
                        token = await github._current_token()
                if repo_url and token:
                    import contextlib
                    pr_number = 0
                    pr_url = str(card.get("pr_url") or "")
                    if pr_url and "/" in pr_url:
                        with contextlib.suppress(ValueError, IndexError):
                            pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
                    notification_svc = state.get("notification_service")
                    rr = await run_rebase_round(
                        active_sessions, merge_sha, repo_url, token,
                        trigger_pr_number=pr_number,
                        notification_service=notification_svc,
                        github=github,
                        human_reviewers=state.get("human_reviewers"),
                    )
                    state["last_rebase_round"] = rr.to_dict()
                    # US2: For any BLOCKED jobs (conflicts), set up the
                    # first one for performer-driven resolution.  The
                    # coordinare's normal dispatch_performer → monitor_performer
                    # pipeline handles the rest asynchronously.
                    from coordinare.models.rebase import RebaseOutcome
                    from coordinare.services.rebase import prepare_conflict_resolution
                    for job in rr.jobs:
                        if job.outcome == RebaseOutcome.BLOCKED:
                            # Route the BLOCKED card's session to the
                            # implementer with conflict details in relay_feedback.
                            session = active_sessions.get(job.card_id)
                            if isinstance(session, dict):
                                prepare_conflict_resolution(
                                    job, session,
                                    human_reviewers=state.get("human_reviewers"),
                                )
                            break  # one at a time — next round handles the rest
                    logger.info(
                        "merge_pr.rebase_round_complete",
                        summary=rr.summary,
                        job_count=len(rr.jobs),
                    )
            except Exception as exc:
                logger.warning("merge_pr.rebase_round_failed", error=str(exc))

    state["phase"] = "idle"
    return state
