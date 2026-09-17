"""Fixture pull requests for the reviewer workflow eval (spec 169 SC-001).

Each fixture carries the diff and prior comments as the reviewer receives
them, the canned model replies in call order (survey proposal, optional
coverage proposal, findings, optional re-anchor), and the expectations
scoring checks. Deterministic by construction; the live mode swaps the stub
for the gateway and keeps the expectations loose where a real model varies.
"""
from __future__ import annotations

from dataclasses import dataclass, field

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

TRUNCATED_DIFF = DIFF + (
    "\ndiff --git a/src/extra.py b/src/extra.py\n--- a/src/extra.py\n+++ b/src/extra.py\n@@ -1,2 +1,3 @@\n import os\n+X = 1\n\n"
    "[coordinare: diff truncated to 60000 chars; run `gh pr diff <pr_url>` for the full changes]\n"
)

SURVEY = {"commands": [{"command": "git log --oneline -3", "reason": "recent history"}]}
OPEN_EXTRA = {"commands": [{"command": "sed -n '1,40p' src/extra.py", "reason": "read the cut file"}]}
DIV_FINDING = {"path": "src/calc.py", "line": 6, "category": "logic_error", "problem": "division by zero is unguarded",
               "why_blocking": "div(1, 0) raises ZeroDivisionError", "evidence": "return a / b"}
NO_FINDINGS = {"findings": [], "dispositions": []}


@dataclass(frozen=True)
class Expectation:
    verdict: str
    min_findings: int
    max_findings: int
    review_event: str | None  # REQUEST_CHANGES, COMMENT, or None when nothing is posted
    coverage_pass: bool
    categories: tuple[str, ...] = ()  # every one of these must appear among the findings
    min_dropped: int = 0
    live_verdicts: tuple[str, ...] = ()  # accepted verdicts in live mode (a real model varies)


@dataclass(frozen=True)
class Fixture:
    name: str
    diff: str
    relay_feedback: list[dict]
    brief: dict
    replies: list[dict]  # stub model replies in call order
    expect: Expectation
    survey_output: dict = field(default_factory=dict)  # command -> output for the fake runner


CLEAN = Fixture(
    name="clean", diff=DIFF, relay_feedback=[], brief={}, replies=[SURVEY, NO_FINDINGS],
    expect=Expectation(verdict="approved", min_findings=0, max_findings=0, review_event="COMMENT", coverage_pass=False,
                       live_verdicts=("approved", "changes_requested")),
)

FINDINGS = Fixture(
    name="findings", diff=DIFF, relay_feedback=[], brief={}, replies=[SURVEY, {"findings": [DIV_FINDING], "dispositions": []}],
    expect=Expectation(verdict="changes_requested", min_findings=1, max_findings=30, review_event="REQUEST_CHANGES", coverage_pass=False,
                       live_verdicts=("changes_requested",)),
)

HALLUCINATED_ANCHOR = Fixture(
    name="hallucinated_anchor", diff=DIFF, relay_feedback=[], brief={},
    replies=[SURVEY, {"findings": [{**DIV_FINDING, "line": 99, "evidence": "if b == 0: raise"}], "dispositions": []}, {"findings": [DIV_FINDING], "dispositions": []}],
    expect=Expectation(verdict="changes_requested", min_findings=1, max_findings=1, review_event="REQUEST_CHANGES", coverage_pass=False, min_dropped=1,
                       live_verdicts=("approved", "changes_requested")),
)

PRIOR_FEEDBACK = Fixture(
    name="prior_feedback", diff=DIFF, brief={},
    relay_feedback=[{"id": 501, "path": "src/calc.py", "line": 6, "body": "Guard against b == 0", "author_login": "jason"},
                    {"body": "Add a changelog entry for div", "author_login": "jason"}],
    replies=[SURVEY, {"findings": [], "dispositions": [{"prior_comment_id": "501", "status": "fixed"}]}],
    expect=Expectation(verdict="changes_requested", min_findings=1, max_findings=30, review_event="REQUEST_CHANGES", coverage_pass=False,
                       categories=("unaddressed_feedback",), live_verdicts=("changes_requested",)),
)

TRUNCATED = Fixture(
    name="truncated", diff=TRUNCATED_DIFF, relay_feedback=[], brief={}, replies=[SURVEY, OPEN_EXTRA, NO_FINDINGS],
    survey_output={"sed -n '1,40p' src/extra.py": "import os\nX = 1\n"},
    # 412 round 12: the bare truncation note accounts for nothing, so the
    # unopenable phantom holds the review even though the coverage pass
    # opened every visible file.
    expect=Expectation(verdict="env_blocked", min_findings=0, max_findings=0, review_event=None, coverage_pass=True,
                       live_verdicts=("env_blocked",)),
)

DOCS_BY_IMPLEMENTER = Fixture(
    name="docs_by_implementer",
    diff=DIFF + "diff --git a/docs/usage.md b/docs/usage.md\n--- a/docs/usage.md\n+++ b/docs/usage.md\n@@ -1,2 +1,3 @@\n # Usage\n+Call div.\n",
    relay_feedback=[], brief={"work_kind": "feature", "milestones": [{"goal": "add div"}]}, replies=[SURVEY, NO_FINDINGS],
    expect=Expectation(verdict="changes_requested", min_findings=1, max_findings=30, review_event="REQUEST_CHANGES", coverage_pass=False,
                       categories=("documentation_by_implementer",), live_verdicts=("changes_requested",)),
)

FIXTURES: list[Fixture] = [CLEAN, FINDINGS, HALLUCINATED_ANCHOR, PRIOR_FEEDBACK, TRUNCATED, DOCS_BY_IMPLEMENTER]
