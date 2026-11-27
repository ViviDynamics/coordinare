from __future__ import annotations

# Mandatory prose security scanner (083, 107). Tool execution and normalization
# live in the performer; the daemon gates the runtime response findings.
# A missing tool, abnormal exit, malformed JSON or scan error fails closed.
# Logs contain only counts and severities, never tool output or source text.

import ast
import json
import os
import re
import stat
import subprocess
import warnings
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
_OK_EXIT_CODES = {0, 1}
_TRUSTED_BINARY_DIRS = (Path("/usr/local/bin"), Path("/usr/bin"))
_UNTRUSTED_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH
_ROOT_UID = 0
# bandit parses every target as Python and reports a file that does not parse
# as a scan error, so a doc beside the code held the floor (#415). bandit gets
# only Python sources; semgrep keeps every target.
_PYTHON_SUFFIXES = frozenset({".py", ".pyi", ".pyw"})
# The suffix security_floor gives an exported file whose name has none. Only
# such files are classified by shebang or content; a file with any other
# suffix is not Python, even when it parses as Python (.ini, .json).
EXTENSIONLESS_SUFFIX = ".scanned"
_EXTENSIONLESS_SUFFIXES = frozenset({"", EXTENSIONLESS_SUFFIX})
# Any interpreter named python..., not ipython or other names that end in it.
_PYTHON_SHEBANG = re.compile(rb"#![^\n]*\bpython")
_UTF8_BOM = b"\xef\xbb\xbf"
# A larger extensionless file is not parsed here; it goes to bandit (fail closed).
_CONTENT_CHECK_MAX_BYTES = 1024 * 1024
SCANNER_INSTALLATION_HELP = (
    "Install semgrep and bandit system-wide as root-owned executables in "
    "/usr/local/bin or /usr/bin, or use the container transport."
)


class ScannerError(Exception):
    """Raised when a scanner tool is missing, crashes, or emits unparseable output.

    Distinct from a *clean* or *findings-present* run : both of those return a
    (possibly empty) list. Callers treat ``ScannerError`` as fail-closed.
    """

_SAFE_DIAGNOSTICS = frozenset({
    'security scan head changed or unavailable',
    'scan target is a directory',
    'scan target is not a regular file',
    'security export must be outside workspace',
    'unsupported or missing security scan request',
    'PR diff unavailable or changed paths overflow',
    'changed paths unavailable',
    'security scan head unavailable',
    'unsafe scan path',
    "semgrep emitted malformed results", "bandit emitted malformed results",
} | {
    f"{tool} {reason}"
    for tool in ("semgrep", "bandit")
    for reason in (
        "binary not found", "timed out", "failed to launch", "produced no output",
        "emitted unparseable JSON", "failed", "emitted malformed results", "reported scan errors",
    )
} | {
    f"git {operation} {reason}"
    for operation in ("rev-parse", "ls-tree", "show")
    for reason in ("failed", "timed out")
})


