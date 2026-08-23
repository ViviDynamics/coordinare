#!/usr/bin/env python
"""Spec 135 — CLI over coordinare.bench.grader / coordinare.bench.noise.

Grade an existing spec-134 run, or measure score noise across N repeats:

    python scripts/board_score.py score --run-dir runs/20260823-120000 [--fixtures m.yaml]
    python scripts/board_score.py noise --repeats 5 [--fixtures m.yaml] [--config c.yaml]

With no --fixtures, uses the built-in tiny fixture. Judging is opt-in
(--judge-base-url/--judge-model); without it grading is deterministic-only and
labeled as such. Weights are embedded in every score object.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from coordinare.bench.artifact import RunArtifact
from coordinare.bench.fixtures import load_manifest, tiny_fixture
from coordinare.bench.grader import GradingError, score_run
from coordinare.bench.judge import make_judge
from coordinare.bench.noise import NoiseError, run_repeats
from coordinare.bench.score import Weights


def _add_shared_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--fixtures", help="fixture manifest YAML (default: built-in tiny fixture)")
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
    p = argparse.ArgumentParser(description="Score board-simulation runs / measure noise (spec 135).")
    sub = p.add_subparsers(dest="command", required=True)

    score = sub.add_parser("score", help="grade one existing run dir (writes score.json)")
    score.add_argument("--run-dir", required=True, help="run dir holding run.json")
    _add_shared_flags(score)

    noise = sub.add_parser("noise", help="run + score one config N times, aggregate a noise report")
    noise.add_argument("--repeats", type=int, required=True, help="N repeats of the same config")
    noise.add_argument("--run-dir", default="runs", help="parent dir for the noise session (default: runs/)")
    noise.add_argument("--config", help="coordinare config YAML (fingerprinted into each artifact)")
    noise.add_argument("--max-cycles", type=int, default=None, help="hard cycle budget per repeat")
    noise.add_argument("--human-login", default="reviewer1", help="approving reviewer login")
    noise.add_argument("--cost-rate", type=float, default=3.0, help="USD per million tokens (estimate)")
    noise.add_argument("--real", action="store_true", help="dispatch real performers (default: stubbed)")
    _add_shared_flags(noise)

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


def _fixtures(args: argparse.Namespace):
    return load_manifest(args.fixtures) if args.fixtures else [tiny_fixture()]


def _cmd_score(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    run_json = run_dir / "run.json"
    if not run_json.exists():
        print(f"no run.json in {run_dir}", file=sys.stderr)
        return 2
    judge, judge_model = _judge(args)
    try:
        artifact = RunArtifact.load(run_json)
    except ValueError as exc:  # JSONDecodeError / pydantic ValidationError
        print(f"error: {run_json} is not a valid spec-134 run artifact: {exc}", file=sys.stderr)
        return 1
    score = score_run(
        artifact,
        _fixtures(args),
        weights=_weights(args),
        judge=judge,
        judge_model=judge_model,
    )
    path = score.write(run_dir)
    mode = "judged" if score.judged else "deterministic-only"
    scalar = "unrankable" if score.scalar is None else f"{score.scalar:.6g}"
    print(f"score ({mode}): scalar={scalar} "
          f"correctness={score.components.correctness_rate} "
          f"harness_failure_rate={score.components.harness_failure_rate} -> {path}")
    for v in score.cards:
        print(f"  {v.card_id} [{v.fixture_id}] {v.category}: {v.deterministic_detail}")
    return 0


async def _cmd_noise(args: argparse.Namespace) -> int:
    if args.repeats < 1:
        print("--repeats must be >= 1", file=sys.stderr)
        return 2
    judge, judge_model = _judge(args)
    base = Path(args.run_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-noise"
    session, n = base, 2
    while True:  # same-second launches must not share a session dir
        try:
            session.mkdir(parents=True, exist_ok=False)  # atomically claim it
            break
        except FileExistsError:
            session = base.with_name(f"{base.name}-{n}")
            n += 1
    report = await run_repeats(
        _fixtures(args),
        args.repeats,
        session,
        config_path=args.config,
        human_login=args.human_login,
        max_cycles=args.max_cycles,
        cost_per_million_tokens=args.cost_rate,
        stub=not args.real,
        weights=_weights(args),
        judge=judge,
        judge_model=judge_model,
    )
    print(f"noise: {report.effective_repeats}/{report.requested_repeats} repeats effective "
          f"-> {session / 'noise-report.md'}")
    if report.scalar_stats is not None:
        s = report.scalar_stats
        print(f"  scalar: mean={s.mean:.6g} stdev={s.stdev:.6g} (n={s.n})")
    for f in report.failures:
        print(f"  repeat #{f.index} FAILED: {f.error}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "score":
            return _cmd_score(args)
        return asyncio.run(_cmd_noise(args))
    except (GradingError, NoiseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
