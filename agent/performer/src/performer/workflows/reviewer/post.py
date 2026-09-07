"""Reviewer post step (spec 169 FR-010, FR-011): exactly one GitHub review.

REQUEST_CHANGES with one inline comment per finding that sits in a diff hunk
(GitHub rejects inline comments outside the diff), the rest in the body, when
findings survive; COMMENT with a short body when none do. Fixed dispositions
are named in the body. No thread is resolved. A failed post is reported back
as an error for the caller to turn into an environment hold.
"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

import structlog

from performer.workflows.reviewer.gate import anchor_in_hunks
from performer.workflows.reviewer.models import ChangedFile, Finding

log = structlog.get_logger(__name__)

__all__ = ["build_review", "pr_number_from_url", "post_review", "PostOutcome", "attribution_header"]

Poster = Callable[..., Awaitable[dict]]


def pr_number_from_url(pr_url: str) -> int:
    match = re.search(r"/pull/(\d+)(?:[/?#]|$)", pr_url or "")
    return int(match.group(1)) if match else 0


def attribution_header(score: Any) -> str:
    """Byte for byte the header main.py's ``_persona_tag`` posts for the reviewing
    stage (spec 077): the hidden ``coordinare-attribution`` marker plus the human
    line. Both units post as the same GitHub app, so this is what tells a reader
    and coordinare's classification which stage, harness and model wrote it."""
    harness = getattr(score, "backend", None) or "?"
    model = getattr(score, "model", None) or "?"
    marker = f"<!-- coordinare-attribution origin=performer role=reviewing harness={harness} model={model} -->"
    return f"{marker}\n> 🤖 **Reviewer** · harness `{harness}` · model `{model}`"


def _finding_line(f: Finding) -> str:
    where = f"`{f.path}:{f.line}`" if f.path else "(pull request)"
    ev = f"\n  Evidence: `{f.evidence[:200]}`" if f.evidence else ""
    return f"- **{f.category}** at {where}: {f.problem}\n  Why blocking: {f.why_blocking}{ev}"


def build_review(findings: list[Finding], changed_files: list[ChangedFile], fixed_ids: list[str], covered: int, header: str) -> tuple[str, str, list[dict]]:
    """Return (event, body, inline_comments)."""
    inline: list[dict] = []
    in_body: list[Finding] = []
    for f in findings:
        if f.path and f.line > 0 and anchor_in_hunks(f, changed_files):
            inline.append({"path": f.path, "line": f.line, "body": f"**{f.category}**: {f.problem}\n\nWhy blocking: {f.why_blocking}" + (f"\n\nEvidence: `{f.evidence[:200]}`" if f.evidence else "")})
        else:
            in_body.append(f)
    fixed = ("\n\nPrior comments addressed: " + ", ".join(fixed_ids)) if fixed_ids else ""
    if findings:
        body = f"{header}\n\n**Bot Review: CHANGES REQUESTED**\n\n{len(findings)} blocking finding(s); {len(inline)} inline."
        if in_body:
            body += "\n\n" + "\n".join(_finding_line(f) for f in in_body)
        return "REQUEST_CHANGES", body + fixed, inline
    body = f"{header}\n\n**Bot Review: APPROVED**\n\nNo blocking findings; {covered} changed file(s) covered. A human reviewer gives the formal approval."
    return "COMMENT", body + fixed, []


class PostOutcome:
    def __init__(self, url: str | None = None, error: str | None = None, event: str = "", inline: int = 0) -> None:
        self.url, self.error, self.event, self.inline = url, error, event, inline

    @property
    def ok(self) -> bool:
        return self.error is None


async def post_review(score: Any, findings: list[Finding], changed_files: list[ChangedFile], fixed_ids: list[str], covered: int, poster: Poster | None = None) -> PostOutcome:
    if poster is None:
        from performer.github import post_pull_request_review as poster  # noqa: PLC0415 - late import keeps the workflow importable without network deps
    number = pr_number_from_url(getattr(score, "pr_url", "") or "")
    if number <= 0:
        return PostOutcome(error=f"pr_url is missing or invalid ({getattr(score, 'pr_url', None)!r})")
    event, body, inline = build_review(findings, changed_files, fixed_ids, covered, attribution_header(score))
    try:
        owner, repo = score.owner_repo
        token = score.effective_github_token
        result = await poster(owner, repo, number, event=event, body=body, comments=inline, token=token)
    except Exception as exc:  # noqa: BLE001 - the hold names the failure
        log.warning("reviewer.post_failed", error=str(exc)[:300], review_event=event)
        return PostOutcome(error=f"posting the review failed: {str(exc)[:300]}", event=event, inline=len(inline))
    url = result.get("html_url") if isinstance(result, dict) else None
    log.info("reviewer.posted", review_event=event, inline=len(inline), url=url)
    return PostOutcome(url=str(url) if url else None, event=event, inline=len(inline))
