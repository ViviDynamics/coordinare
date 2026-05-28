"""Benchmark for spec 074 SC-006.

Times ``persona_classifier.classify()`` end-to-end across ≥20 synthetic cards
spanning the path-class spectrum (docs-only, security-sensitive, runtime,
config, tests, mixed).  Measures p50 / p95 wall-clock per cycle and asserts
the SC-006 budget: ≤2 s p50 / ≤10 s p95.

The classifier itself is bottlenecked by the conducting backend's LLM round
trip in production.  This benchmark stubs the backend with a configurable
synthetic latency so the coordinare-side overhead (input rendering, post-
processing, JSON parse) is isolated and verifiable.  ``--backend-latency-ms``
defaults to 0 to expose coordinare overhead; pass e.g. ``--backend-latency-ms
1500`` to model a realistic LLM round trip and confirm the total stays under
the SC-006 budget.

Run:
    .venv/bin/python scripts/bench_persona_classifier.py [--iterations N]
                                                          [--backend-latency-ms M]
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from types import SimpleNamespace
from typing import Any

from coordinare.services import persona_classifier


# 20 synthetic cards spanning the path-class spectrum.
_CARD_FIXTURES: list[dict[str, Any]] = [
    {"id": f"ITEM_{i}", "title": title, "body": body, "pr_url": f"https://github.com/acme/repo/pull/{i + 1}"}
    for i, (title, body) in enumerate(
        [
            ("Docs: README typo", "Fix typo"),
            ("Docs: update onboarding", "Refresh docs"),
            ("Security: rotate session token TTL", "Tighten auth"),
            ("Security: hash password upgrade", "bcrypt → argon2"),
            ("Runtime: add retry to client", "Network resilience"),
            ("Runtime: refactor scheduler", "Cleanup"),
            ("Tests: add coverage for parser", "More unit tests"),
            ("Tests: fix flaky integration", "Stabilize"),
            ("Config: bump python version", "3.11 → 3.12"),
            ("Config: new yaml key", "Add toggle"),
            ("Mixed: docs + runtime", "Cross-cutting"),
            ("Mixed: tests + config", "Cross-cutting"),
            ("Runtime: refactor large module", "Big change"),
            ("Security-sensitive auth.py", "Auth"),
            ("Docs-only quickstart", "Quickstart"),
            ("Runtime: small bugfix", "One-liner"),
            ("Tests: rename fixture", "Rename"),
            ("Config: deprecate old key", "Cleanup"),
            ("Runtime + security mixed", "Mixed"),
            ("Docs + config mixed", "Mixed"),
        ]
    )
]

# A representative PR-files response per card (varies file mix).
_FILE_FIXTURES: list[list[dict[str, Any]]] = [
    [{"filename": "README.md", "additions": 2, "deletions": 1, "status": "modified"}],
    [{"filename": "docs/onboarding.md", "additions": 30, "deletions": 5, "status": "modified"}],
    [{"filename": "src/auth/session.py", "additions": 4, "deletions": 4, "status": "modified"}],
    [{"filename": "src/auth/password.py", "additions": 60, "deletions": 40, "status": "modified"}],
    [{"filename": "src/client/http.py", "additions": 25, "deletions": 5, "status": "modified"}],
    [{"filename": "src/scheduler/core.py", "additions": 200, "deletions": 100, "status": "modified"}],
    [{"filename": "tests/unit/test_parser.py", "additions": 50, "deletions": 0, "status": "added"}],
    [{"filename": "tests/integration/test_flaky.py", "additions": 10, "deletions": 5, "status": "modified"}],
    [{"filename": "pyproject.toml", "additions": 1, "deletions": 1, "status": "modified"}],
    [{"filename": "config.yaml", "additions": 3, "deletions": 0, "status": "modified"}],
    [
        {"filename": "docs/README.md", "additions": 5, "deletions": 0, "status": "modified"},
        {"filename": "src/runtime/worker.py", "additions": 20, "deletions": 5, "status": "modified"},
    ],
    [
        {"filename": "tests/unit/test_x.py", "additions": 15, "deletions": 0, "status": "added"},
        {"filename": "config.yaml", "additions": 2, "deletions": 0, "status": "modified"},
    ],
    [{"filename": "src/big/module.py", "additions": 400, "deletions": 200, "status": "modified"}],
    [{"filename": "src/auth/oauth.py", "additions": 30, "deletions": 10, "status": "modified"}],
    [{"filename": "docs/quickstart.md", "additions": 80, "deletions": 0, "status": "added"}],
    [{"filename": "src/runtime/util.py", "additions": 2, "deletions": 1, "status": "modified"}],
    [{"filename": "tests/conftest.py", "additions": 3, "deletions": 3, "status": "modified"}],
    [{"filename": "config.yaml", "additions": 0, "deletions": 5, "status": "modified"}],
    [
        {"filename": "src/runtime/x.py", "additions": 10, "deletions": 0, "status": "modified"},
        {"filename": "src/auth/y.py", "additions": 5, "deletions": 0, "status": "modified"},
    ],
    [
        {"filename": "docs/x.md", "additions": 10, "deletions": 0, "status": "modified"},
        {"filename": "config.yaml", "additions": 1, "deletions": 0, "status": "modified"},
    ],
]


def _canned_response() -> dict[str, Any]:
    return {
        "data": {
            "personas": {
                "reviewer": {"depth": "normal", "focus": "x"},
                "security": {"depth": "skim", "focus": "x"},
                "qa": {"depth": "normal", "focus": "x"},
                "tech_writer": {"depth": "skim", "focus": "x"},
                "closer": {"depth": "full", "focus": "x"},
            }
        }
    }


class _StubBackend:
    def __init__(self, latency_ms: float) -> None:
        self._delay = latency_ms / 1000.0

    async def prompt(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return _canned_response()


class _StubGitHub:
    def __init__(self, files: list[dict[str, Any]]) -> None:
        self._files = files

    async def get_pr_files(self, _owner: str, _repo: str, _pr_number: int) -> dict[str, Any]:
        return {"files": self._files, "head_sha": "deadbeef"}


def _config() -> Any:
    return SimpleNamespace(
        persona_scope=SimpleNamespace(
            enabled=True,
            path_classes={
                "docs": ["docs/**", "*.md"],
                "config": ["config.yaml", "pyproject.toml"],
                "tests": ["tests/**"],
                "runtime": ["src/runtime/**", "src/scheduler/**", "src/client/**", "src/big/**"],
                "security_sensitive": ["src/auth/**"],
            },
            classifier_latency_budget_seconds=30.0,
            classifier_failure_warning_cooldown_seconds=600.0,
        ),
    )


async def _one_run(card: dict[str, Any], files: list[dict[str, Any]], backend_latency_ms: float) -> float:
    cfg = _config()
    backend = _StubBackend(backend_latency_ms)
    gh = _StubGitHub(files)
    session = {"feedback_cycle_count": 1}
    t0 = time.perf_counter()
    await persona_classifier.classify(
        session=session,
        card=card,
        conducting_backend=backend,
        config=cfg,
        github_service=gh,
        workspace_path=None,
        classifier_model="bench/stub",
    )
    return (time.perf_counter() - t0) * 1000.0


async def _bench(iterations: int, backend_latency_ms: float) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(iterations):
        for card, files in zip(_CARD_FIXTURES, _FILE_FIXTURES, strict=True):
            samples.append(await _one_run(card, files, backend_latency_ms))
    samples.sort()
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": samples[int(len(samples) * 0.95)] if len(samples) >= 20 else max(samples),
        "max_ms": max(samples),
        "min_ms": min(samples),
        "n": float(len(samples)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=1,
                    help="Number of passes over the 20-card fixture set")
    ap.add_argument("--backend-latency-ms", type=float, default=0.0,
                    help="Simulated backend LLM round-trip latency per call")
    ap.add_argument("--budget-p50-ms", type=float, default=2000.0)
    ap.add_argument("--budget-p95-ms", type=float, default=10000.0)
    args = ap.parse_args()

    stats = asyncio.run(_bench(args.iterations, args.backend_latency_ms))
    print(
        f"classifier samples n={int(stats['n'])} backend_latency={args.backend_latency_ms:.0f}ms: "
        f"p50={stats['median_ms']:.2f}ms  p95={stats['p95_ms']:.2f}ms  "
        f"min={stats['min_ms']:.2f}ms  max={stats['max_ms']:.2f}ms"
    )

    failed = False
    if stats["median_ms"] > args.budget_p50_ms:
        print(f"FAIL: p50 {stats['median_ms']:.2f}ms exceeds SC-006 budget {args.budget_p50_ms:.0f}ms",
              file=sys.stderr)
        failed = True
    if stats["p95_ms"] > args.budget_p95_ms:
        print(f"FAIL: p95 {stats['p95_ms']:.2f}ms exceeds SC-006 budget {args.budget_p95_ms:.0f}ms",
              file=sys.stderr)
        failed = True
    if failed:
        return 1
    print(f"PASS: within SC-006 budget (p50 ≤{args.budget_p50_ms:.0f}ms / p95 ≤{args.budget_p95_ms:.0f}ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
