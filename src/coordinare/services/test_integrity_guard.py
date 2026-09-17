"""090 US3 (L3) — static half of the dual test-integrity guard (FR-019, FR-020).

``analyze_diff`` is a pure function over a unified diff that decides whether an
autonomous baseline-repair candidate may be allowed to *land as a candidate*
(it never governs merge — FR-021).  Its single job is to detect a repair that
makes CI pass by *weakening a test* rather than fixing the code.

    analyze_diff(diff: str) -> tuple[bool, list[str]]   # (is_safe, flagged_patterns)

Contract (FR-019, SC-004):

* It reasons **only about test files** — a production-code change that touches
  no test is always clearable here (the do-not-weaken mandate is about tests;
  production ``assert`` statements are out of scope).
* For any touched test file it runs a battery of weakening detectors (skip/xfail
  markers, removed assertions, loosened comparisons, swallowed/conditional
  assertions, mocked-away requirements, removed setup/teardown/fixtures).
* **Conservative bias**: if a test file has substantive removed lines that *no*
  specific detector explains, the guard cannot confidently clear the change and
  therefore rejects it (``is_safe=False``).  Uncertainty rejects.
* It is pure and deterministic: no clock, RNG, filesystem, or environment reads
  (Constitution II) — same input always yields the same output.

The guard is conservative by construction: false negatives (clearing a real
weakening) are far more costly than false positives (rejecting a safe change),
because a rejected repair simply escalates to a human (FR-021).
"""
from __future__ import annotations

import re

__all__ = ["analyze_diff", "is_test_path"]


# ---------------------------------------------------------------------------
# Test-file classification.  Path heuristics covering the common conventions
# across pytest/unittest (Python), Jest/Vitest (JS/TS), JUnit (Java), xUnit
# (C#), and RSpec (Ruby).  A change to a non-test file is out of scope.
# ---------------------------------------------------------------------------

_TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)")
_TEST_FILE_RE = re.compile(
    r"""
    (^|/)
    (
        test_[^/]+                 # pytest:  test_foo.py
      | conftest\.py               # pytest:  shared fixtures
      | [^/]+_test\.[^/]+          # go/py:   foo_test.py
      | [^/]+Test\.[^/]+           # junit:   FooTest.java
      | [^/]+Tests\.[^/]+          # xunit:   FooTests.cs
      | [^/]+_spec\.[^/]+          # rspec:   foo_spec.rb
      | [^/]+\.(test|spec)\.[^/]+  # jest:    foo.test.ts / foo.spec.ts
    )
    $
    """,
    re.VERBOSE,
)


def is_test_path(path: str) -> bool:
    """True when *path* names a test file under the common conventions.

    A file is a test file if it lives under a ``tests/``/``test/``/``spec/``/
    ``__tests__/`` directory, or its basename matches a recognised test-file
    naming convention.  ``conftest.py`` (pytest fixtures) counts even though it
    does not carry a ``test`` prefix, because removing a fixture weakens tests.
    """
    if _TEST_FILE_RE.search(path):
        return True
    return bool(_TEST_DIR_RE.search(path))


# ---------------------------------------------------------------------------
# Per-detector patterns.  Each operates on the *added* or *removed* line set of
# a single test file.  Lines are the diff payload with the leading +/- stripped.
# ---------------------------------------------------------------------------

# (b) skip / xfail / disable markers added to a test.
_SKIP_RE = re.compile(
    r"""
    @ \s* (pytest \. mark \. )? skip(if)?\b   # @pytest.mark.skip / skipif / @skip
  | @ \s* (pytest \. mark \. )? xfail\b       # @pytest.mark.xfail / @xfail
  | @ \s* unittest \. skip\b                  # @unittest.skip(...)
  | @ \s* (Disabled|Ignore)\b                 # junit @Disabled / testng/@Ignore
  | \. skip \s* \(                            # jest:  it.skip( / describe.skip(
  | \b x(it|describe|test) \s* \(             # jest:  xit( / xdescribe( / xtest(
  | pytest \. skip \s* \(                     # imperative pytest.skip(...)
    """,
    re.VERBOSE,
)
_XFAIL_RE = re.compile(r"\bxfail\b")

# An assertion line, across frameworks.
_ASSERT_RE = re.compile(
    r"""
    \b assert \b            # python assert / junit assert* / chai-ish
  | \b expect \s* \(        # jest / chai expect(
  | \. should \b            # rspec / chai .should
  | \b assertEqual \b
  | \b assertTrue \b
  | \b assertFalse \b
  | \b assertIs \b
  | \b assertRaises \b
    """,
    re.VERBOSE,
)

