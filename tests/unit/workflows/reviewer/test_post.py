"""Spec 169 FR-010, FR-011: exactly one review; inline comments only for findings
inside a hunk, the rest and the body-anchored ones in the body; fixed
dispositions named; COMMENT when clean; a failed post is an error the caller
turns into a hold."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from performer.workflows.reviewer.post import (
    attribution_header,
    build_review,
    post_review,
    pr_number_from_url,
)

from tests.unit.workflows.reviewer._fixtures import changed_files, finding


def test_pr_number_parsing():
    assert pr_number_from_url("https://github.com/o/r/pull/42") == 42
    assert pr_number_from_url("https://github.com/o/r/pull/42/") == 42
    assert pr_number_from_url("https://github.com/o/r") == 0
    assert pr_number_from_url("") == 0


def test_request_changes_puts_hunk_findings_inline_and_the_rest_in_the_body():
    fs = [finding(), finding(line=40, evidence="deep"), finding(path="", line=0, category="unaddressed_feedback", evidence="", origin="rule")]
    event, body, inline = build_review(fs, changed_files(), fixed_ids=["c9"], covered=2, header="<!-- h -->")
    assert event == "REQUEST_CHANGES"
    assert [(c["path"], c["line"]) for c in inline] == [("src/calc.py", 6)]
    assert "logic_error" in inline[0]["body"] and "return a / b" in inline[0]["body"]
    assert "`src/calc.py:40`" in body and "(pull request)" in body
    assert "Prior comments addressed: c9" in body
    assert body.startswith("<!-- h -->") and "CHANGES REQUESTED" in body
    assert "3 blocking finding(s); 1 inline" in body


def test_a_clean_review_is_one_comment_with_a_short_body():
    event, body, inline = build_review([], changed_files(), fixed_ids=[], covered=2, header="<!-- h -->")
    assert event == "COMMENT" and inline == []
    assert "APPROVED" in body and "2 changed file(s) covered" in body and "human reviewer" in body


def test_attribution_header_names_the_reviewer_workflow():
    h = attribution_header(SimpleNamespace(backend="codex", model="m"))
    assert h.startswith("<!-- coordinare-attribution origin=performer role=reviewing harness=codex model=m -->")
    assert "**Reviewer**" in h


def _score(pr_url="https://github.com/o/r/pull/7"):
    return SimpleNamespace(pr_url=pr_url, owner_repo=("o", "r"), effective_github_token="tok", backend="codex", model="m")


@pytest.mark.asyncio
async def test_post_review_calls_the_reviews_api_once_and_returns_the_url():
    posted = []

    async def poster(owner, repo, number, *, event, body, comments, token):
        posted.append((owner, repo, number, event, len(comments), token))
        return {"html_url": "https://github.com/o/r/pull/7#pullrequestreview-1"}

    out = await post_review(_score(), [finding()], changed_files(), [], 2, poster=poster)
    assert out.ok and out.url.endswith("review-1") and out.event == "REQUEST_CHANGES" and out.inline == 1
    assert posted == [("o", "r", 7, "REQUEST_CHANGES", 1, "tok")]


@pytest.mark.asyncio
async def test_post_failure_and_missing_pr_url_are_errors_not_exceptions():
    async def boom(*a, **k):
        raise RuntimeError("422 Unprocessable")

    out = await post_review(_score(), [finding()], changed_files(), [], 2, poster=boom)
    assert not out.ok and "422" in out.error
    missing = await post_review(_score(pr_url=""), [], changed_files(), [], 2, poster=boom)
    assert not missing.ok and "pr_url" in missing.error


def test_pr_number_tolerates_trailing_paths_and_fragments():
    assert pr_number_from_url("https://github.com/o/r/pull/42/files") == 42
    assert pr_number_from_url("https://github.com/o/r/pull/42#pullrequestreview-1") == 42
    assert pr_number_from_url("https://github.com/o/r/pull/42?diff=split") == 42
    assert pr_number_from_url("https://github.com/o/r/pulls") == 0


def test_attribution_header_is_the_077_marker_main_py_posts():
    """The prose path posts `_persona_tag`; the workflow's header must be byte for byte the same shape."""
    from performer.main import _persona_tag
    from performer.models import Score

    score = Score(title="t", repo_url="https://github.com/o/r", branch="b", backend="codex", model="m")
    assert attribution_header(score) == _persona_tag(score, "reviewing")
