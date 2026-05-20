"""Benchmark for spec 066 SC-002.

Measures wall-clock time for `check_board` against a synthetic board at
`max_concurrent_cards=1` to confirm the unified pickup path adds ≤ +50ms
of overhead vs. the pre-066 baseline (single-card branch).

Run:
    .venv/bin/python scripts/bench_066.py [--iterations N] [--board-size M]

The benchmark fixes `max_concurrent_cards=1` by design — SC-002 is the N=1
budget.  ``--board-size`` controls how many synthetic TODO cards are on the
board so we can probe behaviour as board size grows without changing the
concurrency dimension under test.

Exits non-zero if the median wall-clock at N=1 exceeds the SC-002 budget.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from types import SimpleNamespace

from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import initial_state


class _SyntheticBoard:
    def __init__(self, todo_cards: int) -> None:
        self.snapshot = {
            "TODO": [f"ITEM_{i}" for i in range(todo_cards)],
            "IN_PROGRESS": [],
            "IN_REVIEW": [],
        }
        self.titles = {f"ITEM_{i}": f"Card {i}" for i in range(todo_cards)}
        self.descriptions = {f"ITEM_{i}": f"Desc {i}" for i in range(todo_cards)}
        self.issue_numbers = {f"ITEM_{i}": i + 1 for i in range(todo_cards)}

    async def poll_board(self):
        return {
            "snapshot": self.snapshot,
            "titles": self.titles,
            "descriptions": self.descriptions,
            "issue_numbers": self.issue_numbers,
        }


def _config(n: int):
    return SimpleNamespace(
        github_org="acme",
        project_name="repo",
        max_concurrent_cards=n,
        priority=SimpleNamespace(field_name="", priority_order=[]),
        github_api_url="",
        assignee_filter=None,
    )


async def _one_run(board_size: int) -> float:
    state = initial_state()
    state["github_service"] = _SyntheticBoard(board_size)
    # SC-002 is the N=1 budget — concurrency is fixed at 1 by design here.
    state["config"] = _config(1)
    t0 = time.perf_counter()
    await check_board(state)
    return (time.perf_counter() - t0) * 1000.0


async def _bench(iterations: int, board_size: int) -> dict[str, float]:
    # Warm-up — first call sees import/JIT-style costs.
    await _one_run(board_size)
    samples: list[float] = []
    for _ in range(iterations):
        samples.append(await _one_run(board_size))
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": statistics.quantiles(samples, n=20)[18] if len(samples) >= 20 else max(samples),
        "max_ms": max(samples),
        "min_ms": min(samples),
        "n": float(len(samples)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=50)
    ap.add_argument(
        "--board-size",
        "--cards",
        dest="board_size",
        type=int,
        default=20,
        help="Number of synthetic TODO cards on the board (concurrency stays N=1)",
    )
    ap.add_argument("--budget-ms", type=float, default=50.0,
                    help="SC-002 overhead budget vs. a trivial baseline")
    args = ap.parse_args()

    stats = asyncio.run(_bench(args.iterations, args.board_size))
    print(
        f"check_board N=1, board_size={args.board_size}, iters={int(stats['n'])}: "
        f"median={stats['median_ms']:.2f}ms  p95={stats['p95_ms']:.2f}ms  "
        f"min={stats['min_ms']:.2f}ms  max={stats['max_ms']:.2f}ms"
    )

    # SC-002: the unified path must not add > 50ms overhead at N=1.
    # We approximate the pre-066 baseline as the median itself plus a 50ms
    # ceiling — the absolute budget. A failing run prints the diagnostic and
    # exits non-zero so this is callable from CI.
    if stats["median_ms"] > args.budget_ms:
        print(
            f"FAIL: median {stats['median_ms']:.2f}ms exceeds SC-002 budget "
            f"{args.budget_ms:.2f}ms",
            file=sys.stderr,
        )
        return 1
    print(f"PASS: within SC-002 budget ({args.budget_ms:.2f}ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
