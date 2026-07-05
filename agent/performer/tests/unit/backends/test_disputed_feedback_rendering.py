"""Spec 126 (Copilot review) — every backend that renders relay_feedback must
also render disputed_feedback into the task prompt, or a reviewer/security/qa
stage silently loses the dispute-adjudication context.

Guards against a new backend being added with a relay block but no dispute
block (the exact gap Copilot found in opencode_compat and pi).
"""
from __future__ import annotations

import importlib

import pytest

from performer.models import Score

# Every backend module that defines its own _build_task_prompt. openclaw is
# intentionally absent: it imports opencode._build_task_prompt (reuse), so it
# inherits the dispute block automatically.
_OWN_BUILDER_BACKENDS = [
    "claude_code",
    "codex",
    "hermes",
    "junie",
    "opencode",
    "opencode_compat",
    "pi",
]


def _score_with_dispute() -> Score:
    return Score(
        title="T",
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
        role="reviewing",
        disputed_feedback=[
            {"id": "fb-7", "body": "the null check", "reason": "already guarded upstream"},
        ],
    )


def _build(module, score: Score) -> str:
    """Call a backend's _build_task_prompt, accommodating hermes' extra
    positional ``queued_feedback`` argument."""
    build = module._build_task_prompt
    if module.__name__.endswith(".hermes"):
        return build(score, [])
    return build(score)


@pytest.mark.parametrize("backend_name", _OWN_BUILDER_BACKENDS)
def test_disputed_feedback_rendered_in_task_prompt(backend_name: str) -> None:
    module = importlib.import_module(f"performer.backends.{backend_name}")
    prompt = _build(module, _score_with_dispute())

    assert "Disputed feedback to adjudicate" in prompt, (
        f"{backend_name}._build_task_prompt drops disputed_feedback"
    )
    # The specific disputed item (id + reason) must reach the model.
    assert "fb-7" in prompt
    assert "already guarded upstream" in prompt


@pytest.mark.parametrize("backend_name", _OWN_BUILDER_BACKENDS)
def test_no_dispute_section_when_none(backend_name: str) -> None:
    module = importlib.import_module(f"performer.backends.{backend_name}")
    score = Score(
        title="T",
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
    )
    prompt = _build(module, score)

    assert "Disputed feedback to adjudicate" not in prompt
