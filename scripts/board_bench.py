#!/usr/bin/env python
"""Spec 134 — CLI over coordinare.bench.runner.run_board.

Runs the board-simulation benchmark once and writes a run artifact.

    python scripts/board_bench.py --fixtures manifest.yaml --run-dir runs/

With no --fixtures, uses the built-in tiny fixture. By default the performer is
stubbed (deterministic, no model cost); pass --real to dispatch real performers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from coordinare.bench.fixtures import load_manifest, tiny_fixture
from coordinare.bench.runner import run_board


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the board-simulation benchmark (spec 134).")
    p.add_argument("--config", help="coordinare config YAML (fingerprinted into the artifact)")
    p.add_argument("--fixtures", help="fixture manifest YAML (default: built-in tiny fixture)")
    p.add_argument("--run-dir", default="runs", help="directory for run.json (default: runs/)")
    p.add_argument("--max-cycles", type=int, default=None,
                   help="cycle ceiling (default: stub=3N+6; real=budget/poll). Real mode is "
                        "bounded by --wall-clock-budget, not by this.")
    p.add_argument("--human-login", default="reviewer1", help="approving reviewer login")
    p.add_argument("--cost-rate", type=float, default=3.0, help="USD per million tokens (estimate)")
    p.add_argument("--real", action="store_true", help="dispatch real performers (default: stubbed)")
    p.add_argument("--poll-interval", type=float, default=5.0,
                   help="real mode: seconds between performer status polls (default: 5)")
    p.add_argument("--wall-clock-budget", type=float, default=1200.0,
                   help="real mode: hard deadline in seconds — the run is cancelled and the "
                        "artifact emitted when it elapses (default: 1200)")
    return p.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    fixtures = load_manifest(args.fixtures) if args.fixtures else [tiny_fixture()]
    if not fixtures:
        print("no fixtures to run", file=sys.stderr)
        return 2
    run_dir = Path(args.run_dir) / time.strftime("%Y%m%d-%H%M%S")
    artifact = await run_board(
        fixtures,
        run_dir,
        config_path=args.config,
        human_login=args.human_login,
        max_cycles=args.max_cycles,
        cost_per_million_tokens=args.cost_rate,
        stub=not args.real,
        real_poll_interval_seconds=args.poll_interval,
        wall_clock_budget_seconds=args.wall_clock_budget,
    )
    t = artifact.totals
    print(f"run {artifact.run_id}: {t.cards_merged}/{t.cards_total} merged, "
          f"{t.cards_terminal_nonmerge} non-merge terminal -> {run_dir / 'run.json'}")
    for c in artifact.cards:
        print(f"  {c.card_id} [{c.fixture_id}] -> {c.final_state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main_async(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
