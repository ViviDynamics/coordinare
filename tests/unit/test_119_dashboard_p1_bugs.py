"""#119, #120, #121: three P1 dashboard bugs from the 2026-10-13 UX review.

- #119: "Daemon started" always showed a dash.
- #120: the Kubernetes live log showed a Python bytes repr on one line.
- #121: estimated cost stayed $0.00 while the token count beside it was non-zero.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinare.daemon import get_daemon_started_at
from coordinare.dashboard import _DASHBOARD_JS_SOURCES, DashboardStore
from coordinare.dashboard.store import _daemon_started_at_iso
from coordinare.services.kubernetes_runtime import KubernetesRuntime, _decode_pod_log
from tests.unit.dashboard_js_slices import usage_helpers_js

# --- #119 ---------------------------------------------------------------------- #


def test_start_time_is_an_iso_timestamp_matching_the_daemons_record() -> None:
    iso = _daemon_started_at_iso()
    assert iso is not None
    started = datetime.fromisoformat(iso)
    assert started.tzinfo is not None and started.utcoffset().total_seconds() == 0
    assert started.strftime("%Y%m%dT%H%M%SZ") == get_daemon_started_at()
    assert started <= datetime.now(UTC)


def test_metric_rows_returns_a_start_time_for_a_real_metrics_registry() -> None:
    """The original bug: ``build_info.labels()`` raises on a label-less Info
    metric, the except swallowed it, and started_at was always None."""
    from coordinare.metrics import METRICS

    _cycles, started_at = DashboardStore._metric_rows(METRICS)
    assert started_at == _daemon_started_at_iso()


def test_metric_rows_returns_a_start_time_without_any_registry() -> None:
    cycles, started_at = DashboardStore._metric_rows(None)
    assert cycles == 0 and started_at


def test_unparseable_start_record_degrades_to_none(monkeypatch) -> None:
    monkeypatch.setattr("coordinare.daemon.get_daemon_started_at", lambda: "not-a-timestamp")
    assert _daemon_started_at_iso() is None


# --- #120 ---------------------------------------------------------------------- #

_LOG_TEXT = "Setting up Claude Code...\n\nChecking installation status... \u2714\n\x1b[32mdone\x1b[0m\n"


def test_bytes_repr_from_the_client_is_decoded() -> None:
    """kubernetes client 36 builds the "str" by calling str() on the raw bytes."""
    repr_text = str(_LOG_TEXT.encode("utf-8"))
    assert repr_text.startswith("b'") and "\\n" in repr_text and "\\xe2" in repr_text
    assert _decode_pod_log(repr_text) == _LOG_TEXT


@pytest.mark.parametrize("raw", [
    _LOG_TEXT, _LOG_TEXT.encode("utf-8"), bytearray(_LOG_TEXT.encode("utf-8")),
    SimpleNamespace(data=_LOG_TEXT.encode("utf-8")),
])
def test_every_other_response_shape_is_decoded(raw) -> None:
    assert _decode_pod_log(raw) == _LOG_TEXT


def test_double_quoted_bytes_repr_is_decoded() -> None:
    text = "it's a log line\n"
    assert _decode_pod_log(str(text.encode())) == text


@pytest.mark.parametrize(("raw", "expected"), [
    (None, ""), (42, ""), ("", ""),
    ("b'unterminated", "b'unterminated"),
    ("because 'quoted' text", "because 'quoted' text"),
    (b"\xff\xfe bad utf8", "\ufffd\ufffd bad utf8"),
])
def test_odd_input_never_raises(raw, expected) -> None:
    assert _decode_pod_log(raw) == expected


def _runtime(returned) -> KubernetesRuntime:
    class FakeApi:
        def read_namespaced_pod_log(self, *, name, namespace, tail_lines=None):
            return returned

    return KubernetesRuntime(core_v1=FakeApi())


def test_tail_logs_returns_real_lines_with_utf8_and_no_ansi() -> None:
    lines = asyncio.run(_runtime(str(_LOG_TEXT.encode("utf-8"))).tail_logs("pod"))
    assert lines == ["Setting up Claude Code...", "", "Checking installation status... \u2714", "done"]


def test_tail_logs_keeps_only_the_requested_tail() -> None:
    raw = str("\n".join(f"line {i}" for i in range(10)).encode())
    assert asyncio.run(_runtime(raw).tail_logs("pod", lines=3)) == ["line 7", "line 8", "line 9"]


# --- #121 ---------------------------------------------------------------------- #


def _cost_js() -> str:
    performers = _DASHBOARD_JS_SOURCES["performers"]
    helpers = _DASHBOARD_JS_SOURCES["helpers"]

    def fn(source: str, name: str) -> str:
        match = re.search(rf"function {name}\(.*?\n\}}", source, re.DOTALL)
        assert match is not None, f"{name} is no longer in the shipped JS"
        return match.group(0)

    return "\n\n".join([
        fn(helpers, "esc"), fn(helpers, "fmtCost"), usage_helpers_js(), fn(performers, "parseTokenCount"),
        fn(performers, "derivePerformerTokenTotal"), fn(performers, "isStageRunning"),
        fn(performers, "estimateCost"), fn(performers, "costHtml"),
    ])


_COST_CHECKS = Path(__file__).resolve().parents[1] / "js" / "cost_estimate_checks.js"


def test_cost_estimate_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    assert shutil.which("node") is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "cost.js"
    js_path.write_text(_cost_js(), encoding="utf-8")
    result = subprocess.run(
        [shutil.which("node") or "node", str(_COST_CHECKS), str(js_path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 9, f"only {summary.group(1)} checks ran:\n{report}"


def test_every_cost_readout_goes_through_the_shared_estimate() -> None:
    """No page prints card_cost_estimate directly any more."""
    from coordinare.dashboard import _DASHBOARD_HTML

    shipped = _DASHBOARD_HTML + "".join(_DASHBOARD_JS_SOURCES.values())
    direct = re.findall(r"card_cost_estimate\s*\|\|\s*0\)\.toFixed", shipped)
    assert direct == []
    # The card detail, the non-running board card and (154) the active-run tile each read the same cost.
    assert shipped.count("? costHtml(sess, s) :") == 3
    # 154: the card's cost is read on its active-run tile (costHtml), which goes through usageCostHtml.
    assert "usageCostHtml(summary)" in shipped and "costHtml(sess, s)" in shipped
