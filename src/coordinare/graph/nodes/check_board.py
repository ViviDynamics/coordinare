from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from coordinare.lib.acceptance_criteria import parse_acceptance_criteria
from coordinare.models.dependency import DependencyStatus
from coordinare.services.dependency import build_graph, resolve_off_board_dependencies
from coordinare.services.dependency import filter_eligible_todo as _dep_filter
from coordinare.session import create_session_from_card

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


def _sort_by_priority(
    item_ids: list[str],
    item_field_values: dict[str, dict[str, str]],
    field_name: str,
    priority_order: list[str],
) -> list[str]:
    """Sort *item_ids* by their priority field value.

    - When *priority_order* is set, values rank by list index (lower = higher priority).
      Values not in *priority_order* sort after all listed values.
    - When *priority_order* is empty, values are compared lexicographically (ascending).
    - Items without a priority value (None/missing) sort after items that have one.
    - Stable sort: ties preserve the original board position order.
    """
    order_map = {v: i for i, v in enumerate(priority_order)} if priority_order else {}
    max_rank = len(priority_order)  # rank for unlisted values

    def _sort_key(item_id: str) -> tuple[int, int | str]:
        fields = item_field_values.get(item_id, {})
        value = fields.get(field_name)
        if value is None:
            # No value → sort last (after everything)
            return (1, "")
        if order_map:
            return (0, order_map.get(value, max_rank))
        return (0, value)

    return sorted(item_ids, key=_sort_key)


