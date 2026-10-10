"""Actual backend prompts must preserve human answers and relay provenance."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from performer.models import Score

BACKENDS = ["claude_code", "codex", "opencode", "opencode_compat", "hermes", "junie", "pi", "driver", "prime_agent", "openclaw"]
CORRECTION = "I have not authorized the next step. Ask and wait for my answer."
CONTINUATION = "Continue from the prior checkpoint. Next focus: implement step 2."


def prompt(backend: str, score: Score) -> str:
    module = importlib.import_module(f"performer.backends.{backend}")
    return module._build_task_prompt(score, []) if backend == "hermes" else module._build_task_prompt(score)


def score(**kwargs) -> Score:
    return Score(title="Synthetic", repo_url="https://github.com/example/sample", branch="synthetic", **kwargs)


@pytest.mark.parametrize("backend", BACKENDS)
def test_automatic_continuation_is_not_human_authorization(backend: str) -> None:
    text = prompt(backend, score(relay_feedback=[{"author_login": "coordinare", "source": "performer", "body": CONTINUATION}]))
    assert CONTINUATION in text
    assert "## Human Feedback" not in text
    assert "author: coordinare" in text
    assert "source: performer" in text
    assert "does not constitute human approval" in text


@pytest.mark.parametrize("backend", BACKENDS)
def test_current_human_correction_and_answer_survive_alongside_continuation(backend: str) -> None:
    text = prompt(backend, score(
        relay_feedback=[{"author_login": "coordinare", "body": CONTINUATION}],
        clarifications=[{"source": "issue", "comment_id": "123", "author": "human", "body": CORRECTION},
                        {"questions": ["Return blank on empty input?"], "answer": "Yes, return an empty string."}],
    ))
    for expected in [CORRECTION, "source: issue", "comment_id: 123", "author: human", "Return blank on empty input?", "Yes, return an empty string.", CONTINUATION]:
        assert expected in text
    assert "## Human Feedback" not in text


@pytest.mark.parametrize("backend", BACKENDS)
def test_review_identity_and_inline_location_are_preserved(backend: str) -> None:
    text = prompt(backend, score(relay_feedback=[{
        "author_login": "reviewer", "source": "review", "body": "Please change this case.",
        "comments": [{"body": "Keep this regression.", "path": "sample.py", "line": 12}],
    }]))
    for expected in ["author: reviewer", "source: review", "Please change this case.", "sample.py:12", "Keep this regression."]:
        assert expected in text


@pytest.mark.parametrize("backend", BACKENDS)
def test_unknown_feedback_source_is_not_invented(backend: str) -> None:
    text = prompt(backend, score(relay_feedback=[{"body": "Continue after checking the tests."}]))
    assert "source unspecified" in text
    assert "## Human Feedback" not in text


@pytest.mark.asyncio
async def test_junie_resumed_feedback_does_not_claim_a_human_source() -> None:
    from performer.backends.junie import JunieBackend

    backend = JunieBackend()
    backend._stand = SimpleNamespace(path="/tmp/synthetic")
    backend._original_prompt = "original task"
    backend.stop = AsyncMock()
    backend._launch = AsyncMock()
    await backend.relay_feedback(CONTINUATION)
    text = backend._launch.await_args.args[0]
    assert "## Human Feedback" not in text
    assert "source unspecified" in text
    assert CONTINUATION in text and "original task" in text


@pytest.mark.parametrize("raiser", ["reviewing", "qa", "security", "closing_review"])
def test_production_feedback_bounce_retains_its_raising_stage(raiser: str) -> None:
    from coordinare.graph.nodes.monitor.verdict import _stamp_feedback_bounce

    feedback = _stamp_feedback_bounce({}, [{"body": "Address this validation finding."}], raiser, "a" * 40)
    text = prompt("claude_code", score(relay_feedback=feedback))
    assert f"stage: {raiser}" in text
    assert "Address this validation finding." in text


def test_inline_feedback_retains_its_raising_stage() -> None:
    text = prompt("claude_code", score(relay_feedback=[{
        "author_login": "reviewer", "comments": [{"body": "Retain this assertion.", "path": "sample.py", "line": 9, "raiser": "qa"}],
    }]))
    assert "sample.py:9" in text and "Retain this assertion." in text
    assert "stage: qa" in text
