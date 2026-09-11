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
import json
import shlex
from pathlib import Path
from typing import Callable

import structlog

from performer.workflows.security.budgets import SecurityBudgets
from performer.workflows.security.models import ScanResult

logger = structlog.get_logger(__name__)

# CWE numbers whose semgrep WARNING findings escalate to high rather than medium.
_DANGEROUS_CWES = frozenset(
    {
        "20",  # Improper Input Validation
        "22",  # Path Traversal
        "78",  # OS Command Injection
        "79",  # Cross-site Scripting
        "89",  # SQL Injection
        "94",  # Code Injection
        "200",  # Information Exposure
        "287",  # Improper Authentication
        "306",  # Missing Authentication
        "327",  # Broken/Risky Crypto
        "352",  # CSRF
        "434",  # Unrestricted Upload
        "502",  # Insecure Deserialization
        "611",  # XXE
        "798",  # Hardcoded Credentials
        "918",  # SSRF
    }
)


class ScannerUnavailable(Exception):
    """Raised when a scanner tool is missing, crashes, exits abnormally, or emits unparseable output.

    ``results`` holds the ScanResults of the tools that completed before the failure.
    """

    results: list = []

    def __init__(self, tool: str, reason: str):
        self.tool = tool
        self.reason = reason
        super().__init__(f"{tool}: {reason}")


def cwe_numbers(metadata: dict) -> list[str]:
    """Extract bare CWE numbers from semgrep metadata (e.g., ['CWE-78: ...'])."""
    raw = metadata.get("cwe") if isinstance(metadata, dict) else None
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    numbers: list[str] = []
    for item in items:
        text = str(item)
        # "CWE-78: OS Command Injection" -> "78"
        marker = "CWE-"
        if marker in text:
            tail = text.split(marker, 1)[1]
            num = "".join(ch for ch in tail if ch.isdigit() or ch == ":")
            num = num.split(":", 1)[0]
            if num:
                numbers.append(num)
    return numbers


def safe_category(value: str) -> str:
    """Keep category labels short and free of embedded payload text."""
    return value.strip()[:80]


def normalize_semgrep(result: dict) -> dict:
    """Normalize a semgrep finding to the spec-022 schema."""
    extra = result.get("extra", {}) or {}
    metadata = extra.get("metadata", {}) or {}
    severity = str(extra.get("severity", "INFO")).upper()
    cwes = cwe_numbers(metadata)
    dangerous = any(c in _DANGEROUS_CWES for c in cwes)

    if severity in {"ERROR", "CRITICAL"}:
        normalized = "critical"
    elif severity == "WARNING":
        normalized = "high" if dangerous else "medium"
    else:  # INFO and anything unknown -> low
        normalized = "low"

    category = cwes[0] if cwes else str(result.get("check_id", "semgrep"))
    return {
        "severity": normalized,
        "category": safe_category(category),
        "description": f"semgrep:{result.get('check_id', 'rule')}",
        "file": str(result.get("path", "")),
        "line": int((result.get("start", {}) or {}).get("line", 0) or 0),
        "routing": "implementer",
    }


def normalize_bandit(result: dict) -> dict:
    """Normalize a bandit finding to the spec-022 schema."""
    sev = str(result.get("issue_severity", "LOW")).upper()
    conf = str(result.get("issue_confidence", "LOW")).upper()

    if sev == "HIGH" and conf == "HIGH":
        normalized = "critical"
    elif (sev == "HIGH" and conf == "MEDIUM") or (sev == "MEDIUM" and conf == "HIGH"):
        normalized = "high"
    elif sev == "MEDIUM":
        normalized = "medium"
    elif sev == "HIGH":  # HIGH/LOW
        normalized = "high"
    else:  # LOW/*
        normalized = "low"

    category = str(result.get("test_name") or result.get("test_id") or "bandit")
    return {
        "severity": normalized,
        "category": safe_category(category),
        "description": f"bandit:{result.get('test_id', 'check')}",
        "file": str(result.get("filename", "")),
        "line": int(result.get("line_number", 0) or 0),
        "routing": "implementer",
    }


def build_semgrep_command(files: list[str], config: str) -> list[str]:
    """Build semgrep command. Kept for backward compatibility and testing."""
    return ["semgrep", "--config", config, "--json", *files]


def build_bandit_command(files: list[str]) -> list[str]:
    """Build bandit command. Kept for backward compatibility and testing."""
    return ["bandit", "-f", "json", "-r", *files]


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
    tools: list[tuple[str, Callable[[list[str], SecurityBudgets], list[str]], Callable[[dict], dict]]] | None = None,
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
    for tool, build, normalize in tools:
        argv = build(files, budgets)
        try:
            exit_code, stdout, _stderr, duration = await _run_tool(argv, repo_root, tool, runner, budgets)
            try:
                payload = json.loads(stdout)
            except (json.JSONDecodeError, ValueError):
                raise ScannerUnavailable(tool, "emitted unparseable JSON")
        except ScannerUnavailable as exc:
            exc.results = list(results)
            raise
        tool_findings = [normalize(result) for result in (payload.get("results", []) or [])]
        findings.extend(tool_findings)
        results.append(ScanResult(tool=tool, command=shlex.join(argv), exit_code=exit_code, finding_count=len(tool_findings), duration_ms=duration))
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
    "normalize_semgrep",
    "normalize_bandit",
    "cwe_numbers",
    "safe_category",
    "build_semgrep_command",
    "build_bandit_command",
    "run_scan",
    "default_runner",
]
