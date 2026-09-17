"""Spec 169 FR-002: intake parses the diff, records truncation, normalises the
relayed comments and carries the brief. No model call, no command."""
from __future__ import annotations

from types import SimpleNamespace

from performer.workflows.reviewer.intake import build_intake, normalise_prior_comments

from tests.unit.workflows.reviewer._fixtures import DIFF, TRUNCATED_DIFF


def _score(**over):
    base = dict(pr_diff=DIFF, relay_feedback=[], implementation_brief={}, title="Add div", description="Divide numbers", pr_url="https://github.com/o/r/pull/12")
    base.update(over)
    return SimpleNamespace(**base)


def test_intake_parses_files_hunks_and_new_side_ranges():
    intake = build_intake(_score())
    assert intake.changed_paths == ["src/calc.py", "tests/test_calc.py"]
    calc = intake.changed_files[0]
    assert (calc.hunks[0].start_line, calc.hunks[0].end_line) == (1, 8)
    assert calc.fully_in_diff and not calc.opened_by_survey
    assert intake.diff_truncated is False and intake.brief_present is False
    assert any("return a / b" in line for line in intake.diff_line_list())


def test_a_truncated_diff_marks_the_cut_file_unread():
    intake = build_intake(_score(pr_diff=TRUNCATED_DIFF))
    assert intake.diff_truncated is True
    # 412 round 12: the bare note's unaccounted tail rides along as an
    # unopenable phantom so the gates hold on the hidden remainder.
    assert intake.changed_paths[-1] == "<unnamed files beyond the truncated diff>"
    assert intake.changed_paths[-2] == "src/extra.py"
    assert intake.changed_files[-1].fully_in_diff is False
    assert all(f.fully_in_diff for f in intake.changed_files[:-2])
    assert "truncated" in intake.as_text()


def test_prior_comments_normalise_to_id_path_line_body():
    raw = [
        {"id": 77, "path": "src/calc.py", "line": 6, "body": "Guard b == 0", "author_login": "jason"},
        {"body": "Please add a changelog note", "author_login": "coordinare"},
        "a bare string comment",
        {"body": "   "},
        {"path": "x.py", "line": "not a number", "body": "odd line"},
        42,
    ]
    out = normalise_prior_comments(raw)
    assert [(c.id, c.path, c.line) for c in out] == [("77", "src/calc.py", 6), ("c2", "", 0), ("c3", "", 0), ("c5", "x.py", 0)]
    assert normalise_prior_comments(None) == [] and normalise_prior_comments("nope") == []


def test_the_brief_is_carried_only_when_it_is_a_non_empty_dict():
    assert build_intake(_score(implementation_brief={"work_kind": "feature", "milestones": []})).brief_present
    assert not build_intake(_score(implementation_brief=None)).brief_present
    assert not build_intake(_score(implementation_brief="text")).brief_present
    assert build_intake(_score(pr_diff="")).changed_files == []