# Strict comparison tokens whose disappearance signals a loosened assertion.
_STRICT_RE = re.compile(
    r"""
    == | \bis\b
  | \b assertEqual \b | \b assertTrue \b | \b assertIs \b
  | \b toBe \b | \b toEqual \b | \b toStrictEqual \b
  | \b is_exactly \b | \b is_equal_to \b
    """,
    re.VERBOSE,
)
# Loose comparison / tolerance tokens whose appearance signals a loosened assertion.
_LOOSE_RE = re.compile(
    r"""
    != | >= | <= | (?<![=!<>]) > (?!=) | (?<![=!<>]) < (?!=)
  | \b approx \b | \b almost \b
  | \b assertGreater(Equal)? \b | \b assertLess(Equal)? \b
  | \b assertAlmostEqual \b | \b assertNotEqual \b | \b assertIsNot \b
  | \b places \s* = | \b atol \b | \b rtol \b | \b delta \s* =
  | \b toBeCloseTo \b | \b toBeGreaterThan \b | \b toBeLessThan \b
  | \b is_at_least \b | \b is_at_most \b | \b is_close_to \b
    """,
    re.VERBOSE,
)

# Mock-introduction tokens.
_MOCK_RE = re.compile(
    r"""
    \b MagicMock \b | \b Mock \b | \b AsyncMock \b
  | \b mock \. patch \b | \b patch \s* \(
  | \b return_value \b | \b side_effect \b
  | \b mocker \. | \b unittest \. mock \b
  | \b jest \. (fn|mock|spyOn) \b | \. mockReturnValue \b | \. mockResolvedValue \b
    """,
    re.VERBOSE,
)

# setUp / tearDown / fixture definitions whose removal weakens the harness.
_FIXTURE_RE = re.compile(
    r"""
    \b def \s+ (setUp|tearDown|setUpClass|tearDownClass|setup_method|teardown_method)\b
  | \b def \s+ (setup_class|teardown_class|setup_module|teardown_module)\b
  | @ \s* (pytest \. )? fixture \b
  | @ \s* (Before|After)(Each|All|Class)? \b      # junit/jest before/after hooks
  | \b before(Each|All)? \s* \( | \b after(Each|All)? \s* \(   # jest hooks
    """,
    re.VERBOSE,
)

# Control-flow keywords whose appearance can hide an assertion behind a branch.
_CONDITIONAL_RE = re.compile(r"^\s*(if|elif|while|for)\b")
_TRY_RE = re.compile(r"^\s*try\s*:")
_EXCEPT_RE = re.compile(r"^\s*except\b")
_SWALLOW_RE = re.compile(r"^\s*(pass|\.\.\.|continue)\s*$")

# A "substantive" line: not blank, not a pure comment, not a bare-decorator/import
# noise line.  Used by the conservative catch-all.
_COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*|\*/)")


def _strip_payload(lines: list[str]) -> list[str]:
    """Drop the leading +/- and return the raw content of each diff line."""
    return [ln[1:] for ln in lines]


def _is_substantive(line: str) -> bool:
    """True for a line that carries test logic (not blank / not a comment)."""
    stripped = line.strip()
    if not stripped:
        return False
    return not _COMMENT_RE.match(line)


def _count_assertions(lines: list[str]) -> int:
    return sum(1 for ln in lines if _ASSERT_RE.search(ln))


# ---------------------------------------------------------------------------
# Diff parsing.
# ---------------------------------------------------------------------------

class _FileDiff:
    """The added / removed payload lines for one file in a unified diff."""

    __slots__ = ("added", "path", "removed")

    def __init__(self, path: str) -> None:
        self.path = path
        self.added: list[str] = []
        self.removed: list[str] = []


_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_PLUS_HDR_RE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>.+)$")
_MINUS_HDR_RE = re.compile(r"^--- (?:a/)?(?P<path>.+)$")


def _parse_diff(diff: str) -> list[_FileDiff]:
    """Parse a unified diff into per-file added/removed payload line sets.

    Resilient to ``diff --git`` headers, ``+++``/``---`` file headers, ``@@``
    hunk headers, and ``\\ No newline`` markers; everything else is treated as a
    body line and bucketed by its leading character.
    """
    files: list[_FileDiff] = []
    current: _FileDiff | None = None

    for raw in diff.splitlines():
        git_match = _DIFF_GIT_RE.match(raw)
        if git_match:
            path = git_match.group("b")
            current = _FileDiff(path if path != "/dev/null" else git_match.group("a"))
            files.append(current)
            continue

        if raw.startswith("+++"):
            hdr = _PLUS_HDR_RE.match(raw)
            if hdr:
                path = hdr.group("path").split("\t", 1)[0]
                if path != "/dev/null":
                    if current is None:
                        current = _FileDiff(path)
                        files.append(current)
                    else:
                        current.path = path
            continue

        if raw.startswith("---"):
            # ``---`` old-file header (only useful when new file is /dev/null).
            hdr = _MINUS_HDR_RE.match(raw)
            if hdr and current is not None and current.path == "/dev/null":
                path = hdr.group("path").split("\t", 1)[0]
                if path != "/dev/null":
                    current.path = path
            continue

        if raw.startswith(("@@", "\\")):
            continue
        if raw.startswith(("index ", "new file", "deleted file", "old mode", "new mode",
                           "rename ", "similarity ", "copy ")):
            continue

        if current is None:
            continue
        if raw.startswith("+"):
            current.added.append(raw)
        elif raw.startswith("-"):
            current.removed.append(raw)
        # context lines (leading space) carry no signal here

    return files


