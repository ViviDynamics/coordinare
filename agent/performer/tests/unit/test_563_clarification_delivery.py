"""Human clarification delivery at the actual backend prompt boundaries."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from performer.models import Score

BACKENDS = ["claude_code", "codex", "opencode", "opencode_compat", "hermes", "junie"]
BODY = 'Please add test_score_word_nonletters_and_case: score_word("QiUx?! 42") == 20.'


def _score() -> Score:
    return Score(
        title="QA clarification", repo_url="https://github.com/org/sample", branch="qa",
        clarifications=[
            {"source": "issue", "comment_id": "6082656906", "author": "human", "body": BODY},
            {"questions": ["Retain existing tests?"], "answer": "Yes, all existing tests."},
        ],
    )


def _assert_delivery(text: str) -> None:
    assert BODY in text
    assert "6082656906" in text
    assert "human" in text
    assert "issue" in text
    assert "Retain existing tests?" in text
    assert "Yes, all existing tests." in text


@pytest.mark.parametrize("backend", BACKENDS)
def test_issue_comment_reaches_task_prompt(backend: str) -> None:
    module = importlib.import_module(f"performer.backends.{backend}")
    prompt = module._build_task_prompt(_score(), []) if backend == "hermes" else module._build_task_prompt(_score())
    _assert_delivery(prompt)


@pytest.mark.parametrize("module_name,class_name", [("hermes", "HermesBackend"), ("openclaw", "OpenClawBackend")])
def test_issue_comment_reaches_card_document(tmp_path: Path, module_name: str, class_name: str) -> None:
    module = importlib.import_module(f"performer.backends.{module_name}")
    backend = getattr(module, class_name)()
    card = backend._write_card_docs(tmp_path, _score())
    assert card is not None
    _assert_delivery(card.read_text())


def test_issue_comment_reaches_assessor_intake_without_consuming_qa_round() -> None:
    from performer.workflows.assessor.intake import build_intake

    score = _score()
    score.clarifications[1] = {"question": "Retain existing tests?", "answer": "Yes, all existing tests."}
    intake = build_intake(score)
    _assert_delivery(intake.as_text())
    assert intake.answered_rounds == 1
    assert all("question" in c for c in intake.clarifications)


def test_issue_comment_reaches_architect_intake(tmp_path: Path) -> None:
    from performer.workflows.architect.intake import build_intake

    score = _score()
    score.clarifications[1] = {"question": "Retain existing tests?", "answer": "Yes, all existing tests."}
    _assert_delivery(build_intake(score, tmp_path).as_text())
