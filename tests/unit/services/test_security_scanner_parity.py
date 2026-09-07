"""Spec 170 FR-020: the performer's scanner normaliser equals coordinare's over the
whole severity matrix of both tools. The coordinare daemon image does not ship the
performer package, so the two copies are held equal by this test, not by import."""
from __future__ import annotations

import pytest
from performer.workflows.security import scanner as performer_scanner

from coordinare.services import security_scanner as coordinare_scanner


def _semgrep(severity: str, cwe: list[str] | str | None, check_id: str = "rule.id", path: str = "src/a.py", line: int = 7) -> dict:
    return {"check_id": check_id, "path": path, "start": {"line": line}, "extra": {"severity": severity, "metadata": ({"cwe": cwe} if cwe is not None else {})}}


SEMGREP_MATRIX = [
    _semgrep("ERROR", ["CWE-798: Use of Hard-coded Credentials"]),
    _semgrep("CRITICAL", None),
    _semgrep("WARNING", ["CWE-89: SQL Injection"]),
    _semgrep("WARNING", ["CWE-1004: Cookie"]),
    _semgrep("WARNING", None),
    _semgrep("INFO", ["CWE-200: Information Exposure"]),
    _semgrep("BOGUS", "CWE-22: Path Traversal"),
    {"check_id": "x", "path": "", "start": {}, "extra": {}},
]


def _bandit(sev: str, conf: str, test_id: str = "B324", test_name: str = "hashlib", filename: str = "src/h.py", line: int = 9) -> dict:
    return {"test_id": test_id, "test_name": test_name, "filename": filename, "line_number": line, "issue_severity": sev, "issue_confidence": conf}


BANDIT_MATRIX = [
    _bandit("HIGH", "HIGH"), _bandit("HIGH", "MEDIUM"), _bandit("MEDIUM", "HIGH"), _bandit("MEDIUM", "LOW"),
    _bandit("HIGH", "LOW"), _bandit("LOW", "HIGH"), _bandit("LOW", "LOW"), {"test_id": "B101", "filename": "", "line_number": None},
    _bandit("HIGH", "HIGH", test_name="x" * 120),
]


@pytest.mark.parametrize("result", SEMGREP_MATRIX, ids=[f"semgrep-{i}" for i in range(len(SEMGREP_MATRIX))])
def test_semgrep_normalisers_agree(result):
    assert performer_scanner.normalize_semgrep(result) == coordinare_scanner._normalize_semgrep(result)


@pytest.mark.parametrize("result", BANDIT_MATRIX, ids=[f"bandit-{i}" for i in range(len(BANDIT_MATRIX))])
def test_bandit_normalisers_agree(result):
    assert performer_scanner.normalize_bandit(result) == coordinare_scanner._normalize_bandit(result)


def test_dangerous_cwe_sets_and_helpers_agree():
    assert set(performer_scanner._DANGEROUS_CWES) == set(coordinare_scanner._DANGEROUS_CWES)
    meta = {"cwe": ["CWE-78: OS Command Injection", "CWE-89", "no cwe here"]}
    assert performer_scanner.cwe_numbers(meta) == coordinare_scanner._cwe_numbers(meta)
    assert performer_scanner.safe_category("  a" * 60) == coordinare_scanner._safe_category("  a" * 60)


def test_the_commands_match_coordinares_invocations():
    """Coordinare runs `semgrep --config auto --json <files>` and `bandit -f json -r <files>`."""
    assert performer_scanner.build_semgrep_command(["a.py", "b.py"], "auto") == ["semgrep", "--config", "auto", "--json", "a.py", "b.py"]
    assert performer_scanner.build_bandit_command(["a.py"]) == ["bandit", "-f", "json", "-r", "a.py"]
