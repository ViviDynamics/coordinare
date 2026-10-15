"""153: the needs-attention signal (tab title, favicon, nav badges, Dashboard banner).

The derivation is a pure function in ``attention.js``. Its logic is checked under
node (``tests/js/attention_checks.js``). The server adds no key: these tests pin
the snapshot fields the derivation reads, so a rename there fails here first.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from coordinare import vocabulary
from coordinare.dashboard import (
    _DASHBOARD_HTML,
    _DASHBOARD_JS_FILES,
    _DASHBOARD_JS_SOURCES,
    DashboardStore,
)
from tests.unit.dashboard_js_slices import app_js, shipped_function, time_helpers_js
from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_health, _make_mock_metrics

_CHECKS = Path(__file__).resolve().parents[1] / "js" / "attention_checks.js"


def _attention_js() -> str:
    return _DASHBOARD_JS_SOURCES["attention"]


# --- wiring -------------------------------------------------------------------- #


def test_attention_script_is_registered_before_the_application_script() -> None:
    assert "attention" in _DASHBOARD_JS_FILES
    assert _DASHBOARD_JS_FILES[-1] == "app"
    assert _DASHBOARD_JS_FILES.index("attention") < _DASHBOARD_JS_FILES.index("app")


def test_the_application_script_composes_titles_and_refreshes_the_signal() -> None:
    app = app_js()
    assert "setPageTitle(" in app
    assert "document.title =" not in app, "every page title must go through setPageTitle"
    assert "updateAttention(scoped)" in app  # 139: the scoped snapshot, not the whole of it
    assert "attentionLabelOf" not in app


def test_only_the_count_is_a_polite_live_region_and_dismiss_is_a_labelled_button() -> None:
    """The rows carry ages that tick every second. A live region around them
    would be read out continuously, so only the count announces."""
    section = re.search(r'<section id="attention-banner"[^>]*>', _DASHBOARD_HTML)
    assert section is not None
    assert "aria-live" not in section.group(0)
    assert 'aria-label="Needs attention"' in section.group(0)
    assert '<strong id="attention-count" aria-live="polite"></strong>' in _DASHBOARD_HTML
    button = re.search(r'<button type="button" class="action-btn" onclick="dismissAttention\(\)"[^>]*>Dismiss</button>', _DASHBOARD_HTML)
    assert button is not None and 'aria-label="Dismiss this needs-attention list"' in button.group(0)


def test_the_relevant_nav_items_carry_a_badge() -> None:
    for name in ("dashboard", "performers"):
        link = re.search(rf'<a href="/[a-z]*" class="nav-link" data-attention="{name}"[^>]*>.*?</a>', _DASHBOARD_HTML)
        assert link is not None, f"no nav link carries data-attention={name}"
        assert 'class="nav-badge" hidden' in link.group(0)


def test_the_favicon_link_is_the_one_the_badge_redraws() -> None:
    assert '<link rel="icon" type="image/svg+xml" href="/static/coordinare-mark.svg">' in _DASHBOARD_HTML


def test_the_glossary_term_matches_the_banner_label() -> None:
    concepts = {term["code"]: term["label"] for term in vocabulary.payload()["concepts"]}
    assert concepts["needs_human"] == "Needs human"
    assert 'needs_human: \'Needs human\'' in _attention_js()


def test_no_attention_text_uses_a_dash_as_punctuation() -> None:
    for text in (_attention_js(), _CHECKS.read_text(encoding="utf-8")):
        assert "\u2014" not in text and "\u2013" not in text


# --- the snapshot fields the derivation reads ----------------------------------- #


def test_the_snapshot_carries_the_fields_the_derivation_reads() -> None:
    store = DashboardStore()
    daemon = _make_mock_daemon(phase="blocked")
    daemon.state["active_sessions"] = {
        "PVTI_153": {
            "phase": "blocked",
            "performer_stage": "implementing",
            "current_card": {"title": "Add login", "issue_number": 57, "issue_url": "https://github.com/acme/app/issues/57"},
            "open_questions": ["Should we use OAuth2?"],
            "last_blocked_notified_at": datetime(2026, 10, 13, 9, tzinfo=UTC),
        },
    }
    health = _make_mock_health([{
        "name": "github", "status": "unavailable", "required": True,
        "checked_at": "2026-10-13T09:01:00+00:00", "details": "403 from API",
    }])
    snapshot = store.build_snapshot(daemon, _make_mock_metrics(), health)

    (session,) = snapshot["active_sessions"]
    for key in ("card_id", "phase", "card_title", "issue_number", "issue_url", "performer_stage"):
        assert key in session, f"session summary lost {key!r}"
    (question,) = snapshot["open_questions"]
    for key in ("card_id", "card_number", "card_title", "stage", "text", "asked_at", "issue_url"):
        assert key in question, f"open question lost {key!r}"
    (subsystem,) = snapshot["subsystems"]
    for key in ("name", "status", "required", "checked_at", "details"):
        assert key in subsystem, f"subsystem row lost {key!r}"
    assert subsystem["status"] == "unavailable"


def test_the_snapshot_has_no_review_wait_flag_so_that_kind_is_not_derived() -> None:
    """The derivation leaves a PR wait out because the snapshot cannot tell a
    human review from CI. If a flag ever appears, revisit that choice here."""
    daemon = _make_mock_daemon(phase="monitoring_pr")
    daemon.state["active_sessions"] = {"PVTI_1": {"phase": "monitoring_pr", "current_card": {"title": "T"}}}
    snapshot = DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    (session,) = snapshot["active_sessions"]
    assert not {"review_required", "awaiting_review", "review_decision", "ci_state"} & set(session)


# --- the client under node ------------------------------------------------------ #


def test_attention_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "attention.js"
    js_path.write_text(
        "\n\n".join([shipped_function("helpers", "esc"), time_helpers_js(), _attention_js()]),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(_CHECKS), str(js_path)], capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 60, f"only {summary.group(1)} checks ran:\n{report}"
