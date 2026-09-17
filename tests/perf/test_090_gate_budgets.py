"""Spec 090 T036 — gate hot-path budget microbenchmarks.

Asserts the Constitution IV latency budgets for the spec-090 pure hot paths,
measured with in-memory fixtures (no network / disk / clock):

* L1 ``evaluate_base_gate`` coordinare-owned decision overhead ≤500ms p95
  (the head+base GraphQL fetch is network-bound and concurrent; only the pure
  gate decision is owned by coordinare — it must be a vanishingly small slice of
  the 500ms envelope).
* L2 per-rollup classification ≤50ms p95 (baseline index build + per-check
  signature + origin classification over a realistically large rollup).
* ``make_failure_signature`` ≤50µs per check and ≤5ms per rollup.
* static ``analyze_diff`` ≤100ms p95 on a 2000-line diff (research Decision 5).

Pure-Python overhead only — these guard against accidental algorithmic
regressions (e.g. an O(n^2) loop or a per-check recompile of a regex), not
against I/O latency.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from coordinare.graph.nodes.monitor_performer import _build_baseline_index
from coordinare.services.base_gate import evaluate_base_gate
from coordinare.services.ci_gate import FailedCheckWithSignature
from coordinare.services.failure_classification import classify_failure_origin
from coordinare.services.failure_signature import make_failure_signature
from coordinare.services.pr_checks_service import CheckEntry, CheckRollup
from coordinare.services.test_integrity_guard import analyze_diff

pytestmark = pytest.mark.benchmark


# A realistically large rollup: GitHub caps statusCheckRollup contexts at 100.
_ROLLUP_SIZE = 50


def _failing_entry(i: int, *, required: bool = True) -> CheckEntry:
    return CheckEntry(
        name=f"check-{i}",
        status="completed",
        conclusion="failure",
        is_required=required,
        details_url=f"https://ci.example.com/{i}",
        title=f"check-{i} failed",
        summary=f"assertion error in module {i}: expected 200 got 500 (line {i * 7})",
    )


def _head_rollup() -> CheckRollup:
    return CheckRollup(
        pr_number=42,
        head_sha="a" * 40,
        head_pushed_at=datetime(2026, 1, 1, tzinfo=UTC),
        branch_protection_readable=True,
        checks=[_failing_entry(i) for i in range(_ROLLUP_SIZE)],
        base_ref="main",
        rollup_origin="head",
    )


def _base_rollup() -> CheckRollup:
    # Every other check inherited (same name + same reason) so the classifier
    # exercises both the INHERITED match path and the INTRODUCED no-match path.
    return CheckRollup(
        pr_number=42,
        head_sha="b" * 40,
        head_pushed_at=None,
        branch_protection_readable=True,
        checks=[_failing_entry(i) for i in range(0, _ROLLUP_SIZE, 2)],
        base_ref="main",
        rollup_origin="base",
    )


def _percentiles(samples: list[float]) -> tuple[float, float]:
    samples.sort()
    n = len(samples)
    return samples[int(0.95 * n)], samples[int(0.99 * n)]


def test_evaluate_base_gate_l1_decision_under_500ms_p95() -> None:
    """L1 base gate decision overhead MUST be a tiny slice of the 500ms budget."""
    base = _base_rollup()
    samples: list[float] = []
    iterations = 2_000
    for _ in range(iterations):
        started = time.perf_counter()
        evaluate_base_gate(base)
        samples.append((time.perf_counter() - started) * 1000.0)  # ms

    p95, _ = _percentiles(samples)
    assert p95 < 500.0, f"evaluate_base_gate p95={p95:.3f}ms, expected <500ms"
    # The pure decision is the only coordinare-owned cost; it should be far below
    # the network-dominated envelope. A regression past 5ms means an algorithmic
    # problem (the fetch is what consumes the 500ms, not the decision).
    assert p95 < 5.0, f"evaluate_base_gate p95={p95:.3f}ms — algorithmic regression"


def test_classification_per_rollup_under_50ms_p95() -> None:
    """L2 classification of a full rollup MUST stay ≤50ms p95."""
    head = _head_rollup()
    base = _base_rollup()
    samples: list[float] = []
    iterations = 2_000
    for _ in range(iterations):
        started = time.perf_counter()
        baseline_index = _build_baseline_index(base)
        for entry in head.checks:
            head_sig, head_reason = make_failure_signature(
                entry.name, entry.conclusion or "failure", entry.title, entry.summary,
            )
            base_failure = baseline_index.get(entry.name) if baseline_index else None
            fc = FailedCheckWithSignature(
                name=entry.name,
                conclusion=entry.conclusion or "failure",
                html_url=entry.details_url,
                head_signature=head_sig,
                baseline_signature=base_failure.signature if base_failure else None,
            )
            classify_failure_origin(fc, head_reason, baseline_index)
        samples.append((time.perf_counter() - started) * 1000.0)  # ms

    p95, p99 = _percentiles(samples)
    assert p95 < 50.0, f"classification p95={p95:.3f}ms, expected <50ms"
    assert p99 < 100.0, f"classification p99={p99:.3f}ms, expected <100ms"


def test_make_failure_signature_under_50us_per_check_and_5ms_per_rollup() -> None:
    """``make_failure_signature`` MUST be ≤50µs/check and ≤5ms/rollup."""
    head = _head_rollup()

    # Per-check budget.
    per_check_us: list[float] = []
    iterations = 20_000
    sample = head.checks[0]
    for _ in range(iterations):
        started = time.perf_counter()
        make_failure_signature(
            sample.name, sample.conclusion or "failure", sample.title, sample.summary,
        )
        per_check_us.append((time.perf_counter() - started) * 1_000_000.0)  # µs

    per_check_us.sort()
    p95_us = per_check_us[int(0.95 * iterations)]
    assert p95_us < 50.0, f"make_failure_signature p95={p95_us:.2f}µs, expected <50µs"

    # Per-rollup budget (all checks).
    per_rollup_ms: list[float] = []
    rollup_iters = 2_000
    for _ in range(rollup_iters):
        started = time.perf_counter()
        for entry in head.checks:
            make_failure_signature(
                entry.name, entry.conclusion or "failure", entry.title, entry.summary,
            )
        per_rollup_ms.append((time.perf_counter() - started) * 1000.0)  # ms

    p95_ms, _ = _percentiles(per_rollup_ms)
    assert p95_ms < 5.0, f"make_failure_signature rollup p95={p95_ms:.3f}ms, expected <5ms"


def _two_thousand_line_test_diff() -> str:
    """A ~2000-line unified diff spread across test files.

    Test files are the only paths ``analyze_diff`` inspects, so to stress the
    worst case every hunk touches a test file with substantive added/removed
    lines (assertions, mocks, fixtures) that exercise the per-detector regexes.
    """
    parts: list[str] = []
    files = 20
    lines_per_file = 100  # 20 * 100 = 2000 diff body lines
    for f in range(files):
        path = f"tests/unit/test_module_{f}.py"
        parts.append(f"diff --git a/{path} b/{path}")
        parts.append(f"--- a/{path}")
        parts.append(f"+++ b/{path}")
        parts.append(f"@@ -1,{lines_per_file} +1,{lines_per_file} @@")
        for ln in range(lines_per_file):
            if ln % 5 == 0:
                parts.append(f"+    assert result_{ln} == expected_{ln}")
            elif ln % 5 == 1:
                parts.append(f"-    assert old_value_{ln} == {ln}")
            elif ln % 5 == 2:
                parts.append(f"+    mock_{ln} = MagicMock(return_value={ln})")
            elif ln % 5 == 3:
                parts.append(f"     value_{ln} = compute({ln})")
            else:
                parts.append(f"+    self.assertEqual(actual_{ln}, {ln})")
    return "\n".join(parts)


def test_analyze_diff_under_100ms_p95_on_2000_line_diff() -> None:
    """Static ``analyze_diff`` MUST stay ≤100ms p95 on a 2000-line diff."""
    diff = _two_thousand_line_test_diff()
    # Sanity: the fixture really is ~2000 body lines.
    body_lines = sum(1 for ln in diff.splitlines() if ln[:1] in "+- ")
    assert body_lines >= 2000, f"fixture only has {body_lines} body lines"

    samples: list[float] = []
    iterations = 200
    for _ in range(iterations):
        started = time.perf_counter()
        analyze_diff(diff)
        samples.append((time.perf_counter() - started) * 1000.0)  # ms

    p95, p99 = _percentiles(samples)
    assert p95 < 100.0, f"analyze_diff p95={p95:.3f}ms, expected <100ms"
    assert p99 < 200.0, f"analyze_diff p99={p99:.3f}ms, expected <200ms"