async def check_board(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    if github is None:
        state["phase"] = "idle"
        return state

    # In multi-session mode the daemon invokes the graph once per active
    # session within a single cycle.  Cache the board result to avoid
    # redundant GitHub polls.  Only used when max_concurrent_cards > 1;
    # single-card mode always polls fresh to avoid stale cache issues.
    config = state.get("config")
    _raw_max = getattr(config, "max_concurrent_cards", 1) if config else 1
    max_cards = _raw_max if isinstance(_raw_max, int) else 1
    cached_board = state.get("_board_cache") if max_cards > 1 else None
    if cached_board:
        board = cached_board
    else:
        try:
            board = await github.poll_board()
        except Exception as exc:
            logger.error("check_board.poll_failed", error=str(exc))
            state["phase"] = "idle"
            return state
        if max_cards > 1:
            state["_board_cache"] = board
        state["last_poll_at"] = datetime.now(UTC)

    snapshot = board.get("snapshot")
    state["board_snapshot"] = snapshot if isinstance(snapshot, dict) else {}
    # 046: Stash titles and issue_numbers so assess_card can inject active-card
    # context into the assessor's prompt for implicit dependency detection.
    state["_board_titles"] = board.get("titles", {})  # type: ignore[typeddict-unknown-key]
    state["_board_issue_numbers"] = board.get("issue_numbers", {})  # type: ignore[typeddict-unknown-key]
    state["_board_issue_urls"] = board.get("issue_urls", {})  # type: ignore[typeddict-unknown-key]

    # 045: Refresh current_card metadata from the fresh board snapshot whenever
    # we have an active card.  Without this, fields that aren't persisted in
    # WorkflowSnapshot (or that change between restarts — title edits, label
    # tweaks) stay stale.  The concrete failure this repairs: after a restart
    # the restore path used to rebuild current_card with issue_number=0, so the
    # next dispatch opened a PR whose body lacked ``Closes #N``.  Refreshing
    # here also keeps title/description in sync when the human edits the issue
    # mid-flight.  Skip when the card is mid-cancellation (``active_card
    # disappeared`` handler below has its own logic).
    active_card = state.get("current_card")
    if isinstance(active_card, dict):
        active_id = str(active_card.get("id", ""))
        if active_id:
            titles = board.get("titles", {})
            descriptions = board.get("descriptions", {})
            issue_numbers = board.get("issue_numbers", {})
            issue_urls = board.get("issue_urls", {})
            content_node_ids = board.get("content_node_ids", {})
            if active_id in titles or active_id in issue_numbers:
                refreshed_description = str(descriptions.get(active_id, active_card.get("description", "")))
                active_card["title"] = str(titles.get(active_id, active_card.get("title", "")))
                active_card["description"] = refreshed_description
                active_card["acceptance_criteria"] = parse_acceptance_criteria(refreshed_description)
                fresh_number = int(issue_numbers.get(active_id, 0))
                if fresh_number:
                    active_card["issue_number"] = fresh_number
                fresh_url = str(issue_urls.get(active_id, ""))
                if fresh_url:
                    active_card["issue_url"] = fresh_url
                fresh_content_id = str(content_node_ids.get(active_id, ""))
                if fresh_content_id:
                    active_card["issue_id"] = fresh_content_id
                state["current_card"] = active_card

    in_progress = state["board_snapshot"].get("IN_PROGRESS", [])
    in_review = state["board_snapshot"].get("IN_REVIEW", [])
    blocked = state["board_snapshot"].get("BLOCKED", [])
    todo = state["board_snapshot"].get("TODO", [])

    # 026: Detect active card removed from all known columns (cancellation)
    active_card = state.get("current_card")
    active_phase = state.get("phase", "idle")
    if active_card and isinstance(active_card, dict) and active_phase not in ("idle",):
        active_id = str(active_card.get("id", ""))
        all_known_ids = {
            str(item_id) for col in (
                in_progress, in_review, blocked, todo,
                state["board_snapshot"].get("DONE", []),
                state["board_snapshot"].get("BACKLOG", []),
            ) for item_id in col
        }
        if active_id and active_id not in all_known_ids:
            logger.warning(
                "check_board.active_card_disappeared",
                card_id=active_id,
                previous_phase=active_phase,
            )
            from coordinare.cancel import cancel_active_card
            await cancel_active_card(state, move_to_todo=False)
            return state

    if in_review:
        # NOTE (035): In multi-card mode, check_board runs once per daemon
        # cycle (not per-session).  Per-session routing is handled by
        # _invoke_multi_session in the daemon; this early-return for
        # IN_REVIEW / IN_PROGRESS columns is correct for both modes.
        #
        # Preserve dispatching phase from classify_human_feedback even if
        # the GitHub move to IN_PROGRESS failed and the card is still in
        # IN_REVIEW.  The dispatch will move it on the next attempt.
        # Also preserve blocked phase from veto override (031).
        if state.get("phase") in ("dispatching", "blocked"):
            return state
        state["phase"] = "monitoring_pr"
        return state
    if in_progress:
        if state.get("system_error_count", 0) > 0:
            state["phase"] = "system_error"
            return state
        # Preserve dispatching, monitoring_performer, and blocked phases so
        # the lifecycle re-entry, performer monitoring, and veto overrides
        # aren't overwritten by check_board.
        current_phase = state.get("phase")
        if current_phase in ("dispatching", "monitoring_performer", "blocked"):
            return state

        # Re-adopt orphaned IN_PROGRESS card after restart with no state.
        # Without this, a card left in IN_PROGRESS after a state-less restart
        # would never be picked up because current_card is None.
        if state.get("current_card") is None:
            item = in_progress[0]
            titles = board.get("titles", {})
            descriptions = board.get("descriptions", {})
            issue_numbers = board.get("issue_numbers", {})
            issue_urls = board.get("issue_urls", {})
            content_node_ids = board.get("content_node_ids", {})
            description = str(descriptions.get(item, ""))
            state["current_card"] = {
                "id": item,
                "issue_id": str(content_node_ids.get(item, "")),
                "issue_number": int(issue_numbers.get(item, 0)),
                "issue_url": str(issue_urls.get(item, "")),
                "title": str(titles.get(item, "")),
                "description": description,
                "acceptance_criteria": parse_acceptance_criteria(description),
                "status": "IN_PROGRESS",
                "previous_status": "IN_PROGRESS",
            }
            logger.info(
                "check_board.readopted_in_progress_card",
                card_id=item,
                title=str(titles.get(item, "")),
            )
            state["phase"] = "dispatching"
            return state

        state["phase"] = "monitoring_agent"
        return state
    if blocked:
        if state.get("system_error_notified"):
            state["phase"] = "idle"
            return state
        item = blocked[0]
        titles = board.get("titles", {})
        descriptions = board.get("descriptions", {})
        issue_numbers = board.get("issue_numbers", {})
        issue_urls = board.get("issue_urls", {})
        content_node_ids = board.get("content_node_ids", {})
        description = str(descriptions.get(item, ""))
        state["current_card"] = {
            "id": item,
            "issue_id": str(content_node_ids.get(item, "")),
            "issue_number": int(issue_numbers.get(item, 0)),
            "issue_url": str(issue_urls.get(item, "")),
            "title": str(titles.get(item, "")),
            "description": description,
            "acceptance_criteria": parse_acceptance_criteria(description),
            "status": "BLOCKED",
            "previous_status": "BLOCKED",
        }

        last_notified = state.get("last_blocked_notified_at")
        issue_node_id = str(content_node_ids.get(item, ""))
        if last_notified is not None and isinstance(last_notified, datetime):
            try:
                details = await github.get_issue_details(issue_node_id or item)
            except Exception as exc:
                logger.warning("check_board.get_issue_details_failed", card_id=item, error=str(exc))
                state["phase"] = "blocked"
                return state
            comments_node = details.get("comments")
            comments = (
                comments_node.get("nodes", [])
                if isinstance(comments_node, dict)
                else []
            )
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                # 042: Skip comments authored by the coordinare bot itself.
                # GitHub's ``createdAt`` is second-precision while our local
                # ``last_blocked_notified_at`` is sub-second — so the bot's
                # own freshly-posted reminder comment can appear "newer than
                # the cutoff" due to rounding, get misread as a user answer,
                # and trigger an infinite blocked → dispatch loop.  Filtering
                # by author is the correct primary check (only humans can
                # supply answers); the timestamp remains a secondary guard
                # so very old human comments from prior rounds don't count.
                author = comment.get("author") or {}
                author_login = str(author.get("login", "")) if isinstance(author, dict) else ""
                if author_login.endswith("[bot]") or author_login == "vivi-coordinare":
                    continue
                created_raw = comment.get("createdAt", "")
                if not isinstance(created_raw, str) or not created_raw:
                    continue
                try:
                    created_at = datetime.fromisoformat(
                        created_raw.replace("Z", "+00:00")
                    )
                    if created_at > last_notified:
                        # Record the user's answer alongside the questions that
                        # were asked, so assess_card can pass the full Q&A history
                        # to Claude and avoid asking the same questions again.
                        answer_body = str(comment.get("body", "")).strip()
                        prior_questions = [
                            str(q) for q in (state.get("open_questions") or [])
                        ]
                        clarification: dict = {
                            "questions": prior_questions,
                            "answer": answer_body,
                        }
                        existing = state.get("card_clarifications") or []
                        state["card_clarifications"] = [*existing, clarification]
                        state["open_questions"] = []
                        state["agent_dispatch"] = {}

                        await github.move_card(item, "IN_PROGRESS")
                        state["current_card"]["previous_status"] = "BLOCKED"
                        state["current_card"]["status"] = "IN_PROGRESS"
                        # Re-run assess_card with full Q&A history rather than
                        # trying to check status on an already-terminated performer.
                        state["phase"] = "dispatching"
                        state["last_blocked_notified_at"] = None
                        return state
                except (ValueError, TypeError):
                    continue

        raw_hours = state.get("blocked_reminder_hours", 24)
        hours = raw_hours if isinstance(raw_hours, int) else 24
        now = datetime.now(UTC)
        if last_notified is None or (
            isinstance(last_notified, datetime)
            and now - last_notified >= timedelta(hours=hours)
        ):
            state["phase"] = "blocked"
            return state

        state["phase"] = "idle"
        return state
    if todo:
        # Filter out items carrying advocate labels (FR-001a)
        advocate_labels = set()
        handled = state.get("advocate_handled_label", "")
        escalation = state.get("advocate_escalation_label", "")
        if handled:
            advocate_labels.add(str(handled))
        if escalation:
            advocate_labels.add(str(escalation))

        item_labels = board.get("item_labels", {})
        eligible_todo = [
            item_id for item_id in todo
            if not (set(item_labels.get(item_id, [])) & advocate_labels)
        ]

        # 025: Sort by priority field if configured
        config = state.get("config")
        if config is not None and hasattr(config, "priority"):
            prio_cfg = config.priority
            if prio_cfg.field_name:
                item_field_values = board.get("item_field_values", {})
                has_field = any(
                    prio_cfg.field_name in item_field_values.get(iid, {})
                    for iid in eligible_todo
                )
                if has_field:
                    eligible_todo = _sort_by_priority(
                        eligible_todo, item_field_values,
                        prio_cfg.field_name, prio_cfg.priority_order,
                    )
                else:
                    logger.debug(
                        "check_board.priority_field_not_found_on_cards",
                        field_name=prio_cfg.field_name,
                    )

        # 046: Filter out cards whose explicit dependencies haven't reached DONE.
        # Build the dependency graph from the full board (not just TODO) so we
        # can resolve blocker statuses across all columns.  Circular deps are
        # detected here too; cycle members are handled after filtering.
        # Clear blocked_by_dependencies here (not at cycle top) so early-return
        # paths for in_progress / in_review / blocked cards don't lose the
        # dependency context that was set on a previous cycle.
        state["blocked_by_dependencies"] = []  # type: ignore[typeddict-unknown-key]
        if eligible_todo:
            dep_graph = build_graph(board)
            # 046: Resolve off-board dependencies by checking GitHub issue state.
            # This upgrades UNRESOLVABLE → SATISFIED for closed issues that
            # were removed from the project board after completion.
            github = state.get("github_service")
            config = state.get("config")
            repo_slug = ""
            if config is not None:
                org = getattr(config, "github_org", "")
                project = getattr(config, "project_name", "")
                if org and project:
                    repo_slug = f"{org}/{project}"
            await resolve_off_board_dependencies(dep_graph, github, repo_slug)
            pre_filter_list = list(eligible_todo)  # preserve priority-sorted order
            eligible_todo = _dep_filter(eligible_todo, dep_graph)
            post_filter_set = set(eligible_todo)
            # Keep insertion order from pre_filter_list so the first filtered
            # card is the highest-priority one (not an arbitrary set member).
            filtered_ids = [iid for iid in pre_filter_list if iid not in post_filter_set]
            if filtered_ids:
                logger.info(
                    "check_board.dependency_filtered",
                    filtered_count=len(filtered_ids),
                    remaining_count=len(eligible_todo),
                )
                # Populate blocked_by_dependencies for the first filtered card
                # (in the priority-sorted eligible_todo order, not raw board
                # order) so the operator sees the highest-priority blocker.
                titles_map = board.get("titles", {})
                issue_urls_map = board.get("issue_urls", {})
                content_node_ids = board.get("content_node_ids", {})
                blocked_deps_for_state: list[dict] = []
                for item_id in filtered_ids:
                    deps = dep_graph.by_dependent.get(item_id, [])
                    for d in deps:
                        if d.status != DependencyStatus.SATISFIED:
                            blocker_item = dep_graph.issue_to_item.get(d.blocker_issue_number)
                            raw_url = issue_urls_map.get(blocker_item, "") if blocker_item else ""
                            blocked_deps_for_state.append({
                                "issue_number": d.blocker_issue_number,
                                "title": titles_map.get(blocker_item, "") if blocker_item else None,
                                "column": dep_graph.issue_to_column.get(d.blocker_issue_number),
                                "issue_url": raw_url if raw_url else None,
                                "source": d.source.value,
                            })
                    if blocked_deps_for_state:
                        break  # show deps for first blocked card only
                state["blocked_by_dependencies"] = blocked_deps_for_state  # type: ignore[typeddict-unknown-key]

                # FR-010: Cards with UNRESOLVABLE deps (off-board issue not
                # closed) must be blocked with a comment, not silently left
                # in TODO.  Move them to BLOCKED and post a diagnostic.
                github_svc = state.get("github_service")
                if github_svc is not None:
                    for item_id in filtered_ids:
                        unresolvable = [
                            d for d in dep_graph.by_dependent.get(item_id, [])
                            if d.status == DependencyStatus.UNRESOLVABLE
                        ]
                        if unresolvable:
                            import contextlib
                            dep_labels = ", ".join(f"#{d.blocker_issue_number}" for d in unresolvable)
                            with contextlib.suppress(Exception):
                                await github_svc.move_card(item_id, "BLOCKED")
                            issue_node = content_node_ids.get(item_id)
                            if issue_node:
                                with contextlib.suppress(Exception):
                                    await github_svc.add_comment(
                                        issue_node,
                                        f"🔗 **Unresolvable dependency**: {dep_labels}\n\n"
                                        "The referenced issue(s) are not on the project board "
                                        "and could not be verified as closed (the issue may be "
                                        "open, missing, or the API check failed).  Add them to "
                                        "the board or close them to unblock this card.",
                                    )
            # (No else needed — blocked_by_dependencies is reset at the top
            # of every poll cycle; it's only populated when cards are filtered.)

            # Block cards involved in circular dependencies — move them to
            # BLOCKED on the board and post a diagnostic comment so operators
            # know which cards are deadlocked.
            if dep_graph.cycles:
                cycle_item_ids = {iid for cycle in dep_graph.cycles for iid in cycle}
                issue_numbers_map = board.get("issue_numbers", {})
                cycle_issues = [
                    f"#{issue_numbers_map.get(iid, '?')}" for iid in sorted(cycle_item_ids)
                ]
                cycle_desc = ", ".join(cycle_issues)
                logger.warning(
                    "check_board.circular_dependency_detected",
                    cycle_item_ids=sorted(cycle_item_ids),
                    cycle_description=cycle_desc,
                )
                # Remove cycle members from eligible (they can't be dispatched)
                eligible_todo = [
                    iid for iid in eligible_todo if iid not in cycle_item_ids
                ]
                # Best-effort: move cycle members to BLOCKED on the board and
                # post a comment.  This is a fire-and-forget — if it fails,
                # the cards stay in TODO but still won't be dispatched (the
                # filter already removed them).
                github = state.get("github_service")
                content_node_ids = board.get("content_node_ids", {})
                import contextlib

                # Only move TODO cards to BLOCKED — DONE/IN_PROGRESS/etc. cards
                # may appear in cycle_item_ids because build_graph parses all
                # descriptions, but moving a DONE card back to BLOCKED would be
                # destructive.
                todo_set = set(todo)
                if github is not None:
                    for iid in cycle_item_ids:
                        if iid not in todo_set:
                            continue
                        with contextlib.suppress(Exception):
                            await github.move_card(iid, "BLOCKED")
                        issue_node_id = content_node_ids.get(iid)
                        if issue_node_id:
                            with contextlib.suppress(Exception):
                                await github.add_comment(
                                    issue_node_id,
                                    f"🔄 **Circular dependency detected** involving: {cycle_desc}\n\n"
                                    "These cards form a dependency cycle — none can "
                                    "proceed.  Resolve by removing or reordering the "
                                    "dependency declarations in one of the issue bodies.",
                                )

        if eligible_todo:
            titles = board.get("titles", {})
            descriptions = board.get("descriptions", {})
            issue_numbers = board.get("issue_numbers", {})
            issue_urls = board.get("issue_urls", {})
            content_node_ids = board.get("content_node_ids", {})

            # 035: Multi-card pickup — fill active_sessions up to concurrency limit
            max_cards = 1
            config = state.get("config")
            if config is not None and hasattr(config, "max_concurrent_cards"):
                max_cards = max(1, int(config.max_concurrent_cards))

            active_sessions: dict = state.get("active_sessions") or {}
            already_active_ids = set(active_sessions.keys())

            if max_cards > 1:
                # Multi-card mode: pick up cards into active_sessions
                slots_available = max(0, max_cards - len(active_sessions))
                picked = 0
                for item in eligible_todo:
                    if picked >= slots_available:
                        break
                    if item in already_active_ids:
                        continue  # deduplicate — covers both pre-existing sessions
                        # and duplicate IDs within eligible_todo
                    description = str(descriptions.get(item, ""))
                    card_dict = {
                        "id": item,
                        "issue_id": str(content_node_ids.get(item, "")),
                        "issue_number": int(issue_numbers.get(item, 0)),
                        "issue_url": str(issue_urls.get(item, "")),
                        "title": str(titles.get(item, "")),
                        "description": description,
                        "acceptance_criteria": parse_acceptance_criteria(description),
                        "status": "TODO",
                        "previous_status": "TODO",
                    }
                    active_sessions[item] = create_session_from_card(card_dict)
                    already_active_ids.add(item)
                    picked += 1

                state["active_sessions"] = active_sessions

                # Only populate flat current_card when no session has already
                # been loaded into state (i.e., the initial bootstrap call).
                # In per-session invocations, _invoke_multi_session already
                # set current_card via session_to_state before this runs, so
                # overwriting it would clobber the active session's card.
                if not state.get("current_card") and active_sessions:
                    first_session = next(iter(active_sessions.values()))
                    state["current_card"] = first_session.get("current_card")
                    state["phase"] = "dispatching"
                elif not active_sessions:
                    state["phase"] = "idle"
                return state

            # Single-card mode (default): existing behavior unchanged
            item = eligible_todo[0]
            description = str(descriptions.get(item, ""))
            # Only clear clarifications when picking up a genuinely fresh card.
            # If this is the same card returning from a re-queue (after Q&A),
            # preserve the accumulated Q&A history so assess_card can pass it
            # to the performer and the assessment backend.
            prev_card = state.get("current_card") or {}
            if str(prev_card.get("id", "")) != item:
                state["card_clarifications"] = []
                state["processed_review_ids"] = set()  # new card — reset review tracking
                # 042: Clear commit_summary so the next card's dispatch
                # doesn't fire a stale ``card_merged`` notification.  The
                # notify node detects merge success via commit_summary —
                # leaving it set from the previous card causes every
                # subsequent stage notification to misfire as "merged!".
                state["commit_summary"] = None
                # 045: Reset performer_stage to the first stage in the
                # lifecycle.  Without this the stale stage from a prior
                # card's terminal state (e.g. "closing_review" after the
                # previous card blocked) leaks into the fresh card and
                # jumps the pipeline straight to the closer — which has
                # no pr_url to work with and immediately errors the card
                # back to BLOCKED.
                lifecycle_seq = state.get("lifecycle_sequence") or ["implementing"]
                state["performer_stage"] = lifecycle_seq[0] if lifecycle_seq else "implementing"
                # Reset the other per-card counters so stale feedback /
                # retry budget from the previous card doesn't bleed in.
                state["system_error_count"] = 0
                state["system_error_reason"] = None
                state["system_error_notified"] = False
                state["relay_feedback"] = []
                state["open_questions"] = []
                state["feedback_cycle_count"] = 0  # type: ignore[typeddict-unknown-key]
                state["blocked_by_dependencies"] = []  # type: ignore[typeddict-unknown-key]
            state["current_card"] = {
                "id": item,
                "issue_id": str(content_node_ids.get(item, "")),
                "issue_number": int(issue_numbers.get(item, 0)),
                "issue_url": str(issue_urls.get(item, "")),
                "title": str(titles.get(item, "")),
                "description": description,
                "acceptance_criteria": parse_acceptance_criteria(description),
                "status": "TODO",
                "previous_status": "TODO",
            }
            state["phase"] = "dispatching"
            return state

        # All TODO items filtered (by advocate labels or dependencies).
        # If blocked_by_dependencies is non-empty, some cards are waiting on
        # blockers — log it so operators know the board isn't truly empty.
        if state.get("blocked_by_dependencies"):
            logger.info(
                "check_board.all_todo_dependency_blocked",
                blocked_dep_count=len(state["blocked_by_dependencies"]),
                todo_count=len(todo),
            )
        # Clear stale current_card so persisted snapshots don't carry
        # forward a card that's no longer eligible.
        state["current_card"] = None

    state["phase"] = "idle"
    return state
