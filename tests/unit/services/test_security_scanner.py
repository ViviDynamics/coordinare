"""Unit tests for the coordinare-side security scanner (spec 083, Contract 1).

Strategy: mock ``subprocess.run`` with canned semgrep/bandit JSON so tests are
deterministic and offline (semgrep ``--config auto`` would otherwise fetch rules
over the network). ``scan_diff`` is a pure transform over subprocess results.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import structlog

from coordinare.services import security_scanner
from coordinare.services.security_scanner import ScannerError, scan_diff

FIXTURES = Path(__file__).parent / "fixtures" / "security_scanner"


# --- canned tool outputs ---------------------------------------------------


def _semgrep_payload(results: list[dict]) -> str:
    return json.dumps({"results": results, "errors": []})


def _bandit_payload(results: list[dict]) -> str:
    return json.dumps({"results": results, "errors": [], "metrics": {}})


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["tool"], returncode=returncode, stdout=stdout, stderr="",
    )


SEMGREP_INJECTION = {
    "check_id": "python.lang.security.audit.dangerous-system-call",
    "path": "vuln.py",
    "start": {"line": 16},
    "end": {"line": 16},
    "extra": {
        "severity": "ERROR",
        "message": "Found user input flowing into os.system",
        "metadata": {"cwe": ["CWE-78: OS Command Injection"]},
    },
}

BANDIT_SHELL = {
    "filename": "vuln.py",
    "line_number": 17,
    "issue_severity": "HIGH",
    "issue_confidence": "HIGH",
    "issue_text": "subprocess call with shell=True identified",
    "test_id": "B602",
    "test_name": "subprocess_popen_with_shell_equals_true",
}

BANDIT_WEAK_HASH = {
    "filename": "secret.py",
    "line_number": 12,
    "issue_severity": "MEDIUM",
    "issue_confidence": "MEDIUM",
    "issue_text": "Use of weak MD5 hash for security",
    "test_id": "B324",
    "test_name": "hashlib",
}


def _patch_tools(monkeypatch, *, semgrep, bandit):
    """Patch subprocess.run to dispatch by tool name to canned outputs.

    ``semgrep``/``bandit`` may be a CompletedProcess, or an Exception/callable to
    raise (e.g. FileNotFoundError for tool-missing).
    """

    def fake_run(cmd, *args, **kwargs):
        # cmd[0] may be an absolute venv path (see _resolve_binary); match by basename.
        tool = Path(cmd[0]).name
        spec = semgrep if tool == "semgrep" else bandit
        if isinstance(spec, BaseException):
            raise spec
        return spec

    monkeypatch.setattr(security_scanner.subprocess, "run", fake_run)


# --- binary resolution -----------------------------------------------------


def test_resolve_binary_prefers_venv(monkeypatch, tmp_path):
    """semgrep/bandit resolve from the venv bin dir (next to sys.executable)
    even when the daemon was launched without the venv on PATH.

    Regression: the daemon ran as ``.venv/bin/coordinare`` without activating
    the venv, so a bare ``["semgrep", ...]`` call raised FileNotFoundError,
    tripped the fail-closed floor, and wedged the card in ``blocked``.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_semgrep = bindir / "semgrep"
    fake_semgrep.write_text("#!/bin/sh\n")
    fake_semgrep.chmod(0o755)
    fake_python = bindir / "python"

    monkeypatch.setattr(security_scanner.sys, "executable", str(fake_python))
    # PATH lookup would NOT find it — proving venv resolution is what wins.
    monkeypatch.setattr(security_scanner.shutil, "which", lambda _tool: None)

    assert security_scanner._resolve_binary("semgrep") == str(fake_semgrep)


