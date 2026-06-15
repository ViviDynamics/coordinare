"""090 US2 (L2) — tests for the reason-sensitive failure signature.

Background
----------
Layer 2 of spec 090 (Baseline Repair Autonomy) classifies each failing HEAD
check against a merge-base baseline.  The keystone of that classification is a
**reason-sensitive failure signature**: a check that fails for a *different
reason* than the baseline must classify INTRODUCED, never INHERITED (FR-009,
anti-masking).  ``normalize_reason`` strips only volatile *drift* (timestamps,
durations, run IDs, hashes, UUIDs, line/column numbers) so that the same root
cause produces the same signature run-to-run, while a genuinely different root
cause produces a different signature.

What these tests assert
-----------------------
- ``normalize_reason`` source selection: title primary / summary fallback /
  ``""`` when both are absent or whitespace-only.
- Lowercasing + whitespace collapse.
- Each drift class is stripped to its placeholder token.
- Identical root cause with drift → identical signature (the masking guard's
  *floor*: real inherited failures still match).
- Genuinely different reason → different signature (the anti-masking *ceiling*:
  status codes, error types, and bare numbers are NOT drift).
- ``make_failure_signature`` hash is exactly 16 lowercase hex chars and its
  returned normalized reason equals ``normalize_reason``.
- Pure / deterministic: repeated calls are byte-identical (no clock/RNG/env).
"""
from __future__ import annotations

import re

from coordinare.services.failure_signature import (
    make_failure_signature,
    normalize_reason,
)

_HEX16 = re.compile(r"^[0-9a-f]{16}$")


# ---------------------------------------------------------------------------
# Source selection: title primary / summary fallback / "" when absent.
# ---------------------------------------------------------------------------


def test_source_title_primary() -> None:
    assert normalize_reason("Build failed", "ignored summary") == "build failed"


def test_source_summary_fallback_when_title_none() -> None:
    assert normalize_reason(None, "Tests did not pass") == "tests did not pass"


def test_source_summary_fallback_when_title_blank() -> None:
    # A whitespace-only title is treated as absent → fall through to summary.
    assert normalize_reason("   ", "Tests did not pass") == "tests did not pass"


def test_source_empty_when_both_absent() -> None:
    assert normalize_reason(None, None) == ""


def test_source_empty_when_both_blank() -> None:
    assert normalize_reason("  ", "\t\n ") == ""


# ---------------------------------------------------------------------------
# Lowercasing + whitespace collapse.
# ---------------------------------------------------------------------------


def test_lowercases() -> None:
    assert normalize_reason("AssertionError In TestFoo", None) == (
        "assertionerror in testfoo"
    )


def test_collapses_whitespace_runs() -> None:
    assert normalize_reason("a\t\t b\n\nc   d", None) == "a b c d"


# ---------------------------------------------------------------------------
# Drift stripping — one case per drift class.
# ---------------------------------------------------------------------------


def test_strips_iso_timestamp() -> None:
    assert (
        normalize_reason("failed at 2026-06-14T12:34:56.789Z while running", None)
        == "failed at <ts> while running"
    )


def test_strips_iso_date_only() -> None:
    assert normalize_reason("snapshot from 2026-06-14 differs", None) == (
        "snapshot from <ts> differs"
    )


def test_strips_duration() -> None:
    assert normalize_reason("timed out after 1m3s", None) == "timed out after <dur>"
    assert normalize_reason("slow: 42ms elapsed", None) == "slow: <dur> elapsed"
    assert normalize_reason("took 1.2s total", None) == "took <dur> total"


def test_strips_run_job_ids() -> None:
    assert normalize_reason("see run #12345 for details", None) == (
        "see <id> for details"
    )
    assert normalize_reason("job 678 cancelled", None) == "<id> cancelled"


def test_strips_long_hex_and_sha() -> None:
    assert normalize_reason("commit deadbeefcafebabe0123 broke it", None) == (
        "commit <hex> broke it"
    )


def test_strips_uuid() -> None:
    assert (
        normalize_reason(
            "trace 123e4567-e89b-12d3-a456-426614174000 aborted", None
        )
        == "trace <uuid> aborted"
    )


