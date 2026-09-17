"""Spec 173 US1: the advocate workflow end to end against a fake GitHub."""
from __future__ import annotations

from pathlib import Path

import pytest
from performer.workflows.advocate import AdvocateWorkflow

from tests.unit.workflows.advocate._fakes import (
    FakeGitHub,
    FakeToolkit,
    batch,
    classification,
    issue,
    score,
    stand,
)

README = {"README.md": "Run `make start` to boot the app."}


async def _run(gh: FakeGitHub, tk: FakeToolkit, tmp_path: Path, docs=None, **env):
    workflow = AdvocateWorkflow(lister=gh.lister, poster=gh)
    result = await workflow.run(stand(tmp_path, docs if docs is not None else README),
                                score(workflow_env=env), tk)
    return result.report["advocate"]


@pytest.mark.asyncio
async def test_an_answerable_question_is_answered_and_labelled(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1)])
    tk = FakeToolkit([batch(classification())])
    rec = await _run(gh, tk, tmp_path)
    assert rec["verdict"] == "advocate_complete"
    assert [o["action"] for o in rec["outcomes"]] == ["replied"]
    assert gh.labels_for("I_1") == ["advocate-handled"]
    assert gh.comments and "make start" in gh.comments[0][1]
    assert "generated automatically" in gh.comments[0][1], "the disclosure must ride along"


@pytest.mark.asyncio
async def test_an_answer_citing_an_unread_document_is_withheld_and_escalated(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1)])
    tk = FakeToolkit([batch(classification(
        answer="Based on `docs/invented.md`: do this.", cited_documents=["docs/invented.md"]))])
    rec = await _run(gh, tk, tmp_path)
    assert [o["action"] for o in rec["outcomes"]] == ["escalated"]
    assert rec["outcomes"][0]["escalation_reason"] == "no_documentation_match"
    assert rec["withheld"] and "never read" in rec["withheld"][0]["reason"]
    assert gh.labels_for("I_1") == ["needs-human"]
    assert "do this" not in gh.comments[0][1], "the ungrounded answer must not reach the issue"


@pytest.mark.asyncio
async def test_a_sensitive_keyword_escalates_with_no_model_call(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1, title="Billing dispute", body="I want a refund")])
    tk = FakeToolkit([batch(classification())])
    rec = await _run(gh, tk, tmp_path)
    assert tk.metrics.model_calls == 0, "a legal complaint must not depend on the gateway"
    assert rec["outcomes"][0]["escalation_reason"] == "sensitive_keyword"
    assert gh.labels_for("I_1") == ["needs-human"]


@pytest.mark.asyncio
async def test_an_already_handled_issue_is_skipped(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1, labels=["advocate-handled"])])
    tk = FakeToolkit()
    rec = await _run(gh, tk, tmp_path)
    assert rec["issues_seen"] == 0 and rec["outcomes"] == []
    assert tk.metrics.model_calls == 0 and gh.comments == []


@pytest.mark.asyncio
async def test_an_escalated_issue_is_not_answered_over(tmp_path: Path) -> None:
    """A human owns an escalated issue; replying again talks over them."""
    gh = FakeGitHub([issue(1, labels=["needs-human"])])
    rec = await _run(gh, FakeToolkit(), tmp_path)
    assert rec["issues_seen"] == 0 and gh.comments == []


@pytest.mark.asyncio
async def test_a_repository_with_nothing_to_do_costs_no_model_call(tmp_path: Path) -> None:
    """SC-010: an idle project is free to poll."""
    gh = FakeGitHub([])
    tk = FakeToolkit()
    rec = await _run(gh, tk, tmp_path)
    assert rec["issues_seen"] == 0 and tk.metrics.model_calls == 0
    assert rec["documents_read"] == [], "no issues means the documents are not even read"


@pytest.mark.asyncio
async def test_unreadable_documentation_escalates_rather_than_answering(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1)])
    tk = FakeToolkit([batch(classification())])
    rec = await _run(gh, tk, tmp_path, docs={})
    assert rec["outcomes"][0]["escalation_reason"] == "no_documentation_configured"
    assert tk.metrics.model_calls == 0, "there is nothing to answer from, so nothing to ask"


@pytest.mark.asyncio
async def test_a_judgement_about_an_unsent_issue_is_ignored(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1)])
    tk = FakeToolkit([batch(classification(issue_id="I_99"))])
    rec = await _run(gh, tk, tmp_path)
    assert rec["outcomes"][0]["escalation_reason"] == "classification_unavailable"
    assert gh.labels_for("I_99") == []


