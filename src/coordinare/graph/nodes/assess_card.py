from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from coordinare.services.persona_service import get_effective_instructions, load_personas_hot

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def assess_card(state: CoordinareState) -> CoordinareState:
    card = state.get("current_card")
    github = state.get("github_service")
    backend = state.get("conducting_backend")
    if not isinstance(card, dict) or github is None or backend is None:
        state["phase"] = "idle"
        return state

    try:
        details = await github.get_issue_details(str(card.get("issue_id", "")))
        # Work with a mutable copy before attaching additional metadata.
        details = dict(details)

        # Attach any accumulated Q&A history so the assessment backend can
        # incorporate it when generating follow-up questions or deciding
        # whether there is now enough information to proceed.
        clarifications = state.get("card_clarifications") or []
        if clarifications:
            details["clarifications"] = clarifications

        # 046: Inject titles of other active cards (TODO / IN_PROGRESS / IN_REVIEW)
        # so the assessor can detect implicit dependencies — e.g., "Add dark mode"
        # logically depends on "Implement theming" even without explicit syntax.
        board_snapshot = state.get("board_snapshot") or {}
        # board_snapshot only has column → [item_ids]; titles are on the
        # board dict cached in _board_cache for multi-card mode or re-fetched
        # each cycle in single-card mode.  We stash the titles map as a
        # lightweight state extension so assess_card can access it.
        raw_titles = state.get("_board_titles") or {}
        raw_issue_numbers = state.get("_board_issue_numbers") or {}
        active_columns = ("TODO", "IN_PROGRESS", "IN_REVIEW")
        active_cards_for_assessor: list[dict] = []
        own_id = str(card.get("id", ""))
        for col in active_columns:
            for item_id in board_snapshot.get(col, []):
                if item_id == own_id:
                    continue  # don't include the card being assessed
                title = raw_titles.get(item_id, "")
                issue_num = raw_issue_numbers.get(item_id, 0)
                if title or issue_num:
                    active_cards_for_assessor.append({
                        "issue_number": issue_num,
                        "title": title,
                        "column": col,
                    })
        if active_cards_for_assessor:
            details["active_cards"] = active_cards_for_assessor

        # Inject assessor persona instructions with hot-reload (018-performer-personas).
        personas = load_personas_hot(state.get("config_path"), state.get("config"))
        details["persona_instructions"] = get_effective_instructions("assessor", personas)

        assessment = await backend.assess(details)
    except Exception as exc:
        logger.error(
            "assess_card.service_failure",
            card_id=str(card.get("id", "")),
            error=str(exc),
        )
        state["phase"] = "blocked"
        state["open_questions"] = [f"Assessment failed: {exc}"]
        return state

    clarifications = state.get("card_clarifications") or []
    answered_rounds = [c for c in clarifications if isinstance(c, dict) and c.get("answer", "").strip()]

    questions = assessment.get("questions") or []
    sufficient = assessment.get("sufficient", True)

    # 046: Check for assessor-detected dependencies before the sufficiency check.
    # If the assessor flagged blocker issue numbers, block the card with a
    # dependency explanation regardless of the "sufficient" verdict — the card
    # can't proceed until its blockers are resolved.
    raw_deps = assessment.get("dependencies") or []
    if not isinstance(raw_deps, list):
        raw_deps = [raw_deps] if raw_deps else []
    seen_deps: set[int] = set()
    deps: list[int] = []
    for n in raw_deps:
        # Only accept ints (excluding bools, which are int subclasses) or
        # numeric strings.  Anything else from the LLM is silently dropped
        # rather than coerced — avoids surprising int(True) → 1 behaviour.
        if isinstance(n, bool) or not isinstance(n, (int, str)):
            continue
        try:
            val = int(n)
        except (ValueError, TypeError):
            continue
        if val > 0 and val not in seen_deps:
            seen_deps.add(val)
            deps.append(val)
    if deps:
        dep_labels = ", ".join(f"#{n}" for n in deps)
        logger.info(
            "assess_card.dependency_detected",
            card_id=str(card.get("id", "")),
            dependencies=deps,
        )
        dep_questions = [
            f"This card appears to depend on {dep_labels}. "
            "Those cards should complete first to avoid merge conflicts "
            "and wasted implementation effort."
        ]
        # Merge with any sufficiency questions the assessor also raised.
        all_questions = dep_questions + [str(q) for q in questions if str(q).strip()]
        # Populate blocked_by_dependencies so dashboard/Slack show the
        # dependency UI instead of the generic questions preview.
        board_snapshot = state.get("board_snapshot") or {}
        raw_titles = state.get("_board_titles") or {}
        raw_issue_nums = state.get("_board_issue_numbers") or {}
        # issue_urls stashed by check_board alongside titles
        raw_issue_urls: dict = state.get("_board_issue_urls") or {}
        # Reverse lookup: issue_number → item_id
        num_to_item = {v: k for k, v in raw_issue_nums.items() if v > 0}
        dep_info: list[dict] = []
        for n in deps:
            blocker_item = num_to_item.get(n)
            col = None
            if blocker_item:
                for c, items in board_snapshot.items():
                    if blocker_item in items:
                        col = c
                        break
            raw_url = raw_issue_urls.get(blocker_item, "") if blocker_item else ""
            dep_info.append({
                "issue_number": n,
                "title": raw_titles.get(blocker_item, "") if blocker_item else None,
                "column": col,
                "issue_url": raw_url if raw_url else None,
                "source": "assessor",
            })
        state["blocked_by_dependencies"] = dep_info  # type: ignore[typeddict-unknown-key]
        state["phase"] = "blocked"
        state["open_questions"] = all_questions
        return state

    # If the model returned insufficient with no new questions, the assessor
    # had nothing concrete to ask — treat as sufficient rather than blocking
    # indefinitely. This covers both: (a) first assessment where the assessor
    # found no issues, and (b) follow-up rounds where all questions are answered.
    if not sufficient and not questions:
        logger.info(
            "assess_card.no_questions_treating_as_sufficient",
            card_id=str(card.get("id", "")),
            answered_rounds=len(answered_rounds),
            msg="Assessor returned insufficient but asked no questions — proceeding to dispatch",
        )
        sufficient = True

    if sufficient:
        # Embed accumulated clarifications into the card so dispatch_card
        # can forward the full Q&A context to the performer.
        if clarifications and isinstance(card, dict):
            card = dict(card)
            card["clarifications"] = clarifications
            state["current_card"] = card
        state["phase"] = "dispatching"
        state["open_questions"] = []
    else:
        state["phase"] = "blocked"
        state["open_questions"] = [str(item) for item in questions] if isinstance(questions, list) else []
    return state
