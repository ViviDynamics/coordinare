"""237: the daemon's feed mapping and the dashboard's performer-page mapping must agree.

Both sides read one fixture, tests/fixtures/feed_parity_237.json. The daemon side is
monitor/activity.py _feed_fields. The dashboard side is activity_streams.js afEntryFromBackend,
run under node by tests/js/feed_parity_237_checks.js. Two further checks compare the mapping
tables and the step prefixes directly, so a new event type on one side fails here too.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from coordinare.dashboard import _ACTIVITY_STREAM_JS
from coordinare.graph.nodes.monitor.activity import _ACTIVITY_TYPE_BY_EVENT, _feed_fields
from coordinare.services.workflow_step import KNOWN_STEP_PREFIXES

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "tests" / "fixtures" / "feed_parity_237.json"
_CHECKS_JS = _ROOT / "tests" / "js" / "feed_parity_237_checks.js"

# Every type the daemon maps by name. tool_use is handled before the table, so the table's own
# entry for it is never reached and is left out of the comparison.
_TABLE_ONLY = frozenset({"tool_use"})


def _cases() -> list[dict[str, Any]]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


def _daemon(event: dict[str, Any]) -> dict[str, str]:
    fields = _feed_fields({"is_delta": False, **event})
    return {
        "activity_type": fields["activity_type"],
        "summary": fields.get("summary", ""),
        "status": fields.get("status", ""),
    }


def _dashboard_results(tmp_path: Path) -> list[dict[str, str]]:
    sliced = tmp_path / "activity_streams.js"
    sliced.write_text(_ACTIVITY_STREAM_JS, encoding="utf-8")
    result = subprocess.run(
        ["node", str(_CHECKS_JS), str(sliced), str(_FIXTURE)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"feed parity harness failed:\n{result.stdout}{result.stderr}"
    return json.loads(result.stdout)


def test_the_daemon_produces_the_fixture_entry_for_every_case() -> None:
    for case in _cases():
        assert _daemon(case["event"]) == case["expect"], case["id"]


def test_the_dashboard_produces_the_fixture_entry_for_every_case(tmp_path: Path) -> None:
    results = _dashboard_results(tmp_path)
    cases = _cases()
    assert [r["id"] for r in results] == [c["id"] for c in cases]
    for case, result in zip(cases, results, strict=True):
        expected = {k: result[k] for k in ("activity_type", "summary", "status")}
        assert expected == case["expect"], case["id"]


def test_the_dashboard_mapping_table_is_the_daemon_table() -> None:
    match = re.search(r"var AF_BACKEND_KIND = \{(.*?)\};", _ACTIVITY_STREAM_JS, re.DOTALL)
    assert match is not None, "AF_BACKEND_KIND is no longer declared in activity_streams.js"
    js_table = dict(re.findall(r"(\w+): '(\w+)'", match.group(1)))
    daemon_table = {k: v for k, v in _ACTIVITY_TYPE_BY_EVENT.items() if k not in _TABLE_ONLY}
    assert js_table == daemon_table


def test_the_step_prefixes_are_the_daemon_prefixes() -> None:
    match = re.search(r"var AF_STEP_PREFIXES = \[(.*?)\];", _ACTIVITY_STREAM_JS, re.DOTALL)
    assert match is not None, "AF_STEP_PREFIXES is no longer declared in activity_streams.js"
    js_prefixes = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    assert js_prefixes == set(KNOWN_STEP_PREFIXES)


def test_the_fixture_names_each_case_the_review_asked_for() -> None:
    ids = {c["id"] for c in _cases()}
    for required in ("failed", "failure", "completed", "completion", "blocked", "step_marker"):
        assert required in ids, f"the fixture has no {required} case"
