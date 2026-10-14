"""#138, #140, #144: conductor status in plain words, "check" vs "revision
round", and the small duplicate or unclear labels.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from coordinare import config_descriptors as cd
from coordinare import vocabulary
from coordinare.config import ProjectConfiguration
from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.helpers import format_phase_label
from coordinare.dashboard.store import _poll_interval
from tests.unit.dashboard_js_slices import label_helpers_js, page_source, shipped_function

_PERFORMERS = _DASHBOARD_JS_SOURCES["performers"]
_WORKFLOW = _DASHBOARD_JS_SOURCES["workflow"]
_SHIPPED = _DASHBOARD_HTML + "".join(_DASHBOARD_JS_SOURCES.values())

# --- 138 ----------------------------------------------------------------------- #


def test_conductor_states_read_as_plain_status_words() -> None:
    assert vocabulary.phase_label("monitoring_performer") == "Watching an agent"
    assert vocabulary.phase_label("monitoring_agent") == "Watching an agent"
    assert vocabulary.phase_label("monitoring_pr") == "Waiting on review"
    assert vocabulary.phase_label("dispatching") == "Starting an agent"
    for term in vocabulary.PHASES.values():
        assert "Monitoring" not in term.label and "Performer" not in term.label


def test_server_side_phase_label_comes_from_the_glossary() -> None:
    assert format_phase_label("monitoring_performer") == "Watching an agent"
    assert format_phase_label("no_such_state") == "no_such_state"


def test_the_word_phase_is_gone_from_the_card_ui() -> None:
    detail = _PERFORMERS[_PERFORMERS.index("function renderCardDetailContent"):]
    detail = detail[:detail.index("function showPerformerDetail")]
    assert ">Phase<" not in detail and "Phase:" not in detail
    assert ">Stage</td>" in detail
    assert ">Phase:" not in _PERFORMERS


def test_headline_is_built_by_the_sentence_helper_with_the_state_in_a_tooltip() -> None:
    assert "phaseEl.textContent = conductorHeadline(s, activeSess);" in page_source()
    assert "phaseEl.title = 'Conductor state: '" in page_source()


def test_snapshot_poll_interval() -> None:
    assert _poll_interval(SimpleNamespace(poll_interval_seconds=30)) == 30
    for config in (None, SimpleNamespace(), SimpleNamespace(poll_interval_seconds=0),
                   SimpleNamespace(poll_interval_seconds=True), SimpleNamespace(poll_interval_seconds="30")):
        assert _poll_interval(config) is None


# --- 140 ----------------------------------------------------------------------- #


def test_board_polls_are_called_checks_everywhere() -> None:
    for old in ("Cycles Completed", "Cycles completed", "Last Cycle Duration", "Recent cycles",
                "No cycles completed yet", "Cycle already running", "work cycle", "next cycle",
                ">Cycles<", "Cycles: <strong>"):
        assert old not in _SHIPPED, old
    for new in ("Checks completed", "Last check duration", "Recent checks", "No checks completed yet",
                "A check is already running"):
        assert new in _SHIPPED, new
    assert "wait for next check" in _WORKFLOW


def test_feedback_loops_are_called_revision_rounds() -> None:
    field = ProjectConfiguration.model_fields["max_feedback_cycles"]
    setting = cd.setting_from_field("global", "max_feedback_cycles", field, 5)
    assert setting.label == "Max revision rounds per PR (max_feedback_cycles)"
    assert setting.key.endswith("max_feedback_cycles")  # the config key itself is unchanged
    assert "revision_round" in vocabulary.CONCEPTS
    example = (Path(__file__).resolve().parents[2] / "config.example.yaml").read_text(encoding="utf-8")
    assert "Maximum revision rounds per PR" in example


# --- 144 ----------------------------------------------------------------------- #


def test_small_labels() -> None:
    assert ">Check Board Now<" not in _SHIPPED and ">Check now</button>" in page_source()
    assert "Last poll" not in _SHIPPED and "Last checked" in _SHIPPED
    assert "Active / Idle / Max" not in _SHIPPED
    assert '<label for="af-filter">Card</label>' not in page_source()
    assert 'aria-label="Filter activity by card"' in page_source()
    assert "Observer Verdicts" not in _SHIPPED and "Progress checks (recent)" in _PERFORMERS
    assert "progress_check" in vocabulary.CONCEPTS
    assert "Card Tokens" not in _SHIPPED and 'id="card-tokens-label"' in page_source()
    assert "toFixed(4)" not in _SHIPPED


def test_activity_panel_no_longer_repeats_live() -> None:
    assert 'aria-live="polite"></span>' in page_source()
    assert "el.textContent = live ? '' : 'Not live';" in page_source()


def test_naming_batch_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "naming.js"
    js_path.write_text(
        "\n\n".join(
            # 139: the headline reads the scoped view, which lives in symphonies.js.
            [shipped_function("helpers", "esc"), label_helpers_js(), _DASHBOARD_JS_SOURCES["symphonies"]]
            + [shipped_function("helpers", name) for name in (
                "fmtCost", "busyText", "cardRefText", "conductorHeadline", "refreshConductorHeadline",
                "nextCheckSeconds",
            )],
        ),
        encoding="utf-8",
    )
    checks = Path(__file__).resolve().parents[1] / "js" / "naming_batch_checks.js"
    result = subprocess.run([node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 20, f"only {summary.group(1)} checks ran:\n{report}"
