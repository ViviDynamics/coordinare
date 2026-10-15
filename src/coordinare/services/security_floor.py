from __future__ import annotations

from typing import Any

SCAN_VERSION = 1
SCAN_TOOLS = {"semgrep", "bandit"}
FINDING_SEVERITIES = {"critical", "high", "medium", "low"}
FINDING_ROUTES = {"implementer", "architect", "halt"}


def workflow_security_reported(state: Any, status: dict[str, Any]) -> bool:
    """Only explicit operator configuration can select the workflow gate."""
    report = status.get("report")
    security = report.get("security") if isinstance(report, dict) else None
    if not isinstance(security, dict) or security.get("verdict") not in (
        "security_passed", "security_failed", "env_blocked", "nothing_to_scan", "not_applicable",
    ):
        return False
    if security["verdict"] == "security_passed" and not security.get("scan"):
        return False
    performers = getattr(state.get("config"), "performers", None)
    if performers is None:
        return False
    try:
        return getattr(performers.resolved_role("security"), "workflow", None) == "security"
    except (AttributeError, TypeError):
        return False


def reported_findings(status: dict[str, Any]) -> list[dict[str, Any]]:
    """Missing, partial, malformed and unknown-version scans fail closed."""
    report = status.get("report")
    scan = report.get("security_floor") if isinstance(report, dict) else None
    if isinstance(scan, dict):
        findings = scan.get("findings")
        tools = scan.get("tools")
        if (
            type(scan.get("version")) is int and scan["version"] == SCAN_VERSION
            and scan.get("complete") is True
            and isinstance(tools, list) and all(isinstance(t, str) for t in tools)
            and set(tools) == SCAN_TOOLS
            and isinstance(findings, list) and all(_valid_finding(f) for f in findings)
        ):
            return findings
    return [{
        "severity": "critical", "category": "scanner_unavailable",
        "description": "security scanner unavailable: missing, incomplete or unsupported performer scan report; upgrade the performer and rerun security",
        "file": "", "line": 0, "routing": "halt",
    }]


def _valid_finding(finding: Any) -> bool:
    if not isinstance(finding, dict):
        return False
    return (
        isinstance(finding.get("severity"), str) and finding["severity"] in FINDING_SEVERITIES
        and isinstance(finding.get("routing"), str) and finding["routing"] in FINDING_ROUTES
        and all(isinstance(finding.get(key), str) for key in ("category", "description", "file"))
        and type(finding.get("line")) is int and finding["line"] >= 0
    )
