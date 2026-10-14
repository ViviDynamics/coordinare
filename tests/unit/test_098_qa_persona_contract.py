"""098: the QA persona states the new_test_files contract.

Issue #98: a QA turn named an existing test module in new_test_files and
wrote prose into it. The performer now refuses that write, and the persona must
also tell the model what the field is for, so a refused write is the exception
and not the routine.
"""

from __future__ import annotations

from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS


def test_qa_persona_says_new_test_files_must_be_new_paths():
    qa = DEFAULT_INSTRUCTIONS["qa"]
    assert "ONLY for new acceptance-test files" in qa
    assert "must be a NEW path that does not exist in the repository yet" in qa


def test_qa_persona_forbids_overwriting_existing_files():
    qa = DEFAULT_INSTRUCTIONS["qa"]
    assert "Existing files must never be overwritten" in qa
    assert "including existing test modules" in qa


def test_qa_persona_says_a_refused_declaration_is_a_note_not_a_failure():
    qa = DEFAULT_INSTRUCTIONS["qa"]
    assert "is not written and is listed in the QA report" in qa
    assert "does not fail the run" in qa


def test_qa_persona_puts_prose_in_the_report_not_in_test_files():
    qa = DEFAULT_INSTRUCTIONS["qa"]
    assert "never in a test file" in qa
    assert "`verification_steps` and the QA report" in qa
