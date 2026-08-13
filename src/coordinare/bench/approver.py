"""Spec 134 — synthetic approval policy for the board-simulation benchmark.

Coordinare merges only on a human APPROVED review, but a headless benchmark has no
human. An approval policy is a plain callable ``(pr_state) -> bool`` injected into
``FakeGitHubService``; when it returns True the fake inserts an APPROVED review from
a configured ``human_reviewers`` login so ``check_mergeability`` reports
``review_decision == "APPROVED"`` and the merge path proceeds naturally.

A callable is the whole pluggable seam — a later phase can drop in a
ground-truth-aware ("oracle") approver without any class hierarchy here.

The ``pr_state`` dict the fake passes carries the fake-observable signals:
``ci_green`` (bool), ``card_status`` (board column), ``reviews`` (list), and
``head_sha`` (str).
"""

from __future__ import annotations

from typing import Any


def gates_green(pr_state: dict[str, Any]) -> bool:
    """Approve once CI is green AND coordinare has advanced the card to review.

    ``card_status == "IN_REVIEW"`` is the fake-observable proxy that coordinare's
    own upstream gates (assessor/architect/implementer/reviewer/security/qa) have
    passed — coordinare only moves a card to the review column after them. Combined
    with a green CI check, that is the Phase-1 signal to approve (resolves the
    spec-134 analyze finding U1).
    """
    return bool(pr_state.get("ci_green")) and pr_state.get("card_status") == "IN_REVIEW"
