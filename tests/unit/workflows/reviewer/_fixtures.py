"""Shared fixtures for the reviewer workflow tests (spec 169)."""
from __future__ import annotations

from performer.workflows.reviewer.models import ChangedFile, Finding, Hunk, PriorComment

DIFF = """diff --git a/src/calc.py b/src/calc.py
index 1111111..2222222 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,4 +1,8 @@
 def add(a, b):
     return a + b
+
+
+def div(a, b):
+    return a / b
+
+
diff --git a/tests/test_calc.py b/tests/test_calc.py
index 3333333..4444444 100644
--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -5,3 +5,7 @@ def test_add():
     assert add(1, 2) == 3
+
+
+def test_div():
+    assert div(4, 2) == 2
"""

TRUNCATED_DIFF = DIFF + "\ndiff --git a/src/extra.py b/src/extra.py\n--- a/src/extra.py\n+++ b/src/extra.py\n@@ -1,2 +1,3 @@\n import os\n+X = 1\n\n[coordinare: diff truncated to 60000 chars; run `gh pr diff` for the full changes]\n"


def changed_files():
    return [
        ChangedFile(path="src/calc.py", hunks=[Hunk(header="@@ -1,4 +1,8 @@", start_line=1, end_line=8, lines=[])], fully_in_diff=True),
        ChangedFile(path="tests/test_calc.py", hunks=[Hunk(header="@@ -5,3 +5,7 @@", start_line=5, end_line=11, lines=[])], fully_in_diff=True),
    ]


def finding(**over) -> Finding:
    base = dict(path="src/calc.py", line=6, category="logic_error", problem="division by zero is unguarded",
                why_blocking="div(1, 0) raises", evidence="return a / b", origin="model")
    base.update(over)
    return Finding(**base)


def prior(**over) -> PriorComment:
    base = dict(id="c1", path="src/calc.py", line=6, body="Guard against b == 0")
    base.update(over)
    return PriorComment(**base)
