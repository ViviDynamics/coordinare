"""#136: one glossary is the only source of display labels for internal codes.

The guard half of this file is the point: it fails when a conductor phase,
card stage, agent role or check outcome exists in the code with no entry in
``coordinare.vocabulary``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from coordinare import vocabulary
from coordinare.config import PerformersConfig
from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import _DASHBOARD_JS_ORDER, _DASHBOARD_JS_REVISIONS
from coordinare.graph import attribution
from coordinare.graph.nodes import dispatch_performer
from coordinare.graph.state import DaemonPhase
from coordinare.lifecycle import CANONICAL_ORDER, ROLE_TO_STAGE
from tests.unit.test_dashboard import _make_app

_HELPERS = _DASHBOARD_JS_SOURCES["helpers"]
_SRC = Path(__file__).resolve().parents[2] / "src" / "coordinare"

# --- guard: no code without a glossary entry ---------------------------------- #


@pytest.mark.parametrize("phase", get_args(DaemonPhase))
def test_every_conductor_phase_has_an_entry(phase: str) -> None:
    assert phase in vocabulary.PHASES, f"conductor phase {phase!r} has no glossary entry"


def _stage_codes() -> set[str]:
    return (
        set(ROLE_TO_STAGE.values())
        | set(dispatch_performer._STAGE_TO_ROLE)
        | set(attribution._STAGE_TO_ROLE)
    )


def _agent_codes() -> set[str]:
    configured = {name for name in PerformersConfig.model_fields if name != "default"}
    return (
        set(ROLE_TO_STAGE)
        | set(CANONICAL_ORDER)
        | set(dispatch_performer._STAGE_TO_ROLE.values())
        | set(attribution._STAGE_TO_ROLE.values())
        | configured
    )


def test_every_card_stage_has_an_entry() -> None:
    missing = _stage_codes() - set(vocabulary.STAGES)
    assert not missing, f"card stages with no glossary entry: {sorted(missing)}"


def test_every_agent_role_has_an_entry() -> None:
    missing = _agent_codes() - set(vocabulary.AGENTS)
    assert not missing, f"agent roles with no glossary entry: {sorted(missing)}"


def test_every_check_outcome_the_daemon_records_has_an_entry() -> None:
    recorded = set(re.findall(r'outcome="([a-z_]+)"', (_SRC / "daemon.py").read_text(encoding="utf-8")))
    assert recorded, "the daemon no longer records check outcomes the way this test reads them"
    missing = recorded - set(vocabulary.OUTCOMES)
    assert not missing, f"check outcomes with no glossary entry: {sorted(missing)}"


def test_the_guard_would_catch_a_new_code(monkeypatch) -> None:
    """The guards above are not vacuous: a stage added without a label fails them."""
    monkeypatch.setitem(ROLE_TO_STAGE, "translator", "translating")
    assert "translating" in _stage_codes() - set(vocabulary.STAGES)
    assert "translator" in _agent_codes() - set(vocabulary.AGENTS)


# --- the entries themselves ---------------------------------------------------- #


@pytest.mark.parametrize("group", sorted(vocabulary.GROUPS))
def test_every_entry_is_complete(group: str) -> None:
    for code, term in vocabulary.GROUPS[group].items():
        assert term.code == code
        assert term.label.strip() and term.plural.strip(), f"{group}.{code} has an empty label"
        assert term.description.strip().endswith("."), f"{group}.{code} needs a one-line description"
        assert "\n" not in term.description


def test_glossary_explains_the_five_nouns() -> None:
    assert {"symphony", "stage", "agent", "run", "check"} <= set(vocabulary.CONCEPTS)


def test_a_code_shared_by_two_groups_has_one_label() -> None:
    """`security`, `qa` and `advocate` are both a stage and an agent."""
    for code in set(vocabulary.STAGES) & set(vocabulary.AGENTS):
        assert vocabulary.STAGES[code].label == vocabulary.AGENTS[code].label, code
    for code in set(vocabulary.PHASES) & (set(vocabulary.STAGES) | set(vocabulary.AGENTS)):
        raise AssertionError(f"{code} is both a conductor phase and a stage or agent")


def _legacy_title_case(code: str) -> str:
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), code.replace("_", " "))


def test_introducing_the_glossary_changes_no_label() -> None:
    """#136 delivers the module with today's labels; renames come in #137 to #144."""
    for group in ("phases", "stages", "agents"):
        for code, term in vocabulary.GROUPS[group].items():
            assert term.label == _legacy_title_case(code), f"{group}.{code}"
    for code, term in vocabulary.OUTCOMES.items():
        assert term.label == code  # outcomes were always shown as their code


