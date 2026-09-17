"""Security post step (spec 170 FR-013): exactly one GitHub review.

REQUEST_CHANGES with an inline comment per blocking finding inside a hunk of a
changed file, the rest and every advisory in the body, when any blocking
finding exists; COMMENT listing the advisories and downgrades when none. No
thread is resolved; nothing is committed. A failed post is returned as an
error for the caller to turn into a hold.
"""
from __future__ import annotations

from typing import Any

import structlog

from performer.workflows.reviewer.gate import anchor_in_hunks
from performer.workflows.reviewer.models import ChangedFile
from performer.workflows.reviewer.post import PostOutcome, Poster, pr_number_from_url
from performer.workflows.security.models import SecurityFinding

log = structlog.get_logger(__name__)

__all__ = ["attribution_header", "build_security_review", "post_security_review"]


def attribution_header(score: Any) -> str:
    """Byte for byte the header main.py's ``_persona_tag`` posts for the security stage (spec 077)."""
    harness = getattr(score, "backend", None) or "?"
    model = getattr(score, "model", None) or "?"
    marker = f"<!-- coordinare-attribution origin=performer role=security harness={harness} model={model} -->"
    return f"{marker}\n> 🤖 **Security** · harness `{harness}` · model `{model}`"


def _line(f: SecurityFinding) -> str:
    where = f"`{f.path}:{f.line}`" if f.path else "(pull request)"
    tool = "" if f.tool == "model" else f" (reported by {f.tool})"
    down = f"\n  Downgraded to advisory: {f.downgrade_reason}" if f.downgraded else ""
    ev = f"\n  Evidence: `{f.evidence[:200]}`" if f.evidence else ""
    return f"- **{f.severity} {f.category}**{tool} at {where}, introduced by `{f.introduced_by}`: {f.problem}\n  Why blocking: {f.why_blocking}{ev}{down}"


def _baseline_line(f: SecurityFinding) -> str:
    """412 round 25: baseline findings are pre-existing -- they have no
    blocking reason to give, and this PR did not introduce them."""
    where = f"`{f.path}:{f.line}`" if f.path else "(pull request)"
    tool = "" if f.tool == "model" else f" (reported by {f.tool})"
    ev = f"\n  Evidence: `{f.evidence[:200]}`" if f.evidence else ""
    return f"- **{f.severity} {f.category}**{tool} at {where}: {f.problem}{ev}"


#: 412 round 29: GitHub review bodies are size-capped, and a scan-heavy
#: baseline can otherwise blow past the cap and turn a non-blocking report
#: into a failed post (surfacing as env_blocked). The rendered section is
#: bounded by total characters; the persisted record keeps every finding.
MAX_BASELINE_RENDER_CHARS = 6000


def _baseline_section(baseline: list[SecurityFinding]) -> str:
    """412 round 25: baseline findings are pre-existing -- they have no
    blocking reason to give, and this PR did not introduce them. 412 round
    29: the section renders at most MAX_BASELINE_RENDER_CHARS of finding
    text and summarizes the remainder, so the body stays postable."""
    if not baseline:
        return ""
    lines: list[str] = []
    used = 0
    for f in baseline:
        line = _baseline_line(f)
        if used + len(line) > MAX_BASELINE_RENDER_CHARS and lines:
            lines.append(
                f"- (+{len(baseline) - len(lines)} more pre-existing findings omitted from this report;"
                " the full list is in the persisted record)",
            )
            break
        lines.append(line)
        used += len(line)
    return ("\n\nBaseline scanner findings (pre-existing, not introduced by this PR):\n" + "\n".join(lines))


def build_security_review(blocking: list[SecurityFinding], advisory: list[SecurityFinding], changed_files: list[ChangedFile], header: str, baseline: list[SecurityFinding] | None = None) -> tuple[str, str, list[dict]]:
    baseline = baseline or []
    inline: list[dict] = []
    in_body: list[SecurityFinding] = []
    for f in blocking:
        if f.path and f.line > 0 and anchor_in_hunks(f, changed_files):
            body = f"**{f.severity} {f.category}**: {f.problem}\n\nWhy blocking: {f.why_blocking}\n\nRoute: {f.routing}"
            if f.evidence:
                body += f"\n\nEvidence: `{f.evidence[:200]}`"
            if f.tool != "model":
                body += f"\n\nReported by {f.tool}."
            inline.append({"path": f.path, "line": f.line, "body": body})
        else:
            in_body.append(f)
    adv = ("\n\nAdvisories:\n" + "\n".join(_line(f) for f in advisory)) if advisory else ""
    base = _baseline_section(baseline)
    if blocking:
        body = f"{header}\n\n**Bot Security Review: FAILED**\n\n{len(blocking)} blocking finding(s); {len(inline)} inline."
        if in_body:
            body += "\n\n" + "\n".join(_line(f) for f in in_body)
        return "REQUEST_CHANGES", body + adv + base, inline
    body = f"{header}\n\n**Bot Security Review: PASSED**\n\nNo blocking findings; {len(advisory)} advisory finding(s)."
    return "COMMENT", body + adv + base, []


async def post_security_review(score: Any, blocking: list[SecurityFinding], advisory: list[SecurityFinding], changed_files: list[ChangedFile], poster: Poster | None = None, baseline: list[SecurityFinding] | None = None) -> PostOutcome:
    if poster is None:
        from performer.github import post_pull_request_review as poster  # noqa: PLC0415 - keeps the workflow importable without network deps
    number = pr_number_from_url(getattr(score, "pr_url", "") or "")
    if number <= 0:
        return PostOutcome(error=f"pr_url is missing or invalid ({getattr(score, 'pr_url', None)!r})")
    event, body, inline = build_security_review(blocking, advisory, changed_files, attribution_header(score), baseline)
    try:
        owner, repo = score.owner_repo
        token = score.effective_github_token
        result = await poster(owner, repo, number, event=event, body=body, comments=inline, token=token)
    except Exception as exc:  # noqa: BLE001 - the hold names the failure
        log.warning("security.post_failed", error=str(exc)[:300], review_event=event)
        return PostOutcome(error=f"posting the review failed: {str(exc)[:300]}", event=event, inline=len(inline))
    url = result.get("html_url") if isinstance(result, dict) else None
    log.info("security.posted", review_event=event, inline=len(inline), url=url)
    return PostOutcome(url=str(url) if url else None, event=event, inline=len(inline))