class ScanDiagnostic(ScannerError):
    """Code-owned diagnostic; never transport arbitrary exception messages."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason if reason in _SAFE_DIAGNOSTICS else "unexpected security scan failure"


class ScannerSetupError(ScannerError):
    """A trusted scanner is unavailable and needs operator installation."""

    def __init__(self, reason: str, *, tool: str = "") -> None:
        super().__init__(reason)
        self.reason = (
            f"{tool} trusted system binary not found. {SCANNER_INSTALLATION_HELP}"
            if tool in ("semgrep", "bandit") else SCANNER_INSTALLATION_HELP
        )


def scan_diff(changed_files: list[str], repo_root: str | Path) -> list[Finding]:
    """Run semgrep over ``changed_files`` and bandit over their Python files.

    Returns a list of normalized findings (the spec-022 schema). Returns ``[]``
    for a clean scan. Raises :class:`ScannerError` on tool-missing / crash /
    unparseable output.
    """
    repo_root = Path(repo_root)
    files = [f"./{name}" if name.startswith("-") else name for name in changed_files]

    findings: list[Finding] = []
    findings.extend(_run_semgrep(files, repo_root))
    python_files = _python_targets(files, repo_root)
    if files and not python_files:
        # Nothing for bandit to read, but a missing bandit still holds the floor.
        _resolve_binary("bandit")
    findings.extend(_run_bandit(python_files, repo_root))

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


def _python_targets(files: list[str], repo_root: Path) -> list[str]:
    """The targets bandit scans.

    A Python suffix always goes to bandit, so a Python file it cannot parse
    still fails the scan. A file with no suffix goes to bandit when it has a
    python shebang, when it parses as Python (``uv run --script`` files,
    ``SConstruct``), or when it is too large to check here. Any other suffix,
    and any file with a NUL byte, is left to semgrep.
    """
    return [name for name in files if _is_python(repo_root / name)]


def _is_python(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in _PYTHON_SUFFIXES:
        return True
    if suffix not in _EXTENSIONLESS_SUFFIXES:
        return False
    try:
        with path.open("rb") as handle:
            source = handle.read(_CONTENT_CHECK_MAX_BYTES + 1)
    except OSError:
        return False
    if _PYTHON_SHEBANG.match(source.removeprefix(_UTF8_BOM)):
        return True
    return b"\0" not in source and (
        len(source) > _CONTENT_CHECK_MAX_BYTES or _parses_as_python(source)
    )


def _parses_as_python(source: bytes) -> bool:
    try:
        # Parsing never runs the code. Its warnings would print source fragments.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    except (RecursionError, MemoryError):
        return True  # Too deep or too large to check here: bandit decides.
    return True


def _trusted_executable(candidate: Path) -> bool:
    resolved = candidate.resolve()
    if not any(resolved.is_relative_to(directory) for directory in _TRUSTED_BINARY_DIRS):
        return False
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return False
    return all(
        path.stat().st_uid == _ROOT_UID and not path.stat().st_mode & _UNTRUSTED_WRITE_BITS
        for path in (resolved, *resolved.parents, candidate, *candidate.parents)
    )


def _resolve_binary(tool: str) -> str:
    """Use only root-owned system executables, never a workspace venv or PATH."""
    for directory in _TRUSTED_BINARY_DIRS:
        candidate = directory / tool
        if _trusted_executable(candidate):
            return str(candidate)
    reason = f"{tool} trusted system binary not found. {SCANNER_INSTALLATION_HELP}"
    raise ScannerSetupError(reason, tool=tool)


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
        raise ScanDiagnostic(f"{tool} binary not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScanDiagnostic(f"{tool} timed out") from exc
    except OSError as exc:
        raise ScanDiagnostic(f"{tool} failed to launch") from exc

    if not (proc.stdout or "").strip():
        # No JSON at all : a clean run still emits ``{"results": []}``; empty
        # stdout means the tool crashed before producing output.
        raise ScanDiagnostic(f"{tool} produced no output")

    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ScanDiagnostic(f"{tool} emitted unparseable JSON") from exc

    if proc.returncode not in _OK_EXIT_CODES:
        raise ScanDiagnostic(f"{tool} failed")
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ScanDiagnostic(f"{tool} emitted malformed results")
    if payload.get("errors") or any(not isinstance(row, dict) for row in payload["results"]):
        raise ScanDiagnostic(f"{tool} reported scan errors")
    return payload


def _run_semgrep(files: list[str], repo_root: Path) -> list[Finding]:
    if not files:
        return []
    # The copy-only export contains only changed files at HEAD under neutral
    # names, with no repository ignore or config files under their control names.
    # Semgrep receives explicit file targets, never directories.
    payload = _invoke(
        [_resolve_binary("semgrep"), "--config", "auto", "--json",
         "--disable-nosem", "--no-git-ignore", *files],
        repo_root,
        "semgrep",
    )
    findings: list[Finding] = []
    for result in payload.get("results", []) or []:
        try:
            findings.append(_normalize_semgrep(result))
        except (TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise ScanDiagnostic("semgrep emitted malformed results") from exc
    return findings


def _run_bandit(files: list[str], repo_root: Path) -> list[Finding]:
    if not files:
        return []
    payload = _invoke(
        [_resolve_binary("bandit"), "--ignore-nosec", "-f", "json", "-r", *files],
        repo_root,
        "bandit",
    )
    findings: list[Finding] = []
    for result in payload.get("results", []) or []:
        try:
            findings.append(_normalize_bandit(result))
        except (TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise ScanDiagnostic("bandit emitted malformed results") from exc
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
