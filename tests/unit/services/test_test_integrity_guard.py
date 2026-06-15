"""090 US3 (L3) — adversarial corpus for the static test-integrity guard.

The static half of spec 090's dual guard (FR-019, FR-020, SC-004) is a pure
function over a unified diff:

    analyze_diff(diff: str) -> tuple[bool, list[str]]   # (is_safe, flagged_patterns)

It must REJECT (``is_safe=False``) every diff that weakens a test, and clear
(``is_safe=True``) only a change it can confidently prove does not.  Its stated
bias is conservative: a test-file change it cannot confidently clear is treated
as a weakening (FR-019 — "cannot confidently clear ⇒ weakening").

This file is the adversarial **test-weakening corpus** SC-004 mandates.  Each
case is a self-contained unified diff; the corpus covers, at minimum:

  (a) a removed assertion,
  (b) an added ``@pytest.mark.skip`` / ``xfail`` / disable marker,
  (c) a loosened comparison / operator,
  (d) an assertion wrapped in a swallowing ``try/except`` or made conditional,
  (e) a requirement mocked away,
  (f) a removed ``setUp`` / ``tearDown`` / fixture,

plus the two boundary cases that define the guard's contract:

  - a custom / unrecognizable test-assertion framework the guard cannot
    confidently clear → ``is_safe=False`` (uncertainty rejects), and
  - a clean code-only fix (no test file touched) → ``is_safe=True``.

The guard's scope is **test integrity**: it governs whether a repair may land
as a candidate, and only ever reasons about *test* files — a production-code
fix that does not touch a test is always clearable here (the do-not-weaken
mandate is about tests; production ``assert`` statements are out of scope).
"""
from __future__ import annotations

import pytest

from coordinare.services.test_integrity_guard import analyze_diff

# ---------------------------------------------------------------------------
# Diff fixtures.  Each is a minimal but well-formed unified diff exercising one
# corpus case.  ``b/`` paths drive the test-file classification.
# ---------------------------------------------------------------------------

_CLEAN_CODE_FIX = """\
diff --git a/src/coordinare/services/widget.py b/src/coordinare/services/widget.py
index 1111111..2222222 100644
--- a/src/coordinare/services/widget.py
+++ b/src/coordinare/services/widget.py
@@ -10,7 +10,7 @@ def compute(rows):
-    total = sum(r.value for r in rows)
+    total = sum(r.value for r in rows if r.value is not None)
     return total
"""

_REMOVED_ASSERTION = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -4,7 +4,6 @@ def test_compute_totals():
     result = compute(rows)
     assert result.total == 42
-    assert result.skipped == 0
"""

_ADDED_SKIP_MARKER = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -1,5 +1,6 @@ import pytest

+@pytest.mark.skip(reason="flaky on CI")
 def test_compute_totals():
     result = compute(rows)
     assert result.total == 42
"""

_ADDED_XFAIL_MARKER = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -1,5 +1,6 @@ import pytest

+@pytest.mark.xfail(strict=False)
 def test_compute_totals():
     result = compute(rows)
     assert result.total == 42
"""

_LOOSENED_COMPARISON = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -4,5 +4,5 @@ def test_compute_totals():
     result = compute(rows)
-    assert result.total == 42
+    assert result.total >= 0
"""

_TOLERANCE_WIDENED = """\
diff --git a/tests/unit/test_metrics.py b/tests/unit/test_metrics.py
index 1111111..2222222 100644
--- a/tests/unit/test_metrics.py
+++ b/tests/unit/test_metrics.py
@@ -4,5 +4,5 @@ def test_ratio():
     result = ratio(a, b)
-    assert result == 0.5
+    assert result == pytest.approx(0.5, abs=0.4)
"""

_SWALLOWING_TRY_EXCEPT = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -3,4 +3,7 @@ def test_compute_totals():
     result = compute(rows)
-    assert result.total == 42
+    try:
+        assert result.total == 42
+    except AssertionError:
+        pass
"""

_CONDITIONAL_ASSERTION = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -3,3 +3,4 @@ def test_compute_totals():
     result = compute(rows)
-    assert result.total == 42
+    if result is not None:
+        assert result.total == 42
"""

_MOCKED_AWAY_REQUIREMENT = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -3,4 +3,4 @@ def test_persists_row():
-    store = RealStore(db)
+    store = MagicMock(return_value=True)
     store.save(row)
     assert store.contains(row.id)
"""

_REMOVED_SETUP = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -1,6 +1,3 @@ import unittest
 class WidgetTest(unittest.TestCase):
-    def setUp(self):
-        self.store = RealStore()
-
     def test_compute(self):
         assert compute([]) == 0
"""

