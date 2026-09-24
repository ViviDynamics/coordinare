"""Closer post step (spec 172 FR-007): exactly one review, before anything is resolved.

Always a COMMENT review: only humans approve formally. The body says what was
resolved and what remains, so a reader sees the closer's whole reasoning.
"""
from __future__ import annotations

from typing import Any

import structlog

from performer.workflows.closer.models import Thread
from performer.workflows.reviewer.post import PostOutcome, Poster, pr_number_from_url

log = structlog.get_logger(__name__)

__all__ = ["attribution_header", "build_closing_review", "post_closing_review"]


def attribution_header(score: Any) -> str:
    """Byte for byte the header main.py's ``_persona_tag`` posts for the closing_review stage."""
    harness = getattr(score, "backend", None) or "?"
    model = getattr(score, "model", None) or "?"
    marker = f"<!-- coordinare-attribution origin=performer role=closing_review harness={harness} model={model} -->"
    return f"{marker}\n> 🤖 **Closer** · harness `{harness}` · model `{model}`"


def build_closing_review(resolved: list[tuple[str, str]], open_threads: list[Thread], header: str, changes_requested_by: list[str] | None = None) -> tuple[str, str]:
    """(event, body). The event is always COMMENT."""
    verdict = "CHANGES REQUESTED" if open_threads or changes_requested_by else "APPROVED"
    body = f"{header}\n\n**Bot Closer Review: {verdict}**\n\n"
    if changes_requested_by:
        body += f"A human reviewer requested changes: {', '.join(changes_requested_by)}.\n"
    if open_threads:
        body += f"{len(open_threads)} review thread(s) still open:\n"
        for t in open_threads:
            where = f"`{t.path}:{t.line}`" if t.path else "(no file anchor)"
            first = t.comments[0].body.strip().replace("\n", " ")[:200] if t.comments else ""
            body += f"- {where}: {first}\n"
    else:
        body += "Every review thread is resolved. Handing off for human review; coordinare's checks gate remains.\n"
    if resolved:
        body += f"\nResolved by this run ({len(resolved)}):\n"
        for _tid, reason in resolved:
            body += f"- {reason}\n"
    return "COMMENT", body


async def post_closing_review(score: Any, resolved: list[tuple[str, str]], open_threads: list[Thread], poster: Poster | None = None, changes_requested_by: list[str] | None = None) -> PostOutcome:
    if poster is None:
        from performer.github import post_pull_request_review as poster  # noqa: PLC0415 - late import keeps the workflow importable without network deps
    number = pr_number_from_url(getattr(score, "pr_url", "") or "")
    if number <= 0:
        return PostOutcome(error=f"pr_url is missing or invalid ({getattr(score, 'pr_url', None)!r})")
    event, body = build_closing_review(resolved, open_threads, attribution_header(score), changes_requested_by)
    try:
        owner, repo = score.owner_repo
        result = await poster(owner, repo, number, event=event, body=body, comments=[], token=score.effective_github_token)
    except Exception as exc:  # noqa: BLE001 - the hold names the failure
        log.warning("closer.post_failed", error=str(exc)[:300])
        return PostOutcome(error=f"posting the review failed: {str(exc)[:300]}", event=event)
    url = result.get("html_url") if isinstance(result, dict) else None
    log.info("closer.posted", review_event=event, open=len(open_threads), resolved=len(resolved), url=url)
    return PostOutcome(url=str(url) if url else None, event=event)
