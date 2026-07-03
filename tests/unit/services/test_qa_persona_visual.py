"""Spec 120 US3: the QA persona guides the performer to capture visual evidence.

Spec 120 pins the capture path to the pre-installed Playwright Python tooling
(rather than the earlier "use whatever browser tooling is present" wording) so
the model has a concrete, reliable recipe.  The persona must still require
app-boot proof and at least one visual_evidence artifact for a visual change.
"""

from __future__ import annotations

from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS


def test_qa_persona_prescribes_playwright_capture():
    qa = DEFAULT_INSTRUCTIONS["qa"].lower()
    # Playwright Python is the pre-installed, prescribed capture tool.
    assert "playwright" in qa
    assert "screenshot" in qa


def test_qa_persona_still_requires_boot_proof_and_evidence():
    qa = DEFAULT_INSTRUCTIONS["qa"]
    assert "app_boot_check" in qa
    assert "visual_evidence" in qa
    assert "visual_validation_required" in qa