_REMOVED_FIXTURE = """\
diff --git a/tests/unit/conftest.py b/tests/unit/conftest.py
index 1111111..2222222 100644
--- a/tests/unit/conftest.py
+++ b/tests/unit/conftest.py
@@ -1,6 +1,2 @@ import pytest
-@pytest.fixture
-def seeded_db():
-    return seed(RealStore())
-
 def helper():
     return 1
"""

# A test file rewritten with a bespoke assertion DSL the guard does not
# recognize.  It removes a substantive line of existing test code, so the guard
# cannot confidently clear it and must reject (uncertainty → unsafe).
_CUSTOM_FRAMEWORK = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -4,5 +4,5 @@ def test_compute_totals():
     result = compute(rows)
-    verify_that(result.total).is_exactly(42)
+    verify_that(result.total).is_at_least(0)
"""

# Adding a brand-new regression test (pure additions, real assertions, no
# weakening markers) is strengthening, not weakening — the guard clears it.
_PURE_TEST_ADDITION = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1111111..2222222 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -10,3 +10,6 @@ def test_compute_totals():
     assert compute(rows).total == 42
+
+def test_compute_handles_none():
+    assert compute([Row(None)]).total == 0
"""


# ---------------------------------------------------------------------------
# (a)-(f): every weakening in the corpus MUST be rejected, with a reason.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("diff", "reason_substr"),
    [
        pytest.param(_REMOVED_ASSERTION, "assert", id="removed-assertion"),
        pytest.param(_ADDED_SKIP_MARKER, "skip", id="added-skip"),
        pytest.param(_ADDED_XFAIL_MARKER, "xfail", id="added-xfail"),
        pytest.param(_LOOSENED_COMPARISON, "loosen", id="loosened-operator"),
        pytest.param(_TOLERANCE_WIDENED, "loosen", id="tolerance-widened"),
        pytest.param(_SWALLOWING_TRY_EXCEPT, "exception", id="swallowing-try-except"),
        pytest.param(_CONDITIONAL_ASSERTION, "conditional", id="conditional-assertion"),
        pytest.param(_MOCKED_AWAY_REQUIREMENT, "mock", id="mocked-away"),
        pytest.param(_REMOVED_SETUP, "setup", id="removed-setup"),
        pytest.param(_REMOVED_FIXTURE, "fixture", id="removed-fixture"),
    ],
)
def test_weakening_corpus_is_rejected(diff: str, reason_substr: str) -> None:
    """Every test-weakening diff in the SC-004 corpus → is_safe=False, and the
    flagged_patterns name the specific weakening (lower-cased match)."""
    is_safe, flagged = analyze_diff(diff)

    assert is_safe is False
    assert flagged, "a rejected diff must report at least one flagged pattern"
    assert all(isinstance(p, str) for p in flagged)
    assert any(reason_substr in p.lower() for p in flagged), (
        f"expected a flag mentioning {reason_substr!r}; got {flagged!r}"
    )


def test_unrecognized_framework_change_cannot_be_cleared() -> None:
    """A test file rewritten with a custom assertion DSL the guard does not
    recognize still removes existing test code → it cannot be confidently
    cleared, so the conservative bias rejects it (FR-019)."""
    is_safe, flagged = analyze_diff(_CUSTOM_FRAMEWORK)

    assert is_safe is False
    assert flagged
    assert any("confidently" in p.lower() or "clear" in p.lower() for p in flagged)


# ---------------------------------------------------------------------------
# Boundary: clean changes the guard MUST clear (no false positives).
# ---------------------------------------------------------------------------

def test_clean_code_only_fix_is_safe() -> None:
    """A fix that touches only production code (no test file) is exactly what
    L3 should produce — the static guard clears it (SC-004)."""
    is_safe, flagged = analyze_diff(_CLEAN_CODE_FIX)

    assert is_safe is True
    assert flagged == []


def test_pure_new_test_addition_is_safe() -> None:
    """Adding a brand-new test with real assertions (pure additions, no
    weakening markers, nothing removed) is strengthening, not weakening — the
    guard must not raise a false positive on it."""
    is_safe, flagged = analyze_diff(_PURE_TEST_ADDITION)

    assert is_safe is True
    assert flagged == []


def test_empty_diff_is_safe() -> None:
    """No changes → nothing to weaken."""
    assert analyze_diff("") == (True, [])


# ---------------------------------------------------------------------------
# Purity / determinism (Constitution II): same input → same output.
# ---------------------------------------------------------------------------

def test_analyze_diff_is_deterministic() -> None:
    """analyze_diff reads no clock/RNG/environment — repeated calls match."""
    first = analyze_diff(_MOCKED_AWAY_REQUIREMENT)
    second = analyze_diff(_MOCKED_AWAY_REQUIREMENT)

    assert first == second
    assert first[0] is False
