"""Security workflow scanner: runs model-determined security tooling, normalizes output.

Spec 366: The model determines what security tooling applies to this repository.
The scan runs inside the performer, fails closed on any tool problem (missing,
crash, unparseable output, tool not applicable), and produces normalized findings
matching the coordinare's security_scanner.py output for parity (FR-020).

This reverses spec 170's design (scan before model call) — the justification is
that a mechanical floor that scans nothing on three of four languages is not a
floor. Abstention now properly reads as unavailable (env_blocked) not as a pass.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any, Callable

import structlog

from performer.workflows.security.budgets import SecurityBudgets
from performer.workflows.security.models import ScanResult

logger = structlog.get_logger(__name__)

# CWE numbers whose semgrep WARNING findings escalate to high rather than medium.


class ScannerUnavailable(Exception):
    """Raised when a scanner tool is missing, crashes, exits abnormally, or emits unparseable output.

    ``results`` holds the ScanResults of the tools that completed before the failure.
    """

    results: list = []

    def __init__(self, tool: str, reason: str):
        self.tool = tool
        self.reason = reason
        super().__init__(f"{tool}: {reason}")






#: Exit codes that mean the tool ran: 0 (clean) and 1 (findings). Anything else is a tool problem.
OK_EXIT_CODES = frozenset({0, 1})


async def _run_tool(
    argv: list[str], cwd: Path, tool: str, runner: Callable, budgets: SecurityBudgets
) -> tuple[int | None, str, str, int]:
    """Run a scanner subprocess and return (exit_code, stdout, stderr, duration_ms)."""
    import time

    start = time.monotonic()
    try:
        exit_code, stdout, stderr = await runner(argv, cwd, budgets.scan_timeout_s)
    except asyncio.TimeoutError:
        raise ScannerUnavailable(tool, f"timed out after {budgets.scan_timeout_s}s")
    except FileNotFoundError:
        raise ScannerUnavailable(tool, "binary not found")
    duration = int((time.monotonic() - start) * 1000)
    if exit_code not in OK_EXIT_CODES:
        raise ScannerUnavailable(tool, f"exited {exit_code} (only 0 and 1 mean the tool ran)")
    if not (stdout or "").strip():
        raise ScannerUnavailable(tool, f"produced no output (exit {exit_code})")
    return (exit_code, stdout, stderr, duration)


async def run_scan(
    files: list[str],
    repo_root: Path,
    *,
    tools: list[Any] | None = None,
    read: Callable | None = None,
    runner: Callable | None = None,
    budgets: SecurityBudgets,
) -> tuple[list[dict], list[ScanResult]]:
    """Run model-determined security tools over files, return (findings, results).

    Spec 366: The model determines what scanning applies to this repository by
    specifying the tools list. Each tool is a (name, build_fn, normalize_fn) tuple.
    build_fn(files, budgets) -> argv; normalize_fn(result) -> normalized_finding.

    If tools is None or empty, raises ScannerUnavailable to fail closed when no
    applicable scanning determined (abstention must not read as a pass).

    findings: list of dicts in the spec-022 schema. results: one ScanResult per tool
    that ran. Raises ScannerUnavailable on a missing tool, a crash, a timeout, an
    exit code other than 0 or 1, malformed output, or no applicable tools;
    the exception carries the ScanResults of the tools that completed before it.
    """
    if not files:
        return ([], [])
    if runner is None:
        runner = default_runner
    if not tools:
        raise ScannerUnavailable("(no applicable tools)", "model determined no scanning applies to this repository")
    findings: list[dict] = []
    results: list[ScanResult] = []
    examined_any: set[str] = set()
    unexamined: list[str] = []
    for tool in tools:
        argv = list(tool.argv)
        try:
            exit_code, stdout, _stderr, duration = await _run_tool(argv, repo_root, tool.name, runner, budgets)
        except ScannerUnavailable as exc:
            exc.results = list(results)
            raise
        # 366: the model reads the tool's own output. No per-tool normalizer,
        # and no JSON-shape assumption -- a scanner that prints a table is as
        # readable as one that prints JSON, and neither needs coordinare to
        # learn its format.
        reading = await read(tool.name, argv, stdout, files)
        examined_any.update(reading.examined_paths())
        unexamined.extend(f"{c.path} ({tool.name}: {c.reason})" for c in reading.unexamined())
        # The tool's identity is known exactly here, so stamp it. The gate used
        # to recover it by testing whether the description started with
        # "bandit:", which only works in a world with two scanners whose names
        # coordinare already knows -- the world #366 exists to leave.
        findings.extend({**f, "tool": tool.name} for f in reading.findings)
        results.append(ScanResult(
            tool=tool.name, command=shlex.join(argv), exit_code=exit_code,
            finding_count=len(reading.findings), duration_ms=duration,
        ))

    # 366: the defect this exists to stop. bandit on a Ruby repository exits 0
    # with zero findings and an errors block nobody read, so "examined nothing"
    # was indistinguishable from "found nothing". An abstention is a hold, not
    # a pass.
    if not examined_any:
        exc = ScannerUnavailable(
            ", ".join(t.name for t in tools),
            "examined none of the %d changed file(s): %s" % (len(files), "; ".join(unexamined[:5]) or "no tool reported reading any of them"),
        )
        exc.results = list(results)
        raise exc
    return (findings, results)


async def default_runner(argv: list[str], cwd: Path, timeout_s: int) -> tuple[int | None, str, str]:
    """Default async runner using asyncio.create_subprocess_exec."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout_s,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return (proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"))
    except asyncio.TimeoutError:
        raise


__all__ = [
    "ScannerUnavailable",
    "run_scan",
    "default_runner",
]