def test_label_lookup() -> None:
    assert vocabulary.label("monitoring_performer") == "Monitoring Performer"
    assert vocabulary.label("tech_writer") == "Tech Writer"
    assert vocabulary.label("not_a_code") == "not_a_code"
    assert vocabulary.label(None) == "" and vocabulary.label("") == ""
    assert vocabulary.labels()["closing_review"] == "Closing Review"


def test_payload_is_plain_json_and_the_script_embeds_it() -> None:
    payload = vocabulary.payload()
    assert json.loads(json.dumps(payload)) == payload
    script = vocabulary.javascript()
    embedded = script.split("var COORDINARE_VOCABULARY = ", 1)[1].rstrip().removesuffix(";")
    assert json.loads(embedded) == payload
    assert "</" not in script


# --- the dashboard reads it ---------------------------------------------------- #


def test_human_phase_no_longer_title_cases() -> None:
    body = re.search(r"function humanPhase\(phase\) \{.*?\n\}", _HELPERS, re.DOTALL)
    assert body is not None
    assert "toUpperCase" not in body.group(0) and "replace(" not in body.group(0)
    assert "vocabularyLabel(phase)" in body.group(0)
    assert "toUpperCase" not in _HELPERS


def test_glossary_script_is_generated_and_loads_first() -> None:
    assert _DASHBOARD_JS_ORDER[0] == "vocabulary"
    assert _DASHBOARD_JS_SOURCES["vocabulary"] == vocabulary.javascript()
    tags = re.findall(r'<script src="/static/([a-z]+)\.js\?v=([0-9a-f]+)"></script>', _DASHBOARD_HTML)
    assert [name for name, _ in tags] == list(_DASHBOARD_JS_ORDER)
    assert dict(tags)["vocabulary"] == _DASHBOARD_JS_REVISIONS["vocabulary"]


def test_glossary_routes() -> None:
    client: TestClient = _make_app()
    assert client.get("/api/vocabulary").json() == vocabulary.payload()
    script = client.get("/static/vocabulary.js")
    assert script.status_code == 200 and script.text == vocabulary.javascript()
    page = client.get("/glossary")
    assert page.status_code == 200 and 'id="glossary-section"' in page.text
    assert 'href="/glossary"' in page.text


def _sliced_js() -> str:
    def fn(name: str) -> str:
        match = re.search(rf"function {name}\(.*?\n\}}", _HELPERS, re.DOTALL)
        assert match is not None, f"{name} is no longer in helpers.js"
        return match.group(0)

    alias = "var formatPhaseLabel = humanPhase;"
    assert alias in _HELPERS
    return "\n\n".join([
        _DASHBOARD_JS_SOURCES["vocabulary"], fn("esc"), fn("vocabularyLabel"), fn("humanPhase"), alias,
        fn("renderGlossaryPage"),
    ])


def test_vocabulary_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "vocabulary.js"
    js_path.write_text(_sliced_js(), encoding="utf-8")
    checks = Path(__file__).resolve().parents[1] / "js" / "vocabulary_checks.js"
    result = subprocess.run([node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 18, f"only {summary.group(1)} checks ran:\n{report}"
