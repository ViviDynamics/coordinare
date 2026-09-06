"""165 FR-010 last mile: the briefs reach the readers' prompts, only theirs,
and every backend renders them."""
from __future__ import annotations

import inspect
import re

import pytest
from performer.backends._card_docs import brief_prompt_sections

_IMPL = {
    "summary": "Add deliverable categories to time entries.",
    "milestones": [
        {"goal": "migration and model", "scope": ["db/migrate", "app/models/time_entry.rb"], "done_when": "model spec passes"},
        {"goal": "form select", "scope": ["app/views/time_entries"], "done_when": "select renders"},
    ],
    "modules": [{"path": "app/models/", "note": "TimeEntry gains a category"}],
    "data_model": {"changes": [{"kind": "column", "name": "time_entries.deliverable_category", "note": "string, required"}]},
    "interfaces": [],
    "risks": ["existing entries need a default"],
    "size": "large",
}
_DOCS = {
    "summary": "Time entries now carry a deliverable category.",
    "docs": [{"topic": "Deliverable categories", "location": "docs/wiki/time-tracking.md", "say": "what a category is and how it is chosen"}],
    "modules": [{"path": "app/models/", "note": "n"}],
}


class _Score:
    def __init__(self, role="implementing", impl=None, docs=None, single=False):
        self.role = role
        self.implementation_brief = impl or {}
        self.documentation_brief = docs or {}
        self.implementer_single_turn = single


def test_implementer_sees_milestones_and_the_no_documentation_rule():
    text = "\n".join(brief_prompt_sections(_Score(impl=_IMPL)))
    assert "Implementation Brief" in text
    assert "1. **migration and model**" in text and "2. **form select**" in text
    assert "time_entries.deliverable_category" in text
    assert "existing entries need a default" in text
    assert "Do not create or edit documentation" in text
    assert "SINGLE TURN" not in text


def test_small_blueprint_adds_the_single_turn_instruction():
    text = "\n".join(brief_prompt_sections(_Score(impl={**_IMPL, "size": "small"}, single=True)))
    assert "SINGLE TURN" in text and "Do not emit PARTIAL_PROGRESS" in text


def test_documenter_sees_topics_and_the_tree_rule_but_no_milestones():
    text = "\n".join(brief_prompt_sections(_Score(role="documenting", docs=_DOCS, impl=_IMPL)))
    assert "Documentation Brief" in text
    assert "docs/wiki/time-tracking.md" in text
    assert "only under the documentation tree" in text
    assert "migration and model" not in text and "Implementation Brief" not in text


@pytest.mark.parametrize("role", ["assessing", "architecting", "reviewing", "security", "qa", "closing_review"])
def test_other_roles_render_nothing_even_with_briefs_present(role):
    assert brief_prompt_sections(_Score(role=role, impl=_IMPL, docs=_DOCS)) == []


def test_no_brief_renders_nothing():
    assert brief_prompt_sections(_Score()) == []
    assert brief_prompt_sections(_Score(role="documenting")) == []
    assert brief_prompt_sections(_Score(impl={"milestones": []})) == []


@pytest.mark.parametrize("backend", ["claude_code", "junie", "hermes", "pi", "opencode_compat", "codex", "opencode"])
def test_every_prompt_builder_renders_the_briefs(backend):
    import importlib

    mod = importlib.import_module(f"performer.backends.{backend}")
    src = inspect.getsource(mod._build_task_prompt)
    assert re.search(r"brief_prompt_sections\(score\)", src), f"{backend}._build_task_prompt does not render the briefs"


def test_codex_prompt_actually_contains_the_brief():
    from performer.backends.codex import _build_task_prompt
    from performer.models import Score

    score = Score(title="t", description="d", repo_url="https://github.com/o/r.git", branch="b",
                  role="implementing", implementation_brief=_IMPL, implementer_single_turn=False)
    prompt = _build_task_prompt(score)
    assert "Implementation Brief" in prompt and "form select" in prompt
