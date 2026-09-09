"""Per-card attempt lifecycle shared by dispatch and terminal graph nodes."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from coordinare.attempt_log import TerminalState, Verdict, VerdictSource
    from coordinare.graph.state import CoordinareState


def close_attempt(
    state: CoordinareState,
    verdict: Verdict,
    source: VerdictSource,
    terminal_state: TerminalState | None,
) -> None:
    """Close the current attempt and clear its persisted identity together."""
    log = state.get("attempt_log")
    attempt_id = state.get("last_attempt_id")
    if log is None or not attempt_id:
        return
    log.close_attempt(
        attempt_id=attempt_id,
        verdict=verdict,
        verdict_source=source,
        terminal_state=terminal_state,
    )
    state["last_attempt_id"] = None
    state["last_attempt_log_path"] = None
    state["last_attempt_failure_source"] = None


def start_attempt(state: CoordinareState, card: dict[str, Any], model_name: str | None = None) -> None:
    """Open after successful implementing dispatch, retaining non-content retries."""
    log = state.get("attempt_log")
    if log is None:
        return
    parent = state.get("last_attempt_id")
    failure_source = state.get("last_attempt_failure_source")
    if parent:
        if failure_source not in {"human", "qa_role", "grader"}:
            return
        close_attempt(state, "fail", cast("VerdictSource", failure_source), None)
    description = str(card.get("description") or "")
    lowered = description.lower()
    attempt_id = log.open_attempt(
        task_id=str(card.get("id", "")),
        routing_reason="default_policy",
        parent_attempt_id=parent,
        model_name=model_name,
        spec_word_count=len(description.split()) if description.strip() else None,
        spec_has_acceptance_tests=(
            "acceptance" in lowered or all(word in lowered for word in ("given", "when", "then"))
        ) if description.strip() else None,
    )
    path = log.log_path_for(attempt_id)
    # open_attempt registers its path before returning, including on write failure.
    state["last_attempt_id"] = attempt_id
    state["last_attempt_log_path"] = path
    state["last_attempt_failure_source"] = None
