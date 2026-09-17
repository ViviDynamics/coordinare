"""Coordinare-side static-analysis scanner (spec 083, Contract 1).

Runs semgrep + bandit over a PR's changed files and normalizes both tools'
output into the spec-022 finding schema. This is the *authoritative floor*: its
findings gate the security verdict in the monitor, independent of the model.

Design notes:
- Pure transform over subprocess results — deterministic given fixed tool output.
- "Tool found issues" (non-zero exit with parseable JSON) is NOT an error; only
  tool-missing / crash / unparseable output raises :class:`ScannerError`, so the
  dispatch layer can apply fail-closed handling (FR-008).
- INFO logs are limited to a scan summary (counts/severities). Raw diff text,
  finding messages, and ``auth_env`` values MUST NOT be logged (FR-011).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Finding = dict with keys: severity, category, description, file, line, routing.
Finding = dict

# CWE numbers whose semgrep WARNING findings escalate to ``high`` rather than
# ``medium`` (the "dangerous CWE" set referenced in data-model.md).
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
    },
)

_DEFAULT_ROUTING = "implementer"
_SUBPROCESS_TIMEOUT_S = 120


class ScannerError(Exception):
    """Raised when a scanner tool is missing, crashes, or emits unparseable output.

    Distinct from a *clean* or *findings-present* run — both of those return a
    (possibly empty) list. Callers treat ``ScannerError`` as fail-closed.
    """


def scan_diff(changed_files: list[str], repo_root: str | Path) -> list[Finding]:
    """Run semgrep + bandit over ``changed_files`` under ``repo_root``.

    Returns a list of normalized findings (the spec-022 schema). Returns ``[]``
    for a clean scan. Raises :class:`ScannerError` on tool-missing / crash /
    unparseable output.
    """
    repo_root = Path(repo_root)
    files = list(changed_files)

    findings: list[Finding] = []
    findings.extend(_run_semgrep(files, repo_root))
    findings.extend(_run_bandit(files, repo_root))

    # Summary-only logging (FR-011): counts by severity, never raw text.
    severity_counts: dict[str, int] = {}
    for f in findings:
        severity_counts[f["severity"]] = severity_counts.get(f["severity"], 0) + 1
    logger.info(
        "security_scan.complete",
        file_count=len(files),
        finding_count=len(findings),
        severities=severity_counts,
    )
    return findings


def _resolve_binary(tool: str) -> str:
    """Resolve a scanner executable, preferring the active venv's bin dir.

    The coordinare daemon is commonly launched as ``.venv/bin/coordinare`` *without*
    activating the venv, so ``.venv/bin`` is not on ``PATH``. A bare-name
    subprocess call then raises ``FileNotFoundError`` -> :class:`ScannerError`,
    which fails the security floor closed and wedges the card in ``blocked``.

    Resolution order:
      1. ``<dir of sys.executable>/<tool>`` — the venv that's running us.
      2. ``shutil.which(tool)`` — a PATH lookup.
      3. the bare ``tool`` name — last resort (subprocess will raise if absent,
         preserving the documented fail-closed behavior).
    """
    candidate = Path(sys.executable).parent / tool
    if candidate.exists():
        return str(candidate)
    found = shutil.which(tool)
    if found:
        return found
    return tool


def _invoke(cmd: list[str], cwd: Path, tool: str) -> dict:
    """Run a scanner subprocess and return its parsed JSON stdout.

    Tool "found issues" non-zero exit is normal; only a missing binary or
    unparseable stdout raises :class:`ScannerError`.
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise ScannerError(f"{tool} binary not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScannerError(f"{tool} timed out") from exc
    except OSError as exc:  # pragma: no cover - defensive
        raise ScannerError(f"{tool} failed to launch") from exc

    if not (proc.stdout or "").strip():
        # No JSON at all — a clean run still emits ``{"results": []}``; empty
        # stdout means the tool crashed before producing output.
        raise ScannerError(f"{tool} produced no output (exit {proc.returncode})")

    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ScannerError(f"{tool} emitted unparseable JSON") from exc


def _run_semgrep(files: list[str], repo_root: Path) -> list[Finding]:
    if not files:
        return []
    payload = _invoke(
        [_resolve_binary("semgrep"), "--config", "auto", "--json", *files],
        repo_root,
        "semgrep",
    )
    findings: list[Finding] = []
    for result in payload.get("results", []) or []:
        findings.append(_normalize_semgrep(result))
    return findings


def _run_bandit(files: list[str], repo_root: Path) -> list[Finding]:
    if not files:
        return []
    payload = _invoke(
        [_resolve_binary("bandit"), "-f", "json", "-r", *files],
        repo_root,
        "bandit",
    )
    findings: list[Finding] = []
    for result in payload.get("results", []) or []:
        findings.append(_normalize_bandit(result))
    return findings


def _cwe_numbers(metadata: dict) -> list[str]:
    """Extract bare CWE numbers from semgrep metadata (``["CWE-78: ..."]``)."""
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


def _normalize_semgrep(result: dict) -> Finding:
    extra = result.get("extra", {}) or {}
    metadata = extra.get("metadata", {}) or {}
    severity = str(extra.get("severity", "INFO")).upper()
    cwes = _cwe_numbers(metadata)
    dangerous = any(c in _DANGEROUS_CWES for c in cwes)

    if severity in {"ERROR", "CRITICAL"}:
        normalized = "critical"
    elif severity == "WARNING":
        normalized = "high" if dangerous else "medium"
    else:  # INFO and anything unknown -> low (conservative floor: low won't gate)
        normalized = "low"

    category = cwes[0] if cwes else str(result.get("check_id", "semgrep"))
    return {
        "severity": normalized,
        "category": _safe_category(category),
        "description": f"semgrep:{result.get('check_id', 'rule')}",
        "file": str(result.get("path", "")),
        "line": int((result.get("start", {}) or {}).get("line", 0) or 0),
        "routing": _DEFAULT_ROUTING,
    }


def _normalize_bandit(result: dict) -> Finding:
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
        "category": _safe_category(category),
        "description": f"bandit:{result.get('test_id', 'check')}",
        "file": str(result.get("filename", "")),
        "line": int(result.get("line_number", 0) or 0),
        "routing": _DEFAULT_ROUTING,
    }


def _safe_category(value: str) -> str:
    """Keep category labels short and free of any embedded payload text."""
    return value.strip()[:80]
