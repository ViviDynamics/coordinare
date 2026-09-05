"""FR-012 last mile: the repair brief must reach the implementer's PROMPT.

Second review round, by hand: qa_findings was registered in the payload
contract, carried into card_context, declared on Score -- and rendered into no
prompt anywhere. The Change Protocol's step 4 was skipped, so the whole of US3
arrived on Score and was ignored.
"""
from __future__ import annotations

import inspect
import re

import pytest
from performer.backends._card_docs import qa_findings_prompt_section

FINDING = {
    "category": "unexpected_regression",
    "severity": "high",
    "criterion": "Users can sign in",
    "expected": "password field present",
    "observed": "password field absent after the change",
    "evidence": {"command": "pytest -q tests/test_signin.py", "exit_code": 1},
    "repro_command": "pytest -q tests/test_signin.py",
}


class _Score:
    def __init__(self, role="implementing", findings=None):
        self.role = role
        self.qa_findings = findings if findings is not None else [FINDING]


def test_the_section_renders_the_brief_for_the_implementer():
    text = "\n".join(qa_findings_prompt_section(_Score()))
    assert "QA Findings" in text
    assert "password field absent" in text
    assert "pytest -q tests/test_signin.py" in text
    assert "exited 1" in text


@pytest.mark.parametrize("role", ["qa", "reviewing", "security", "documenting", "assessing"])
def test_only_the_implementer_receives_it(role):
    """Handing QA its own prior findings would have it grade its own round."""
    assert qa_findings_prompt_section(_Score(role=role)) == []


def test_empty_findings_render_nothing():
    assert qa_findings_prompt_section(_Score(findings=[])) == []


def test_duplicates_collapse():
    """The regression scenario produced one finding per surface plus the
    judge's note -- three entries for one lost field."""
    text = "\n".join(qa_findings_prompt_section(_Score(findings=[FINDING, dict(FINDING), dict(FINDING)])))
    assert text.count("unexpected_regression") == 1


def test_the_section_never_prescribes_a_fix():
    """FR-014. It says what failed and how to reproduce, not how to fix."""
    text = "\n".join(qa_findings_prompt_section(_Score())).lower()
    for word in ("suggested fix", "you should change", "replace the", "patch:"):
        assert word not in text


@pytest.mark.parametrize(
    "backend", ["claude_code", "junie", "hermes", "pi", "opencode_compat", "codex", "opencode"]
)
def test_every_prompt_builder_renders_the_section(backend):
    """Each backend builds its own prompt (no shared builder), so each must call
    the shared section. Source-inspected so a new backend that forgets is
    caught here, and not by an implementer who never saw the brief."""
    import importlib

    mod = importlib.import_module(f"performer.backends.{backend}")
    src = inspect.getsource(mod._build_task_prompt)
    assert re.search(r"qa_findings_prompt_section\(score\)", src), (
        f"{backend}._build_task_prompt does not render the QA findings section"
    )


def test_claude_code_prompt_actually_contains_the_brief():
    """One real end-to-end call, not only source inspection."""
    from performer.backends.claude_code import _build_task_prompt
    from performer.models import Score

    score = Score(
        title="t", description="d", repo_url="https://github.com/o/r.git", branch="b",
        role="implementing", qa_findings=[FINDING],
    )
    prompt = _build_task_prompt(score)
    assert "QA Findings" in prompt
    assert "password field absent" in prompt
