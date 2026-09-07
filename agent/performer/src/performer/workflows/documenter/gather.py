"""Documenter gather step (spec 171 FR-005): per-page evidence through the allow-list.

Commands are built by code, not proposed by the model: the current page, the
named modules' first lines, and the diff hunks the page cites or documents.
Every command passes the spec-165 allow-list before it runs and is recorded.
"""
from __future__ import annotations

import shlex
from pathlib import Path

import structlog

from performer.workflows.architect.allowlist import is_allowed
from performer.workflows.documenter.models import PageEvidence, PagePlan
from performer.workflows.reviewer.models import ChangedFile

log = structlog.get_logger(__name__)

__all__ = ["gather_page", "hunks_for_page"]

_COMMAND_TIMEOUT_S = 30


def hunks_for_page(plan: PagePlan, changed_files: list[ChangedFile], citations: list[str], diff_text: str) -> str:
    """The diff sections of the changed files this page cites or documents."""
    wanted = set()
    for f in changed_files:
        for c in list(citations) + list(plan.modules):
            c = c.rstrip("/")
            if f.path == c or f.path.startswith(c + "/"):
                wanted.add(f.path)
    if not wanted:
        return ""
    out = []
    for section in diff_text.split("diff --git ")[1:]:
        header = section.split("\n", 1)[0]
        if any(header.endswith(" b/" + p) for p in wanted):
            out.append("diff --git " + section[:6000])
    return "\n".join(out)[:20000]


async def gather_page(toolkit, workspace: Path, plan: PagePlan, *, max_commands: int, max_output_chars: int) -> tuple[PageEvidence, str, str]:
    """Run the page's evidence commands; return (evidence record, current page content, evidence text)."""
    commands: list[str] = []
    if plan.exists:
        commands.append(f"git show HEAD:{shlex.quote(plan.path)}")
    for module in plan.modules[: max(0, max_commands - len(commands))]:
        commands.append(f"sed -n '1,200p' {shlex.quote(module)}")
    records: list[dict] = []
    current = ""
    evidence_parts: list[str] = []
    for cmd in commands[:max_commands]:
        ok, reason = is_allowed(cmd)
        if not ok:
            records.append({"command": cmd, "exit_code": None, "chars": 0, "refused": True, "reason": reason})
            log.info("documenter.gather", command=cmd[:120], allowed=False, reason=reason)
            continue
        result = await toolkit.run_command(cmd, cwd=workspace, timeout_s=_COMMAND_TIMEOUT_S)
        output = (result.output_excerpt or "")[:max_output_chars]
        records.append({"command": cmd, "exit_code": result.exit_code, "chars": len(output), "refused": False, "reason": ""})
        log.info("documenter.gather", command=cmd[:120], allowed=True, exit_code=result.exit_code, chars=len(output))
        if cmd.startswith("git show HEAD:") and result.exit_code == 0:
            current = output
        elif result.exit_code == 0 and output.strip():
            target = shlex.split(cmd)[-1]
            evidence_parts.append(f"### {target}\n```\n{output}\n```")
    evidence = PageEvidence(commands=records, chars=sum(r["chars"] for r in records))
    return evidence, current, "\n\n".join(evidence_parts)
