"""Spec 169 FR-006 to FR-009, FR-017: every gate rule is a pure function with its
own test. Each test names the mutation that breaks it (data-model "Mutation test
rules"); the mutation table in the PR records each one applied in the real tree."""
from __future__ import annotations

from performer.workflows.reviewer.diffparse import diff_lines
from performer.workflows.reviewer.gate import (
    add_documentation_by_implementer,
    add_unaddressed_feedback,
    anchor_in_hunks,
    anchor_in_surveyed,
    anchor_ok,
    cap_findings,
    evidence_matches,
    full_coverage,
    has_disposition,
    is_documentation_path,
    run_gate,
    verdict,
)
from performer.workflows.reviewer.models import ChangedFile, Disposition, Hunk

from tests.unit.workflows.reviewer._fixtures import DIFF, changed_files, finding, prior

LINES = diff_lines(DIFF)


# anchor_in_hunks: mutation = drop the changed_files path check (any path with a line in some hunk passes)
def test_anchor_in_hunks_requires_the_changed_path_and_a_line_inside_a_hunk():
    assert anchor_in_hunks(finding(), changed_files())
    assert not anchor_in_hunks(finding(path="src/other.py"), changed_files()), "an unchanged path never anchors"
    assert not anchor_in_hunks(finding(line=40), changed_files()), "a line outside every hunk of that file never anchors"
    assert not anchor_in_hunks(finding(path="tests/test_calc.py", line=2), changed_files()), "hunks are per file, not pooled"


# anchor_in_surveyed: mutation = always return True
def test_anchor_in_surveyed_is_membership_in_the_opened_set():
    assert anchor_in_surveyed(finding(), ["src/calc.py"])
    assert not anchor_in_surveyed(finding(), [])
    assert not anchor_in_surveyed(finding(), ["tests/test_calc.py"])


# evidence_matches: mutation = always return True
def test_evidence_must_be_a_substring_of_a_diff_or_survey_line():
    assert evidence_matches(finding(), LINES, [])
    assert evidence_matches(finding(evidence="a  /  b"), ["return  a / b"], []) is True, "whitespace folds on both sides"
    assert evidence_matches(finding(evidence="return a/b"), LINES, []) is False
    assert evidence_matches(finding(evidence="opened line"), [], ["  opened line here"]) is True
    assert evidence_matches(finding(evidence="nowhere"), LINES, ["x"]) is False
    assert evidence_matches(finding(evidence="", origin="rule"), [], []) is True
    assert evidence_matches(finding(evidence="x", origin="rule"), ["x"], []) is False, "rule findings carry no evidence"


# anchor_ok: mutation = drop the evidence clause, or accept a surveyed file that is not a changed file
def test_anchor_ok_combines_path_hunk_or_survey_and_evidence():
    files = changed_files()
    assert anchor_ok(finding(), files, [], LINES, [])
    assert not anchor_ok(finding(line=40), files, [], LINES, []), "outside the hunks and not surveyed"
    assert anchor_ok(finding(line=40, evidence="deep line"), files, ["src/calc.py"], LINES, ["deep line"]), "surveyed file: any line, evidence from the survey"
    assert not anchor_ok(finding(line=40, evidence="deep line"), files, ["src/calc.py"], LINES, []), "surveyed but the evidence matches nothing"
    assert not anchor_ok(finding(path="src/other.py", evidence="return a / b"), files, ["src/other.py"], LINES, ["return a / b"]), "a surveyed file that is not a changed file never anchors"
    assert not anchor_ok(finding(evidence="made up"), files, [], LINES, []), "hallucinated evidence drops the finding"


# has_disposition: mutation = always return True
def test_has_disposition_matches_the_comment_id():
    ds = [Disposition(prior_comment_id="c1", status="fixed")]
    assert has_disposition("c1", ds)
    assert not has_disposition("c2", ds)
    assert not has_disposition("c1", [])


# add_unaddressed_feedback: mutation = skip the loop (never add)
def test_every_prior_comment_without_a_disposition_becomes_a_rule_finding():
    out = add_unaddressed_feedback([], [prior(), prior(id="c2", path="", line=0, body="Please add a changelog note")], [Disposition(prior_comment_id="c1", status="fixed")])
    assert [f.category for f in out] == ["unaddressed_feedback"]
    f = out[0]
    assert (f.path, f.line, f.origin, f.evidence) == ("", 0, "rule", ""), "a comment without a file anchor maps to the PR body"
    assert "c2" in f.problem and "changelog" in f.problem
    anchored = add_unaddressed_feedback([], [prior()], [])
    assert (anchored[0].path, anchored[0].line) == ("src/calc.py", 6)


