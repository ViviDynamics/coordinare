"""1-alert-1-runbook invariant test (spec 009 US5, T027)."""
from __future__ import annotations

import json
import re
from pathlib import Path

# Project root is 3 levels up from tests/unit/
_REPO_ROOT = Path(__file__).parent.parent.parent
_DASHBOARD_PATH = _REPO_ROOT / "docs" / "dashboards" / "coordinare.json"

_REQUIRED_SECTIONS = [
    "## Trigger Condition",
    "## Operational Impact",
    "## Diagnostic Steps",
    "## Resolution Actions",
    "## Escalation Path",
]


def _extract_alert_runbooks(dashboard: dict) -> list[tuple[str, str]]:
    """Return [(alert_name, runbook_url), ...] for all alerts in all panels."""
    results = []
    for panel in dashboard.get("panels", []):
        alert = panel.get("alert")
        if not alert:
            continue
        name = alert.get("name", "")
        runbook_url = alert.get("annotations", {}).get("runbookURL", "")
        results.append((name, runbook_url))
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dashboard_is_valid_json() -> None:
    """docs/dashboards/coordinare.json is a valid JSON file."""
    assert _DASHBOARD_PATH.exists(), f"Dashboard not found at {_DASHBOARD_PATH}"
    with _DASHBOARD_PATH.open() as f:
        data = json.load(f)
    assert isinstance(data, dict)
    assert data.get("title") == "Coordinare Daemon"


def test_every_alert_has_runbook() -> None:
    """Every alert rule in the dashboard has a corresponding runbook file."""
    with _DASHBOARD_PATH.open() as f:
        dashboard = json.load(f)

    alerts = _extract_alert_runbooks(dashboard)
    assert alerts, "No alert rules found in dashboard"

    for alert_name, runbook_url in alerts:
        assert runbook_url, f"Alert '{alert_name}' has no runbookURL annotation"
        assert runbook_url.startswith("docs/runbooks/") and runbook_url.endswith(".md"), (
            f"Alert '{alert_name}' runbookURL must be under docs/runbooks/ and end with .md: {runbook_url!r}"
        )
        runbook_path = _REPO_ROOT / runbook_url
        assert runbook_path.exists(), (
            f"Alert '{alert_name}' references missing runbook: {runbook_url}"
        )


def test_runbooks_have_required_sections() -> None:
    """Each runbook file contains all 5 required section headings."""
    with _DASHBOARD_PATH.open() as f:
        dashboard = json.load(f)

    alerts = _extract_alert_runbooks(dashboard)
    for _alert_name, runbook_url in alerts:
        runbook_path = _REPO_ROOT / runbook_url
        content = runbook_path.read_text()
        for section in _REQUIRED_SECTIONS:
            assert section in content, (
                f"Runbook '{runbook_url}' is missing section: {section!r}"
            )


def test_runbooks_have_minimum_content_depth() -> None:
    """Each runbook has ≥3 numbered items in Diagnostic Steps and ≥1 in Resolution Actions."""
    with _DASHBOARD_PATH.open() as f:
        dashboard = json.load(f)

    alerts = _extract_alert_runbooks(dashboard)
    for _alert_name, runbook_url in alerts:
        runbook_path = _REPO_ROOT / runbook_url
        content = runbook_path.read_text()

        # Extract section content between headings
        sections = re.split(r"\n## ", content)
        diag_section = next(
            (s for s in sections if s.startswith("Diagnostic Steps")), None
        )
        resolution_section = next(
            (s for s in sections if s.startswith("Resolution Actions")), None
        )

        assert diag_section is not None, f"{runbook_url}: Missing Diagnostic Steps section"
        assert resolution_section is not None, f"{runbook_url}: Missing Resolution Actions section"

        diag_items = re.findall(r"^\d+\.", diag_section, re.MULTILINE)
        resolution_items = re.findall(r"^\d+\.", resolution_section, re.MULTILINE)

        assert len(diag_items) >= 3, (
            f"{runbook_url}: Diagnostic Steps has {len(diag_items)} items, need ≥3"
        )
        assert len(resolution_items) >= 1, (
            f"{runbook_url}: Resolution Actions has {len(resolution_items)} items, need ≥1"
        )