def test_strips_path_with_embedded_hash() -> None:
    # A path whose token embeds a long hex run collapses whole to <path>, not a
    # partial <hex> substitution leaving slashes behind.
    assert (
        normalize_reason(
            "artifact /tmp/cache/9f8e7d6c5b4a3210ff/out.log missing", None
        )
        == "artifact <path> missing"
    )


def test_strips_line_col_suffix() -> None:
    assert normalize_reason("error at foo.py:123:7 unexpected", None) == (
        "error at foo.py:<n> unexpected"
    )
    assert normalize_reason("error at foo.py:123 unexpected", None) == (
        "error at foo.py:<n> unexpected"
    )


# ---------------------------------------------------------------------------
# Floor — identical root cause with drift → identical signature.
# ---------------------------------------------------------------------------


def test_identical_root_cause_with_drift_same_signature() -> None:
    name, conclusion = "ci/test", "failure"
    a_sig, a_norm = make_failure_signature(
        name,
        conclusion,
        "AssertionError at 2026-06-14T00:00:00Z run #123: expected 200",
        None,
    )
    b_sig, b_norm = make_failure_signature(
        name,
        conclusion,
        "AssertionError at 2026-06-13T23:59:59Z run #456: expected 200",
        None,
    )
    assert a_norm == b_norm == "assertionerror at <ts> <id>: expected 200"
    assert a_sig == b_sig


# ---------------------------------------------------------------------------
# Ceiling (anti-masking) — genuinely different reason → different signature.
# ---------------------------------------------------------------------------


def test_different_error_type_different_signature() -> None:
    a, _ = make_failure_signature("ci/test", "failure", "ConnectionError: refused", None)
    b, _ = make_failure_signature("ci/test", "failure", "TimeoutError: deadline", None)
    assert a != b


def test_different_status_code_different_signature() -> None:
    # Bare numbers (HTTP status codes) are NOT drift — they distinguish reasons.
    a, _ = make_failure_signature("ci/test", "failure", "expected 200 got 500", None)
    b, _ = make_failure_signature("ci/test", "failure", "expected 200 got 404", None)
    assert a != b


def test_exit_code_not_stripped() -> None:
    # "exit code 1" vs "exit code 2": no id prefix word, no leading-colon digit,
    # so the bare number survives → different reasons → different signatures.
    a, an = make_failure_signature("ci/test", "failure", "exit code 1", None)
    b, bn = make_failure_signature("ci/test", "failure", "exit code 2", None)
    assert an == "exit code 1"
    assert bn == "exit code 2"
    assert a != b


def test_different_name_different_signature() -> None:
    a, _ = make_failure_signature("ci/test", "failure", "boom", None)
    b, _ = make_failure_signature("ci/lint", "failure", "boom", None)
    assert a != b


def test_different_conclusion_different_signature() -> None:
    # The conclusion is part of the signed payload: same name + reason but a
    # different conclusion is a different signature.
    a, _ = make_failure_signature("ci/test", "failure", "boom", None)
    b, _ = make_failure_signature("ci/test", "timed_out", "boom", None)
    assert a != b


# ---------------------------------------------------------------------------
# Hash shape + determinism + contract between the two functions.
# ---------------------------------------------------------------------------


def test_hash_is_16_lowercase_hex() -> None:
    sig, _ = make_failure_signature("ci/test", "failure", "anything", None)
    assert _HEX16.match(sig)


def test_normalized_return_matches_normalize_reason() -> None:
    title, summary = "Failed at run #99 in 2.5s", None
    sig, norm = make_failure_signature("ci/test", "failure", title, summary)
    assert norm == normalize_reason(title, summary)
    assert _HEX16.match(sig)


def test_deterministic_repeated_calls() -> None:
    args = ("ci/test", "failure", "AssertionError at 2026-06-14T00:00:00Z", None)
    first = make_failure_signature(*args)
    second = make_failure_signature(*args)
    assert first == second


def test_empty_reason_is_stable() -> None:
    # Both absent → "" normalized reason, but still a well-formed 16-hex hash
    # over (name, conclusion, "").
    sig, norm = make_failure_signature("ci/test", "failure", None, None)
    assert norm == ""
    assert _HEX16.match(sig)