# is_documentation_path / add_documentation_by_implementer: mutation = drop a prefix or ignore brief_present
def test_documentation_tree_is_docs_doc_and_the_named_basenames():
    for p in ("docs/guide.md", "doc/x.rst", "README.md", "README", "sub/CONTRIBUTING.md", "CHANGELOG.rst"):
        assert is_documentation_path(p), p
    for p in ("src/docs_helper.py", "documents/x.md", "readme_parser.py", "src/README_gen/x.py"):
        assert not is_documentation_path(p), p


def test_documentation_finding_needs_the_brief_and_a_documentation_path():
    docs = ChangedFile(path="docs/usage.md", hunks=[Hunk(header="@@ -1 +1,2 @@", start_line=1, end_line=2, lines=[])], fully_in_diff=True)
    files = [*changed_files(), docs]
    assert add_documentation_by_implementer([], files, brief_present=False) == []
    assert add_documentation_by_implementer([], changed_files(), brief_present=True) == []
    out = add_documentation_by_implementer([], files, brief_present=True)
    assert len(out) == 1 and out[0].category == "documentation_by_implementer"
    assert (out[0].path, out[0].line, out[0].origin) == ("docs/usage.md", 1, "rule")
    assert "docs/usage.md" in out[0].problem


# full_coverage: mutation = always return True, or ignore the truncation clause
def test_full_coverage_needs_every_file_read_and_the_pass_after_truncation():
    files = changed_files()
    assert full_coverage(files, truncated=False, coverage_pass_ran=False)
    unread = [*files, ChangedFile(path="src/extra.py", hunks=[], fully_in_diff=False)]
    assert not full_coverage(unread, truncated=False, coverage_pass_ran=True)
    opened = [*files, ChangedFile(path="src/extra.py", hunks=[], fully_in_diff=False, opened_by_survey=True)]
    assert full_coverage(opened, truncated=False, coverage_pass_ran=False)
    assert not full_coverage(files, truncated=True, coverage_pass_ran=False), "a truncated diff needs the coverage pass"
    assert full_coverage(files, truncated=True, coverage_pass_ran=True)


# verdict: mutation = approve with findings, or approve without coverage
def test_verdict_is_derived_by_code():
    assert verdict([finding()], coverage_ok=True) == "changes_requested"
    assert verdict([finding()], coverage_ok=False) == "changes_requested"
    assert verdict([], coverage_ok=True) == "approved"
    assert verdict([], coverage_ok=False) == "env_blocked"


# cap_findings: mutation = drop the slice
def test_findings_are_capped_at_thirty():
    many = [finding(line=1) for _ in range(45)]
    assert len(cap_findings(many)) == 30
    assert len(cap_findings(many[:3])) == 3


def test_run_gate_end_to_end_orders_the_rules():
    files = changed_files()
    files[0] = files[0].model_copy(update={"opened_by_survey": True})
    model = [finding(), finding(evidence="hallucinated"), finding(line=30, evidence="deep")]
    result = run_gate(model, [Disposition(prior_comment_id="c1", status="fixed"), Disposition(prior_comment_id="zzz", status="fixed")],
                      changed_files=files, prior_comments=[prior(), prior(id="c2", path="", line=0)], diff_lines=LINES, survey_lines=["deep"],
                      brief_present=True, truncated=False, coverage_pass_ran=False)
    assert [f.evidence for f in result.dropped] == ["hallucinated"]
    cats = [f.category for f in result.findings]
    assert cats == ["logic_error", "logic_error", "unaddressed_feedback"], cats
    assert result.verdict == "changes_requested"
    assert [d.prior_comment_id for d in result.dispositions] == ["c1"], "a disposition for an unknown comment is discarded"
    assert result.fixed_ids == ["c1"]
    clean = run_gate([], [], changed_files=changed_files(), prior_comments=[], diff_lines=LINES, survey_lines=[], brief_present=False, truncated=False, coverage_pass_ran=False)
    assert clean.verdict == "approved" and clean.findings == [] and clean.covered_files == ["src/calc.py", "tests/test_calc.py"]
    hold = run_gate([], [], changed_files=[*changed_files(), ChangedFile(path="src/extra.py", hunks=[], fully_in_diff=False)], prior_comments=[], diff_lines=LINES, survey_lines=[], brief_present=False, truncated=True, coverage_pass_ran=True)
    assert hold.verdict == "env_blocked" and hold.unread_files == ["src/extra.py"]
