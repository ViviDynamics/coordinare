#!/usr/bin/env python
"""Spec 137 — CLI over coordinare.bench.optimizer.

Run a budgeted, seed-deterministic adaptive search over a 136 search space:

    python scripts/board_optimize.py --space benchmarks/spaces/default.yaml \
        --seed 1 --max-evaluations 20

A real-substrate search (--real) REQUIRES --noise-report (a spec-135
noise-report.json measured on that substrate) — 135's go/no-go condition,
enforced. Stub searches default to 1 repeat per the committed stub
measurement and are labeled signal-free for gate/model dimensions.
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
from coordinare.bench.optimizer import real_evaluator, run_optimizer
from coordinare.bench.score import Weights
from coordinare.bench.space import SpaceError, load_space
from coordinare.bench.sweep import derive_repeats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Optimize coordinare config over the board benchmark (spec 137).")
    p.add_argument("--space", required=True, help="search-space definition YAML (spec 136)")
    p.add_argument("--run-dir", default="runs", help="parent dir for the session (default: runs/)")
    p.add_argument("--fixtures", help="fixture manifest YAML (default: built-in tiny fixture)")
    p.add_argument("--seed", type=int, default=1, help="search seed (determinism)")
    p.add_argument("--max-evaluations", type=int, default=30, help="hard cap on charged evaluations")
    p.add_argument("--max-seconds", type=float, default=3600.0, help="hard wall-clock cap")
    p.add_argument("--repeats", type=int, default=None, help="fixed repeats per evaluation")
    p.add_argument("--noise-report", help="spec-135 noise-report.json to derive repeats from "
                                          "(REQUIRED with --real)")
    p.add_argument("--resolution", type=float, default=0.01,
                   help="target standard error of the mean scalar (with --noise-report)")
    p.add_argument("--max-repeats", type=int, default=10, help="cap on derived repeats")
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
    """(repeats, source, noise_report_ref, notes) — enforcing the 135 gate for --real."""
    if args.real and not args.noise_report:
        print(
            "error: a real-substrate search requires a noise report measured on that "
            "substrate (spec-135's go/no-go condition). Produce one first:\n"
            "  python scripts/board_score.py noise --repeats 5 --real [--fixtures ...]\n"
            "then pass it via --noise-report.",
            file=sys.stderr,
        )
        raise SystemExit(2)
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
    return 1, "default", None, [
        "repeats=1 per the committed spec-135 stub measurement (scalar stdev 4.4e-06)"
    ]


async def _main_async(args: argparse.Namespace) -> int:
    loaded = load_space(args.space)
    repeats, source, report_ref, notes = _repeats(args)
    judge, judge_model = _judge(args)
    weights = _weights(args)
    fixtures = load_manifest(args.fixtures) if args.fixtures else [tiny_fixture()]

    base = Path(args.run_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-optimize"
    session, n = base, 2
    while True:  # same-second launches must not share a session dir
        try:
            session.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            session = base.with_name(f"{base.name}-{n}")
            n += 1

    total = 1
    for d in loaded.definition.dimensions:
        total *= len(dict.fromkeys(d.choices))
    print(f"space '{loaded.definition.name}': {total} grid configurations, "
          f"{len(loaded.definition.candidates)} candidate(s); budget "
          f"{args.max_evaluations} evaluations / {args.max_seconds:.0f}s; "
          f"{repeats} repeat(s) per evaluation [{source}]; seed {args.seed}")

    evaluate = real_evaluator(
        loaded, fixtures, session,
        repeats=repeats, weights=weights, judge=judge, judge_model=judge_model,
        stub=not args.real, human_login=args.human_login,
        max_cycles=args.max_cycles, cost_per_million_tokens=args.cost_rate,
    )
    artifact = await run_optimizer(
        loaded, evaluate,
        seed=args.seed, max_evaluations=args.max_evaluations, max_seconds=args.max_seconds,
        repeats=repeats, repeats_source=source, noise_report_ref=report_ref,
        weights=weights, evaluator_kind="real" if args.real else "stub",
        session_dir=session, extra_notes=notes,
    )

    b = artifact.budget
    print(f"search done ({b.stop_reason}): {b.charged_evaluations}/{b.max_evaluations} "
          f"evaluations (+{b.cache_hits} cached), coverage "
          f"{artifact.distinct_evaluated}/{artifact.space_total_combinations} "
          f"-> {session / 'optimizer-report.md'}")
    if artifact.recommendation is None:
        print("  NO recommendation — every evaluation failed")
        return 1
    r = artifact.recommendation
    print(f"  recommendation: eval #{r.eval_id} scalar={r.mean_scalar} fingerprint={r.fingerprint}")
    for k, v in sorted(r.overrides.items()):
        print(f"    {k} = {v!r}")
    for row in r.comparison:
        if row.label != "recommendation":
            delta = "" if row.delta_vs_recommendation is None else f" ({row.delta_vs_recommendation:+.6g})"
            dup = " [identical]" if row.duplicate_of_recommendation else ""
            print(f"  vs {row.label}: {row.mean_scalar}{delta}{dup}")
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
