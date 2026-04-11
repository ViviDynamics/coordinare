from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from coordinare.lib.acceptance_criteria import parse_acceptance_criteria
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

        # All TODO items are filtered by advocate labels — clear any stale current_card
        # so persisted snapshots don't carry forward a card that's no longer eligible.
        state["current_card"] = None

    state["phase"] = "idle"
    return state
