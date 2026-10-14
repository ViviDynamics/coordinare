"""#137: each stage and each agent has exactly one display name.

A stage says what is happening (Implement); an agent says who does it
(Implementer). They are shown as a pair. Several codes are both a stage and
an agent with different labels, so every lookup is by kind.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinare import vocabulary
from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from coordinare.lifecycle import ROLE_TO_STAGE
from coordinare.services import persona_service
from tests.unit.dashboard_js_slices import page_source

_HELPERS = _DASHBOARD_JS_SOURCES["helpers"]
_PERFORMERS = _DASHBOARD_JS_SOURCES["performers"]

_STAGE_NAMES = {
    "assessing": "Assess", "architecting": "Plan", "implementing": "Implement",
    "reviewing": "Review", "security": "Security", "qa": "QA",
    "documenting": "Docs", "closing_review": "Close",
}
_AGENT_NAMES = {
    "assessor": "Assessor", "architect": "Architect", "implementer": "Implementer",
    "reviewer": "Reviewer", "security": "Security reviewer", "qa": "QA",
    "tech_writer": "Documenter", "closer": "Closer",
    "curator": "Intake (issue triage)", "advocate": "Issue responder",
}


@pytest.mark.parametrize(("code", "name"), sorted(_STAGE_NAMES.items()))
def test_stage_names(code: str, name: str) -> None:
    assert vocabulary.stage_label(code) == name


@pytest.mark.parametrize(("code", "name"), sorted(_AGENT_NAMES.items()))
def test_agent_names(code: str, name: str) -> None:
    assert vocabulary.agent_label(code) == name


def test_display_names_are_unique_within_a_kind() -> None:
    """Exactly one name each: no two stages share a label, no two agents do."""
    for group in (vocabulary.STAGES, vocabulary.AGENTS):
        labels = [term.label for term in group.values()]
        assert len(labels) == len(set(labels)), labels


def test_a_code_that_is_both_gets_the_right_name_for_its_kind() -> None:
    assert vocabulary.stage_label("security") == "Security"
    assert vocabulary.agent_label("security") == "Security reviewer"


@pytest.mark.parametrize(("stage", "pair"), [
    ("implementing", "Implement · Implementer"),
    ("architecting", "Plan · Architect"),
    ("documenting", "Docs · Documenter"),
    ("closing_review", "Close · Closer"),
    ("security", "Security · Security reviewer"),
    ("qa", "QA · QA"),
])
def test_stage_is_shown_with_its_agent(stage: str, pair: str) -> None:
    assert vocabulary.stage_with_agent(stage) == pair


def test_every_lifecycle_stage_has_an_agent_in_the_glossary() -> None:
    for role, stage in ROLE_TO_STAGE.items():
        assert vocabulary.STAGE_AGENT[stage] == role
        assert stage in vocabulary.STAGES and role in vocabulary.AGENTS
    for stage, agent in vocabulary.STAGE_AGENT.items():
        assert stage in vocabulary.STAGES and agent in vocabulary.AGENTS


def test_unknown_codes_are_shown_as_they_are() -> None:
    assert vocabulary.stage_label("translating") == "translating"
    assert vocabulary.agent_label("translator") == "translator"
    assert vocabulary.stage_with_agent("translating") == "translating"
    assert vocabulary.stage_with_agent(None) == ""


def test_payload_carries_the_kind_maps() -> None:
    payload = vocabulary.payload()
    assert payload["stage_labels"]["closing_review"] == "Close"
    assert payload["agent_labels"]["tech_writer"] == "Documenter"
    assert payload["stage_agent"]["documenting"] == "tech_writer"


def test_no_call_site_labels_a_stage_or_role_through_the_phase_lookup() -> None:
    shipped = page_source() + _PERFORMERS
    assert not re.findall(r"(?:humanPhase|formatPhaseLabel)\([^)]*(?:performer_stage|selectedRole|\.role)[^)]*\)", shipped)
    assert "esc(r.role) + '</td>'" not in _PERFORMERS
    assert "' + esc(p.role) + '</label>'" not in page_source()


def test_closer_persona_no_longer_calls_the_documenter_tech_writer() -> None:
    source = Path(persona_service.__file__).read_text(encoding="utf-8")
    closer = source[source.index('"closer": ('):]
    closer = closer[:closer.index('\n    ),')]
    assert "tech_writer" not in closer and "documenter" in closer


def _fn(source: str, name: str) -> str:
    match = re.search(rf"function {name}\(.*?\n\}}", source, re.DOTALL)
    assert match is not None, f"{name} is no longer in the shipped JS"
    return match.group(0)


def _sliced_js() -> str:
    return "\n\n".join(
        [_DASHBOARD_JS_SOURCES["vocabulary"]]
        + [_fn(_HELPERS, name) for name in (
            "esc", "vocabularyLabel", "humanPhase", "vocabularyKindLabel", "stageLabel", "agentLabel",
            "agentForStage", "stageAgentLabel", "personaStageNote", "roleOrStageLabel",
        )],
    )


def test_stage_agent_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "names.js"
    js_path.write_text(_sliced_js(), encoding="utf-8")
    checks = Path(__file__).resolve().parents[1] / "js" / "stage_agent_name_checks.js"
    result = subprocess.run([node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 20, f"only {summary.group(1)} checks ran:\n{report}"
