"""#281: unchanged callers need both a verified survey and a changed cause."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from performer.workflows.reviewer.gate import anchor_ok
from performer.workflows.reviewer.survey import opened_paths

from tests.unit.workflows.reviewer._fixtures import changed_files, finding


@pytest.mark.parametrize("cause,survey,evidence,accepted", [
    ("src/calc.py", ["src/caller.py"], "consume(value)", True),
    ("", ["src/caller.py"], "consume(value)", False),
    ("src/other.py", ["src/caller.py"], "consume(value)", False),
    ("src/calc.py", [], "consume(value)", False),
    ("src/calc.py", ["src/caller.py"], "invented()", False),
])
def test_unchanged_anchor_needs_changed_cause_survey_and_evidence(cause, survey, evidence, accepted):
    f = finding(path="src/caller.py", introduced_by=cause, evidence=evidence)
    assert anchor_ok(f, changed_files(), survey, [], ["consume(value)"]) is accepted


@pytest.mark.parametrize("command,allowed,exit_code,expected", [
    ("cat caller.py", True, 0, {"caller.py"}),
    ("git show HEAD:caller.py", True, 0, {"caller.py"}),
    ("git --no-pager show HEAD:caller.py", True, 0, {"caller.py"}),
    ("git --no-color show HEAD:caller.py", True, 0, {"caller.py"}),
    ("git -c core.quotepath=false show HEAD:caller.py", True, 0, {"caller.py"}),
    ("git -c show HEAD:caller.py", True, 0, set()),
    ("git show --no-color HEAD:caller.py", True, 0, {"caller.py"}),
    ("git show --no-pager HEAD:caller.py", True, 0, {"caller.py"}),
    ("git show --format=HEAD:caller.py", True, 0, set()),
    ("git show HEAD -- caller.py", True, 0, set()),
    ("cat tests/caller.py", True, 0, set()),
    ("cat caller.py", False, 0, set()),
    ("cat caller.py", True, 1, set()),
    ("ls caller.py", True, 0, set()),
    ("git status -- caller.py", True, 0, set()),
    ("ls caller.py | cat", True, 0, set()),
    ("cat other.py;ls caller.py", True, 0, set()),
])
def test_opened_paths_require_successful_admitted_exact_path(command, allowed, exit_code, expected):
    record = SimpleNamespace(command=command, allowed=allowed, exit_code=exit_code)
    assert opened_paths([record], ["caller.py"]) == expected
