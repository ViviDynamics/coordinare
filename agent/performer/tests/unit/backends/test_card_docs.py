"""Unit tests for the shared card-docs prompt helper."""
from __future__ import annotations

from pathlib import Path

from performer.backends._card_docs import (
    card_doc_folder,
    card_docs_prompt_section,
)
from performer.models import Score


def _score(**kwargs) -> Score:
    defaults = dict(
        title="Task Title",
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
    )
    defaults.update(kwargs)
    return Score(**defaults)


class TestCardDocFolder:
    def test_with_issue_number(self) -> None:
        score = _score(title="Ability for an employee to enter hours", issue_number=70)
        assert card_doc_folder(score) == "docs/cards/70-ability-for-an-emplo"

    def test_without_issue_number(self) -> None:
        score = _score(title="Some Task")
        assert card_doc_folder(score) == "docs/cards/some-task"

    def test_empty_title_falls_back_to_untitled(self) -> None:
        score = _score(title="", issue_number=12)
        assert card_doc_folder(score) == "docs/cards/12-untitled"

    def test_special_chars_collapsed_to_hyphens(self) -> None:
        score = _score(title="Foo / Bar  !!  Baz", issue_number=1)
        assert card_doc_folder(score) == "docs/cards/1-foo-bar-baz"


class TestCardDocsPromptSection:
    def test_emits_section_when_folder_exists(self, tmp_path: Path) -> None:
        (tmp_path / "docs" / "cards" / "70-ability-for-an-emplo").mkdir(parents=True)
        score = _score(title="Ability for an employee to enter hours", issue_number=70)
        section = card_docs_prompt_section(score, tmp_path)
        joined = "\n".join(section)
        assert "## Card Documentation" in joined
        assert "docs/cards/70-ability-for-an-emplo/" in joined
        assert "plan.md" in joined

    def test_omits_section_when_folder_missing(self, tmp_path: Path) -> None:
        score = _score(title="Brand new card", issue_number=99)
        assert card_docs_prompt_section(score, tmp_path) == []

    def test_omits_section_when_stand_path_none(self) -> None:
        score = _score(title="anything", issue_number=1)
        assert card_docs_prompt_section(score, None) == []
