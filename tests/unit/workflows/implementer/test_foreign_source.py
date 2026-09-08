"""#279 narrow ownership rule. Mutation: return False disables enforcement."""
from __future__ import annotations

import pytest
from performer.workflows.implementer.cycle import foreign_source_path, scope_violations


@pytest.mark.parametrize("path,own,other,expected", [
    ("src/m1.py", ["src/m0.py"], ["src/m1.py"], True),
    ("src/m1/child.py", ["src/m0.py"], ["src/m1/"], True),
    ("src/m10.py", [], ["src/m1"], False),
    ("src/shared.py", ["src/"], ["src/shared.py"], False),
    ("src/helper.py", ["src/m0.py"], ["src/m1.py"], False),
    ("tests/test_m1.py", [], ["tests/"], False),
    ("src/m1.py", [], ["."], False),
    ("src/m1.py", [], ["src/m1.py"], False),
    ("src/m1.py", ["."], ["src/m1.py"], False),
    ("src/m1.py", ["./"], ["src/m1.py"], False),
    ("src/m1.py", ["src/m0.py"], ["./src/m1.py"], True),
])
def test_foreign_source_requires_exclusive_declared_ownership(path, own, other, expected):
    assert foreign_source_path(path, own, other) is expected


@pytest.mark.parametrize("change", ["added", "modified", "deleted"])
def test_foreign_source_edits_are_reverted_for_implementation(change):
    result = scope_violations("implement", {"src/m1.py": change}, "pytest", ["src/m0.py"], foreign_scope_paths=["src/m1.py"])
    assert [(r["path"], r["kind"]) for r in result] == [("src/m1.py", "reverted_foreign_source")]
