"""Spec 120 US3: the QA persona permits any in-image browser tooling for capture.

The persona must free the performer to drive whatever browser tooling is present
(Chromium / Playwright / a screenshot CLI) — no single mandated tool — while
still requiring app-boot proof and at least one visual_evidence artifact for a
visual change.
"""

from __future__ import annotations

from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS


def test_qa_persona_permits_any_browser_tooling():
    qa = DEFAULT_INSTRUCTIONS["qa"].lower()
    assert "however is easiest" in qa
    # mentions concrete options without mandating one
    assert "chromium" in qa
    assert "playwright" in qa
    assert "no single tool is mandated" in qa


def test_qa_persona_still_requires_boot_proof_and_evidence():
    qa = DEFAULT_INSTRUCTIONS["qa"]
    assert "app_boot_check" in qa
    assert "visual_evidence" in qa
    assert "visual_validation_required" in qa
