"""391: the QA persona says what ``failures`` and ``executed_checks`` hold."""
from __future__ import annotations

from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS


def test_qa_persona_requires_failed_checks_and_object_failures():
    text = DEFAULT_INSTRUCTIONS["qa"]
    assert "every command you ran, including the ones that failed" in text
    assert "one object per defect, never a string" in text