# ---------------------------------------------------------------------------
# Per-file weakening analysis.
# ---------------------------------------------------------------------------

def _analyze_test_file(fd: _FileDiff) -> list[str]:
    """Return the weakening reasons found in one *test* file's diff.

    An empty list means "no weakening detected"; for a test file with
    substantive removals that no detector explained, a conservative catch-all
    reason is appended so the guard cannot be fooled by an unrecognised
    weakening (FR-019 — cannot confidently clear ⇒ unsafe).
    """
    reasons: list[str] = []
    added = _strip_payload(fd.added)
    removed = _strip_payload(fd.removed)

    removed_sub = [ln for ln in removed if _is_substantive(ln)]

    # (b) skip / xfail / disable markers added.
    if any(_SKIP_RE.search(ln) for ln in added):
        if any(_XFAIL_RE.search(ln) for ln in added):
            reasons.append(f"{fd.path}: added an xfail marker — disables the test")
        else:
            reasons.append(f"{fd.path}: added a skip/disable marker — disables the test")

    # (a) removed assertion(s): fewer assertions after than before.
    removed_asserts = _count_assertions(removed)
    added_asserts = _count_assertions(added)
    if removed_asserts > added_asserts:
        reasons.append(
            f"{fd.path}: removed assertion(s) "
            f"({removed_asserts} removed vs {added_asserts} added)",
        )

    # (c) loosened comparison / widened tolerance: a strict assertion replaced
    # by a loose one.  Only fires when an assertion was both removed and added
    # (a replacement), to avoid flagging a pure new loose assertion addition.
    if removed_asserts and added_asserts:
        removed_strict = any(
            _ASSERT_RE.search(ln) and _STRICT_RE.search(ln) for ln in removed
        )
        added_loose = any(
            _ASSERT_RE.search(ln) and _LOOSE_RE.search(ln) for ln in added
        )
        if removed_strict and added_loose:
            reasons.append(
                f"{fd.path}: loosened an assertion comparison/tolerance "
                "(strict check replaced by a looser one)",
            )

    # (d) assertion swallowed by a try/except or made conditional.
    added_try = any(_TRY_RE.match(ln) for ln in added)
    added_except = any(_EXCEPT_RE.match(ln) for ln in added)
    added_swallow = any(_SWALLOW_RE.match(ln) for ln in added)
    if added_try and added_except and (added_swallow or added_asserts):
        reasons.append(
            f"{fd.path}: wrapped assertion in a try/except that swallows the "
            "AssertionError exception (failure no longer propagates)",
        )
    added_conditional = any(_CONDITIONAL_RE.match(ln) for ln in added)
    if added_conditional and added_asserts and removed_asserts:
        reasons.append(
            f"{fd.path}: made an assertion conditional "
            "(it may now be skipped at runtime)",
        )

    # (e) a real requirement mocked away: a mock introduced while a substantive
    # non-mock line was removed (the real collaborator swapped for a stub).
    added_mock = any(_MOCK_RE.search(ln) for ln in added)
    removed_non_mock = any(
        not _MOCK_RE.search(ln) for ln in removed_sub
    )
    if added_mock and removed_non_mock and not any("mock" in r.lower() for r in reasons):
        reasons.append(
            f"{fd.path}: replaced a real requirement with a mock "
            "(the behaviour is no longer exercised)",
        )

    # (f) removed setUp / tearDown / fixture.
    if any(_FIXTURE_RE.search(ln) for ln in removed):
        reasons.append(
            f"{fd.path}: removed a setUp/tearDown/fixture "
            "(test preconditions no longer established)",
        )

    # Conservative catch-all: a test file with substantive removed lines that no
    # specific detector explained cannot be confidently cleared.  Pure additions
    # (nothing substantive removed) are strengthening and stay clear.
    if removed_sub and not reasons:
        reasons.append(
            f"{fd.path}: existing test code was modified in a way the guard "
            "cannot confidently clear (conservative reject)",
        )

    return reasons


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def analyze_diff(diff: str) -> tuple[bool, list[str]]:
    """Statically judge whether *diff* weakens any test (FR-019, SC-004).

    Returns ``(is_safe, flagged_patterns)``:

    * ``is_safe`` is ``True`` only when no test file in the diff shows any sign
      of weakening (and any test-file modification the guard does not recognise
      is treated as a weakening — uncertainty rejects).
    * ``flagged_patterns`` lists a human-readable reason per detected weakening;
      it is empty exactly when ``is_safe`` is ``True``.

    Pure and deterministic — no clock, RNG, filesystem, or environment reads.
    """
    if not diff or not diff.strip():
        return True, []

    flagged: list[str] = []
    for fd in _parse_diff(diff):
        if not is_test_path(fd.path):
            continue  # production-code change — out of scope for test integrity
        flagged.extend(_analyze_test_file(fd))

    return (not flagged), flagged
