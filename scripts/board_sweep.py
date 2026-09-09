#!/usr/bin/env python
"""Spec 136 — CLI over coordinare.bench.space / coordinare.bench.sweep.

Explore a declarative config search space through the board-simulation
benchmark (run via 134, score via 135):

    python scripts/board_sweep.py ablate     --space benchmarks/spaces/default.yaml
    python scripts/board_sweep.py candidates --space benchmarks/spaces/default.yaml

Prints the enumerated point count before running (no silent scope), and the
coverage reconciliation after (no silent truncation). Repeats per point:
--repeats N, or --noise-report <135 report> to derive them from the measured
scalar spread.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from coordinare.bench.fixtures import load_manifest, tiny_fixture
from coordinare.bench.judge import make_judge
from coordinare.bench.noise import NoiseReport
from coordinare.bench.score import Weights
from coordinare.bench.space import SpaceError, load_space
from coordinare.bench.sweep import enumerate_ablation, enumerate_candidates, run_sweep


def _add_shared_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--space", required=True, help="search-space definition YAML")
    p.add_argument("--run-dir", default="runs", help="parent dir for the sweep session (default: runs/)")
    p.add_argument("--fixtures", help="fixture manifest YAML (default: built-in tiny fixture)")
    p.add_argument("--repeats", type=int, default=None, help="fixed repeats per point (default: 1)")
    p.add_argument("--noise-report", help="spec-135 noise-report.json to derive repeats from")
    p.add_argument("--resolution", type=float, default=0.01,
                   help="target standard error of the mean scalar (with --noise-report)")
    p.add_argument("--max-repeats", type=int, default=10, help="cap on derived repeats")
    p.add_argument("--wall-clock-budget", type=float, default=1200.0,
                   help="real mode: hard deadline in seconds per run (default: 1200)")
    p.add_argument("--max-cycles", type=int, default=None, help="hard cycle budget per run")
    p.add_argument("--human-login", default="reviewer1", help="approving reviewer login")
    p.add_argument("--cost-rate", type=float, default=3.0, help="USD per million tokens (estimate)")
    p.add_argument("--real", action="store_true", help="dispatch real performers (default: stubbed)")
    p.add_argument("--judge-base-url", help="LiteLLM-proxy base URL; enables the LLM judge")
    p.add_argument("--judge-model", help="judge model id (required with --judge-base-url)")
    p.add_argument("--judge-api-key-env", default="LITELLM_API_KEY",
                   help="env var holding the judge API key (default: LITELLM_API_KEY)")
    p.add_argument("--w-correctness", type=float, default=1.0)
    p.add_argument("--w-cost", type=float, default=0.1)
    p.add_argument("--w-time", type=float, default=0.1)
    p.add_argument("--cost-budget", type=float, default=1.0, help="USD normalizer for the cost term")
    p.add_argument("--time-budget", type=float, default=600.0, help="seconds normalizer for the time term")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep a config search space over the board benchmark (spec 136).")
    sub = p.add_subparsers(dest="command", required=True)
    _add_shared_flags(sub.add_parser("ablate", help="baseline + one run per dimension alternative"))
    _add_shared_flags(sub.add_parser("candidates", help="run + rank the named candidate configs"))
    return p.parse_args(argv)


def _weights(args: argparse.Namespace) -> Weights:
    try:
        return Weights(
            w_correctness=args.w_correctness,
            w_cost=args.w_cost,
            w_time=args.w_time,
            cost_budget_usd=args.cost_budget,
            time_budget_seconds=args.time_budget,
        )
    except ValidationError as exc:
        print(f"invalid weights: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _judge(args: argparse.Namespace):
    if not args.judge_base_url:
        return None, None
    if not args.judge_model:
        print("--judge-model is required with --judge-base-url", file=sys.stderr)
        raise SystemExit(2)
    api_key = os.environ.get(args.judge_api_key_env, "")
    if not api_key:
        print(f"warning: ${args.judge_api_key_env} is empty — judge calls will carry no "
              "credentials and may fail with judge_error", file=sys.stderr)
    return make_judge(args.judge_base_url, api_key, args.judge_model), args.judge_model


def _repeats(args: argparse.Namespace):
    """(repeats, source, noise_report_ref, notes) from the CLI flags."""
    from coordinare.bench.sweep import derive_repeats

    if args.repeats is not None:
        if args.repeats < 1:
            print("--repeats must be >= 1", file=sys.stderr)
            raise SystemExit(2)
        if args.noise_report:
            print("use either --repeats or --noise-report, not both", file=sys.stderr)
            raise SystemExit(2)
        return args.repeats, "operator", None, []
    if args.noise_report:
        try:
            report = NoiseReport.load(args.noise_report)
        except (OSError, ValueError) as exc:
            print(f"error: cannot load noise report {args.noise_report}: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        repeats, source, note = derive_repeats(
            report, resolution=args.resolution, max_repeats=args.max_repeats
        )
        return repeats, source, args.noise_report, ([note] if note else [])
    return 1, "default", None, []


async def _main_async(args: argparse.Namespace) -> int:
    loaded = load_space(args.space)
    mode = "ablation" if args.command == "ablate" else "candidates"
    specs = (
        [None, *enumerate_ablation(loaded)] if mode == "ablation" else enumerate_candidates(loaded)
    )
    repeats, source, report_ref, notes = _repeats(args)
    judge, judge_model = _judge(args)
    fixtures = load_manifest(args.fixtures) if args.fixtures else [tiny_fixture()]

    print(f"space '{loaded.definition.name}': {len(specs)} point(s) declared "
          f"({mode}), {repeats} repeat(s) per point [{source}]")

    base = Path(args.run_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-sweep"
    session, n = base, 2
    while True:  # same-second launches must not share a session dir
        try:
            session.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            session = base.with_name(f"{base.name}-{n}")
            n += 1

    artifact = await run_sweep(
        loaded, mode, session,
        fixtures=fixtures,
        repeats=repeats, repeats_source=source, noise_report_ref=report_ref,
        repeat_notes=notes,
        weights=_weights(args), judge=judge, judge_model=judge_model,
        stub=not args.real, human_login=args.human_login,
        max_cycles=args.max_cycles, cost_per_million_tokens=args.cost_rate,
        wall_clock_budget_seconds=args.wall_clock_budget,
    )

    c = artifact.coverage
    print(f"sweep done: declared={c.declared_points} scored={c.scored_points} "
          f"dropped={len(c.dropped)} -> {session / 'sweep-report.md'}")
    for d in c.dropped:
        print(f"  DROPPED {d.point_id}: {d.reason}")
    for delta in artifact.deltas:
        print(f"  {delta.dimension}={delta.value!r}: Δscalar="
              f"{'' if delta.scalar_delta is None else f'{delta.scalar_delta:+.6g}'}")
    for i, pid in enumerate(artifact.ranking, start=1):
        print(f"  #{i} {pid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except SpaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
