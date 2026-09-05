"""T040 — FR-012: the QA repair brief reaches the next implementer round.

Precedent: scanner_findings (spec 083) already travels this path. Before 164,
card_context carried scanner_findings, repair_mandate, disputed_feedback,
relay_feedback and prior_clarifications -- and nothing from QA, so a QA failure
reached the next round as prose, if it survived at all.

These tests drive the REAL dispatch helper and the REAL payload builder. An
earlier version of this file re-implemented the carrier logic inline; deleting
the carrier from dispatch_performer.py entirely left all of it green, which is
worth remembering as the shape of a test that proves nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

from coordinare.graph.nodes.dispatch_performer import inject_qa_findings
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.workspace import WorkspaceInfo

_PERFORMER_SRC = Path(__file__).resolve().parents[2] / "agent" / "performer" / "src"
if str(_PERFORMER_SRC) not in sys.path:
    sys.path.insert(0, str(_PERFORMER_SRC))

FINDING = {
    "file": "app/models/user.rb",
    "line": 42,
    "category": "unexpected_regression",
    "severity": "high",
    "criterion": "Users can sign in",
    "expected": "password field present",
    "observed": "password field absent after the change",
    "evidence": {"command": "pytest -q", "exit_code": 1, "output_excerpt": "AssertionError"},
    "repro_command": "pytest -q",
}


def test_findings_are_injected_for_the_implementer():
    card_context: dict = {}
    inject_qa_findings(card_context, {"qa_findings": [FINDING]}, role="implementing")
    assert card_context["qa_findings"] == [FINDING]


def test_findings_are_not_handed_back_to_qa_or_the_reviewer():
    """Handing QA its own prior findings would have it grade its own round; the
    brief is for the stage that can act on it."""
    for role in ("qa", "reviewing", "security", "documenting"):
        card_context: dict = {}
        inject_qa_findings(card_context, {"qa_findings": [FINDING]}, role=role)
        assert "qa_findings" not in card_context, role


def test_no_findings_means_the_key_is_absent_not_empty():
    card_context: dict = {}
    inject_qa_findings(card_context, {"qa_findings": []}, role="implementing")
    inject_qa_findings(card_context, {}, role="implementing")
    assert "qa_findings" not in card_context


def test_malformed_entries_are_filtered_rather_than_crashing_the_dispatch():
    card_context: dict = {}
    inject_qa_findings(
        card_context, {"qa_findings": [FINDING, "not a finding", None, 42]}, role="implementing"
    )
    assert card_context["qa_findings"] == [FINDING]


def test_findings_survive_the_payload_boundary_onto_score():
    """The end of the road: dispatch payload -> JobInitPayload -> Score.
    Score uses extra="ignore", so an undeclared field is dropped in silence."""
    svc = HTTPPerformerService(
        PerformerEndpointConfig(id="e", mode="ephemeral", roles=["implementer"], image="img")
    )
    card_context = {"id": "PVTI_1", "role": "implementing", "qa_findings": [FINDING]}
    ws = WorkspaceInfo(path=None, branch="b", repo_url="https://github.com/o/r.git")

    payload = svc._build_job_payload(card_context, ws)
    assert payload.metadata["qa_findings"] == [FINDING]

    from performer.models import Score

    score = Score(**{**payload.metadata, "title": "t",
                     "repo_url": "https://github.com/o/r.git", "branch": payload.branch})
    assert score.qa_findings == [FINDING]


def test_the_dedup_key_matches_the_scanner_findings_key():
    """monitor_performer dedups findings on (file, line, category). The QA
    finding must present that key or it cannot share the merge path."""
    from performer.workflows.qa.models import Finding

    payload = Finding(
        category="unexpected_regression", criterion="c", expected="e", observed="o"
    ).model_dump()
    assert all(k in payload for k in ("file", "line", "category", "severity"))