def test_resolve_binary_falls_back_to_path(monkeypatch, tmp_path):
    """When the binary is not beside sys.executable, fall back to a PATH lookup."""
    fake_python = tmp_path / "python"  # no sibling "bandit" exists
    monkeypatch.setattr(security_scanner.sys, "executable", str(fake_python))
    monkeypatch.setattr(security_scanner.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    assert security_scanner._resolve_binary("bandit") == "/usr/bin/bandit"


def test_resolve_binary_bare_name_last_resort(monkeypatch, tmp_path):
    """If neither the venv nor PATH has it, return the bare name (subprocess
    then raises FileNotFoundError -> ScannerError, preserving fail-closed)."""
    fake_python = tmp_path / "python"
    monkeypatch.setattr(security_scanner.sys, "executable", str(fake_python))
    monkeypatch.setattr(security_scanner.shutil, "which", lambda _tool: None)

    assert security_scanner._resolve_binary("semgrep") == "semgrep"


# --- tests -----------------------------------------------------------------


def test_injection_yields_critical_or_high(monkeypatch):
    _patch_tools(
        monkeypatch,
        semgrep=_completed(_semgrep_payload([SEMGREP_INJECTION]), returncode=1),
        bandit=_completed(_bandit_payload([BANDIT_SHELL]), returncode=1),
    )

    findings = scan_diff(["vuln.py"], FIXTURES / "injection_diff")

    assert len(findings) >= 1
    sevs = {f["severity"] for f in findings}
    assert sevs & {"critical", "high"}
    # semgrep ERROR -> critical, located correctly
    sem = next(f for f in findings if f["line"] == 16)
    assert sem["severity"] == "critical"
    assert sem["file"] == "vuln.py"
    assert "injection" in sem["category"].lower() or "78" in sem["category"]
    assert sem["routing"] == "implementer"
    # bandit HIGH/HIGH -> critical
    ban = next(f for f in findings if f["line"] == 17)
    assert ban["severity"] == "critical"


def test_clean_diff_returns_empty(monkeypatch):
    _patch_tools(
        monkeypatch,
        semgrep=_completed(_semgrep_payload([]), returncode=0),
        bandit=_completed(_bandit_payload([]), returncode=0),
    )

    assert scan_diff(["benign.py"], FIXTURES / "clean_diff") == []


def test_semgrep_only(monkeypatch):
    semgrep_warn = {
        "check_id": "javascript.browser.security.insecure-innerhtml",
        "path": "jsleak.js",
        "start": {"line": 4},
        "extra": {
            "severity": "WARNING",
            "message": "innerHTML assignment from user input",
            "metadata": {"cwe": ["CWE-79: Cross-site Scripting"]},
        },
    }
    _patch_tools(
        monkeypatch,
        semgrep=_completed(_semgrep_payload([semgrep_warn]), returncode=1),
        bandit=_completed(_bandit_payload([]), returncode=0),
    )

    findings = scan_diff(["jsleak.js"], FIXTURES / "semgrep_only")

    assert len(findings) == 1
    assert findings[0]["file"] == "jsleak.js"
    assert findings[0]["line"] == 4
    # WARNING + dangerous CWE (XSS) -> high
    assert findings[0]["severity"] == "high"


def test_bandit_only(monkeypatch):
    _patch_tools(
        monkeypatch,
        semgrep=_completed(_semgrep_payload([]), returncode=0),
        bandit=_completed(_bandit_payload([BANDIT_WEAK_HASH]), returncode=1),
    )

    findings = scan_diff(["secret.py"], FIXTURES / "bandit_only")

    assert len(findings) == 1
    # MEDIUM/MEDIUM severity/confidence -> medium per data-model mapping (MEDIUM/* -> medium)
    assert findings[0]["severity"] == "medium"
    assert findings[0]["file"] == "secret.py"
    assert findings[0]["line"] == 12


def test_malformed_output_raises(monkeypatch):
    _patch_tools(
        monkeypatch,
        semgrep=_completed("not json {{{", returncode=0),
        bandit=_completed(_bandit_payload([]), returncode=0),
    )

    with pytest.raises(ScannerError):
        scan_diff(["vuln.py"], FIXTURES / "injection_diff")


def test_tool_missing_raises(monkeypatch):
    _patch_tools(
        monkeypatch,
        semgrep=FileNotFoundError("semgrep not found"),
        bandit=_completed(_bandit_payload([]), returncode=0),
    )

    with pytest.raises(ScannerError):
        scan_diff(["vuln.py"], FIXTURES / "injection_diff")


def test_info_logs_never_contain_raw_findings(monkeypatch):
    """FR-011: INFO logs limited to a summary (counts/severities); never raw text."""
    _patch_tools(
        monkeypatch,
        semgrep=_completed(_semgrep_payload([SEMGREP_INJECTION]), returncode=1),
        bandit=_completed(_bandit_payload([BANDIT_SHELL]), returncode=1),
    )

    with structlog.testing.capture_logs() as logs:
        scan_diff(["vuln.py"], FIXTURES / "injection_diff")

    # Flatten every value of every captured log event into one string.
    blob = " ".join(str(v) for entry in logs for v in entry.values())
    # The raw finding message text must not leak into logs.
    assert "os.system" not in blob
    assert "shell=True" not in blob
    assert "Found user input" not in blob


def test_info_logs_are_summary_only(monkeypatch):
    """SC-005: the only scan log emitted is the ``security_scan.complete`` summary
    (counts/severities) — no raw diff content and no auth_env-resolved secrets.

    Asserts BOTH the positive (the summary event is present, carrying only
    count/severity keys) and the negative (no secret value, no raw diff line,
    no raw finding text appears in any captured event).
    """
    # A secret value a misbehaving scanner might echo from a resolved auth_env.
    secret_value = "sk-super-secret-token-value-DO-NOT-LOG"
    raw_diff_marker = "RAW_DIFF_LINE_password_eq_hunter2"
    monkeypatch.setenv("LITELLM_PROXY_AUTH_TOKEN", secret_value)

    _patch_tools(
        monkeypatch,
        semgrep=_completed(_semgrep_payload([SEMGREP_INJECTION]), returncode=1),
        bandit=_completed(_bandit_payload([BANDIT_SHELL]), returncode=1),
    )

    with structlog.testing.capture_logs() as logs:
        findings = scan_diff(["vuln.py"], FIXTURES / "injection_diff")

    assert findings  # sanity: the scan produced findings

    # Positive: exactly one scan event, and it is the summary with only safe keys.
    summaries = [e for e in logs if e.get("event") == "security_scan.complete"]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["file_count"] == 1
    assert summary["finding_count"] == 2
    assert set(summary["severities"]) <= {"critical", "high", "medium", "low"}
    # No payload-bearing keys (diff/message/auth) on the summary event.
    assert not ({"diff", "message", "messages", "findings", "auth_env", "auth"} & set(summary))

    # Negative: nothing sensitive leaks into ANY captured event.
    blob = " ".join(str(v) for entry in logs for v in entry.values())
    assert secret_value not in blob
    assert raw_diff_marker not in blob
    assert "os.system" not in blob
    assert "shell=True" not in blob
    assert "Found user input" not in blob
