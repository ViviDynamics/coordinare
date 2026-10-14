"""#141: one name and one id format for a run, in the glossary, the server's text and the markup.

The browser behaviour (the short id, the copy action, the technical section, hostile ids in a real
page) is in tests/e2e/test_141_run_naming.py. The node checks for the helpers are in
test_141_run_naming_js.py.
"""
from __future__ import annotations

import re
from pathlib import Path

from coordinare import vocabulary
from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import DASHBOARD_HTML_BUDGET_BYTES

_REPO = Path(__file__).resolve().parents[2]
_STATIC = _REPO / "src" / "coordinare" / "dashboard" / "dashboard_static"
_TEMPLATES = _REPO / "src" / "coordinare" / "dashboard" / "templates"

# The words the issue retires from every view. Attempt numbers are not shown at all (#141).
_RETIRED = re.compile(r"\b(session|sessions|attempt|attempts|agent session)\b", re.IGNORECASE)


def _all_terms() -> list[vocabulary.Term]:
    return [term for terms in vocabulary.GROUPS.values() for term in terms.values()]


def test_run_is_the_one_name_for_an_agent_working_on_a_stage() -> None:
    run = vocabulary.CONCEPTS["run"]
    assert run.label == "Run"
    assert run.plural == "Runs"
    assert "one agent working on one stage of one card" in run.description.lower()


def test_the_short_id_format_is_in_the_glossary() -> None:
    entry = vocabulary.CONCEPTS["run_id"]
    assert entry.label == "Run id"
    assert "run 299a83" in entry.description
    assert "Copy" in entry.description


def test_the_container_is_explained_as_technical_only() -> None:
    entry = vocabulary.CONCEPTS["container"]
    assert entry.label == "Container"
    assert "technical section" in entry.description


def test_no_glossary_label_retires_a_word() -> None:
    offenders = [term.label for term in _all_terms() if _RETIRED.search(term.label) or _RETIRED.search(term.plural)]
    assert offenders == []


def test_the_script_the_page_loads_carries_the_new_terms() -> None:
    script = _DASHBOARD_JS_SOURCES["vocabulary"]
    assert '"code":"run_id"' in script
    assert '"code":"container"' in script


def test_the_stop_warning_names_a_run_not_a_session() -> None:
    source = (_REPO / "src" / "coordinare" / "cancel.py").read_text(encoding="utf-8")
    assert "no run id for this" in source
    assert "no session id" not in source


def test_the_page_has_no_session_label_left() -> None:
    assert "Agent Session" not in _DASHBOARD_HTML
    assert "Current run:" in _DASHBOARD_HTML
    assert len(_DASHBOARD_HTML.encode("utf-8")) <= DASHBOARD_HTML_BUDGET_BYTES


def test_no_shipped_script_shows_a_session_or_attempt_label() -> None:
    """A quoted string with spaces in the shipped scripts is prose the page can show.

    Element ids, class names and field names are one word and are not labels, so they are not
    scanned. None of the prose may say session or attempt.
    """
    offenders: list[str] = []
    for path in [*sorted(_STATIC.glob("*.js")), *sorted(_TEMPLATES.glob("*.js"))]:
        for literal in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"", path.read_text(encoding="utf-8")):
            text = literal[0] or literal[1]
            if " " in text and _RETIRED.search(text):
                offenders.append(f"{path.name}: {text}")
    assert offenders == []


def test_no_template_shows_a_session_or_attempt_label() -> None:
    visible = re.sub(r"<script.*?</script>|<style.*?</style>", "", _DASHBOARD_HTML, flags=re.DOTALL)
    visible = re.sub(r"<[^>]+>", " ", visible)
    assert _RETIRED.search(visible) is None, _RETIRED.search(visible)
