"""Spec 173 US1: the advocate gate. Every rule here is mutation-tested.

Each rule is a decision to speak in public on the project's behalf, which is
why the bar is the same as the reviewer's and the security scanner's: a claim
survives only when its evidence is in material the run actually read.
"""
from __future__ import annotations

import pytest
from performer.workflows.advocate.gate import (
    accept_classification,
    answer_is_grounded,
    claimed_paths,
    confident_enough,
    path_like_tokens,
    undeclared_citations,
    unknown_citations,
)
from performer.workflows.advocate.models import Classification, DocumentRead

README = DocumentRead(path="README.md", content="Run `make start` to boot.", read=True)
MISSING = DocumentRead(path="docs/absent.md", content="", read=False)


def _c(**over: object) -> Classification:
    base = {
        "issue_id": "I_1",
        "classification": "question",
        "confidence": 0.9,
        "reasoning": "covered by the readme",
        "answer": "Based on `README.md`: run make start.",
        "cited_documents": ["README.md"],
    }
    base.update(over)
    return Classification(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- grounding


def test_an_answer_citing_a_read_document_is_posted() -> None:
    ok, why = answer_is_grounded(_c(), [README])
    assert ok and why == ""


def test_an_answer_citing_a_document_the_run_never_read_is_withheld() -> None:
    ok, why = answer_is_grounded(_c(cited_documents=["docs/invented.md"]), [README])
    assert not ok and "never read" in why


def test_an_answer_citing_an_unreadable_document_is_withheld() -> None:
    """A configured path that was absent on disk is not evidence."""
    ok, why = answer_is_grounded(_c(cited_documents=["docs/absent.md"]), [README, MISSING])
    assert not ok and "never read" in why


def test_an_answer_citing_nothing_is_withheld() -> None:
    ok, why = answer_is_grounded(_c(cited_documents=[]), [README])
    assert not ok and "cites no documentation" in why


def test_an_empty_answer_is_withheld() -> None:
    ok, why = answer_is_grounded(_c(answer="   "), [README])
    assert not ok and "no answer text" in why


@pytest.mark.parametrize("kind", ["complaint", "feature_request", "bug_report", "off_topic"])
def test_an_answer_is_never_posted_for_an_unanswerable_kind(kind: str) -> None:
    ok, why = answer_is_grounded(_c(classification=kind), [README])
    assert not ok and "not permitted" in why


def test_prose_naming_an_unread_file_is_withheld_even_when_the_metadata_is_honest() -> None:
    """The declared list can be correct while the prose attributes a claim to a
    file that does not exist. A reader believes the prose."""
    ok, why = answer_is_grounded(
        _c(answer="Based on `README.md`: see also `config/secrets.yml` for the key."),
        [README],
    )
    assert not ok and "names files this run never read" in why


def test_backticked_code_is_not_mistaken_for_a_citation() -> None:
    """Spec 171 threw away good pages by counting `/charge`, `@app.route` and
    `charge()` as missing citations. Only file-shaped tokens count."""
    ok, why = answer_is_grounded(
        _c(answer="Based on `README.md`: call `make start`, hit `/health`, see `boot()`."),
        [README],
    )
    assert ok, why


def test_path_like_tokens_picks_only_files() -> None:
    tokens = path_like_tokens("`README.md` `/charge` `boot()` `src/app.py` `--flag`")
    assert tokens == ["README.md", "src/app.py"]


def test_unknown_citations_names_every_offender() -> None:
    got = unknown_citations(_c(cited_documents=["a.md", "README.md", "b.md"]), [README])
    assert sorted(got) == ["a.md", "b.md"]


def test_claimed_paths_strips_backticks_and_duplicates() -> None:
    assert claimed_paths(_c(cited_documents=["`README.md`", "README.md", " "])) == ["README.md"]


def test_undeclared_citations_ignores_documents_that_were_read() -> None:
    assert undeclared_citations("see `README.md`", [README]) == []


# ---------------------------------------------------------------- unsent issues


def test_a_judgement_about_an_unsent_issue_is_discarded() -> None:
    assert not accept_classification(_c(issue_id="I_99"), {"I_1"})


def test_a_judgement_about_a_sent_issue_is_kept() -> None:
    assert accept_classification(_c(issue_id="I_1"), {"I_1"})


# ---------------------------------------------------------------- confidence


def test_a_low_confidence_answer_does_not_clear_the_threshold() -> None:
    assert not confident_enough(_c(confidence=0.5), 0.7)


def test_confidence_at_the_threshold_clears_it() -> None:
    assert confident_enough(_c(confidence=0.7), 0.7)


@pytest.mark.parametrize("kind", ["complaint", "feature_request", "bug_report", "off_topic"])
def test_the_threshold_never_applies_to_an_unanswerable_kind(kind: str) -> None:
    """Preserved behaviour: a complaint escalates whatever the confidence, and
    a bug report is triaged whatever the confidence."""
    assert confident_enough(_c(classification=kind, confidence=0.01), 0.7)


def test_confidence_out_of_range_is_clamped_not_rejected() -> None:
    assert _c(confidence=1.4).confidence == 1.0
    assert _c(confidence=-2).confidence == 0.0


# ---------------------------------------------------------------- settings


def test_settings_fall_back_to_the_shipped_defaults() -> None:
    from performer.workflows.advocate.settings import AdvocateSettings

    s = AdvocateSettings.from_env({})
    assert s.handled_label == "advocate-handled" and s.confidence_threshold == 0.70
    assert "billing" in s.sensitive_keywords and s.doc_sources == ["README.md"]


def test_a_non_numeric_threshold_falls_back_rather_than_crashing() -> None:
    from performer.workflows.advocate.settings import AdvocateSettings

    assert AdvocateSettings.from_env({"ADVOCATE_CONFIDENCE_THRESHOLD": "high"}).confidence_threshold == 0.70


def test_a_json_object_where_a_list_belongs_falls_back() -> None:
    from performer.workflows.advocate.settings import AdvocateSettings

    assert AdvocateSettings.from_env({"ADVOCATE_DOC_SOURCES": '{"a": 1}'}).doc_sources == ["README.md"]


# ---------------------------------------------------------------- documents


def test_a_document_path_escaping_the_repository_is_refused(tmp_path) -> None:
    """A doc_sources entry of ../../etc/passwd would end up quoted into a
    public issue comment."""
    from performer.workflows.advocate.docs import read_documents

    (tmp_path / "inside.md").write_text("ok")
    docs = read_documents(tmp_path, ["inside.md", "../outside.md"])
    assert [d.path for d in docs if d.read] == ["inside.md"]


def test_a_very_large_document_is_truncated_not_dropped(tmp_path) -> None:
    from performer.workflows.advocate.docs import read_documents

    (tmp_path / "big.md").write_text("x" * 60_000)
    doc = read_documents(tmp_path, ["big.md"])[0]
    assert doc.read and doc.content.endswith("[truncated]") and len(doc.content) < 60_000


def test_the_rendered_documentation_contains_only_documents_that_were_read(tmp_path) -> None:
    from performer.workflows.advocate.docs import read_documents, render

    (tmp_path / "a.md").write_text("alpha")
    rendered = render(read_documents(tmp_path, ["a.md", "missing.md"]))
    assert "alpha" in rendered and "missing.md" not in rendered


# ---------------------------------------------------------------- batching


def test_issues_are_split_into_bounded_calls() -> None:
    from performer.workflows.advocate.classify import batches
    from performer.workflows.advocate.models import IssueCandidate

    issues = [IssueCandidate(issue_id=f"I_{i}") for i in range(7)]
    assert [len(b) for b in batches(issues, 3)] == [3, 3, 1]
    assert [len(b) for b in batches(issues, 0)] == [1] * 7, "a zero size must not divide by zero"