@pytest.mark.asyncio
async def test_an_issue_the_model_did_not_judge_escalates(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1), issue(2)])
    tk = FakeToolkit([batch(classification("I_1"))])
    rec = await _run(gh, tk, tmp_path)
    actions = {o["issue_id"]: o["action"] for o in rec["outcomes"]}
    assert actions["I_1"] == "replied" and actions["I_2"] == "escalated"


@pytest.mark.asyncio
async def test_a_low_confidence_answer_escalates(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1)])
    tk = FakeToolkit([batch(classification(confidence=0.2))])
    rec = await _run(gh, tk, tmp_path)
    assert rec["outcomes"][0]["escalation_reason"] == "low_confidence"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "action", "label", "commented"),
    [
        ("complaint", "escalated", "needs-human", True),
        ("feature_request", "acknowledged", "advocate-handled", True),
        ("bug_report", "triaged", "advocate-handled", False),
        ("off_topic", "redirected", "advocate-handled", True),
    ],
)
async def test_every_preserved_branch_still_behaves_as_before(
    tmp_path: Path, kind: str, action: str, label: str, commented: bool,
) -> None:
    gh = FakeGitHub([issue(1)])
    tk = FakeToolkit([batch(classification(classification=kind, answer=None, cited_documents=[]))])
    rec = await _run(gh, tk, tmp_path)
    assert rec["outcomes"][0]["action"] == action
    assert gh.labels_for("I_1") == [label]
    assert bool(gh.comments) is commented


@pytest.mark.asyncio
async def test_a_complaint_escalates_however_confident(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1)])
    tk = FakeToolkit([batch(classification(classification="complaint", confidence=0.99, answer=None,
                                           cited_documents=[]))])
    rec = await _run(gh, tk, tmp_path)
    assert rec["outcomes"][0]["escalation_reason"] == "complaint"


@pytest.mark.asyncio
async def test_the_label_is_applied_before_the_comment(tmp_path: Path) -> None:
    """If the comment lands and the label does not, the next run answers again."""
    order: list[str] = []
    gh = FakeGitHub([issue(1)])
    orig_label, orig_comment = gh.label, gh.comment

    async def label(iid, lab):
        order.append("label")
        await orig_label(iid, lab)

    async def comment(num, body):
        order.append("comment")
        await orig_comment(num, body)

    gh.label, gh.comment = label, comment  # type: ignore[method-assign]
    await _run(gh, FakeToolkit([batch(classification())]), tmp_path)
    assert order == ["label", "comment"]


@pytest.mark.asyncio
async def test_a_failed_label_does_not_abandon_the_rest_of_the_run(tmp_path: Path) -> None:
    gh = FakeGitHub([issue(1), issue(2)], fail_label_on={"I_1"})
    tk = FakeToolkit([batch(classification("I_1"), classification("I_2"))])
    rec = await _run(gh, tk, tmp_path)
    assert len(rec["outcomes"]) == 2
    assert rec["outcomes"][0]["label_applied"] == "", "the failure is recorded, not raised"
    assert gh.labels_for("I_2") == ["advocate-handled"]


@pytest.mark.asyncio
async def test_a_listing_failure_holds_the_run(tmp_path: Path) -> None:
    gh = FakeGitHub([], fail_list=True)
    rec = await _run(gh, FakeToolkit(), tmp_path)
    assert rec["verdict"] == "env_blocked" and "could not list" in (rec["error"] or "")


@pytest.mark.asyncio
async def test_the_run_writes_nothing(tmp_path: Path) -> None:
    """SC-003: no commit, no push, no pull request. The proof is executed."""
    gh = FakeGitHub([issue(1)])
    tk = FakeToolkit([batch(classification())])
    rec = await _run(gh, tk, tmp_path)
    assert "git status --porcelain" in tk.commands
    assert rec["write_free_check"] == ""


@pytest.mark.asyncio
async def test_the_persona_never_travels_inside_the_documentation(tmp_path: Path) -> None:
    """FR-023, and the precondition for the whole gate: instruction and evidence
    must be separable, or a citation cannot be checked against anything."""
    gh = FakeGitHub([issue(1)])
    tk = FakeToolkit([batch(classification())])
    await _run(gh, tk, tmp_path)
    persona, content = tk.calls[0]
    # content is the toolkit's block list, as the real Toolkit receives it.
    text = "".join(block.get("text", "") for block in content)
    docs_section = text.split("Classify each of the following")[0]
    assert "You triage inbound GitHub issues" in persona
    assert "You triage inbound GitHub issues" not in docs_section
    assert "make start" in docs_section, "the documentation itself is there"
