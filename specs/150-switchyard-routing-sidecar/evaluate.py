#!/usr/bin/env python
"""Opt-in real implementer comparison; start the optional sidecar first.

Export LITELLM_MASTER_KEY and SWITCHYARD_API_KEY. Run from the repository root.
This uses fake GitHub, a real performer, and the fixture's real pytest gate.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import runpy
from pathlib import Path

import yaml

from coordinare.bench import runner
from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.grader import score_run
from coordinare.bench.sweep import _real_point_config
from coordinare.config import CoordinareConfiguration


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="coordinare-performer:150-full")
    parser.add_argument("--seconds", type=float, default=600)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output must be a new directory")
    if not 0 < args.seconds <= 3600:
        parser.error("--seconds must be between zero and 3600")
    if not os.environ.get("SWITCHYARD_API_KEY"):
        parser.error("export SWITCHYARD_API_KEY before running the comparison")
    repo = Path(__file__).resolve().parents[2]
    factory = runpy.run_path(str(repo / "specs/161-board-bench-harness/live/bench_harness_live.py"))["configuration"]
    baseline_routing = repo / "specs/161-board-bench-harness/live/routing.yaml"
    base = factory(args.image, baseline_routing)
    combined = yaml.safe_load(baseline_routing.read_text())
    sidecar = yaml.safe_load((repo / "examples/switchyard/routing.yaml").read_text())
    combined["selfhosted_routing"] += sidecar["selfhosted_routing"]
    args.output.mkdir(parents=True)
    routing = args.output.resolve() / "routing.yaml"
    routing.write_text(yaml.safe_dump(combined))  # env names only, never resolved keys
    raw = base.model_dump()  # kept in memory; includes runtime credentials
    ep = raw["global_config"]["performer_endpoints"][0]
    ep["id"] = "bench204-implementer"
    ep["volumes"][0]["host_path"] = str(routing)
    raw["global_config"]["endpoints"].append({
        "name": "switchyard", "kind": "litellm",
        "base_url": "http://host.docker.internal:4008", "auth_env": "SWITCHYARD_API_KEY",
    })
    raw["global_config"]["model_endpoints"].append({
        "name": "sidecar", "endpoint": "switchyard", "model": "agent",
    })
    raw["global_config"]["modes"].append({
        "name": "sidecar", "strategy": "single", "tool": "sidecar",
    })
    # Isolate the persona under test, avoiding assessment/architecture spending
    # its entire exposure window. Both cells use this identical lifecycle.
    prior_sequence = runner._FULL_LIFECYCLE_SEQUENCE
    runner._FULL_LIFECYCLE_SEQUENCE = ["implementing"]
    try:
        for name in ("static", "sidecar"):
            raw["global_config"]["performers"]["implementer"]["mode"] = (
                "sidecar" if name == "sidecar" else "fixed"
            )
            cfg = CoordinareConfiguration(**raw)
            fixture = tiny_fixture()
            dest = args.output / name
            print(f"{name}: real implementer, {args.seconds}s budget", flush=True)
            result = await runner.run_board(
                [fixture], dest, stub=False, config=cfg,
                real_config=_real_point_config(cfg.global_config),
                wall_clock_budget_seconds=args.seconds,
            )
            score_run(result, [fixture]).write(dest)
            print(f"{name}: {result.totals.cards_merged} card(s) merged", flush=True)
    finally:
        runner._FULL_LIFECYCLE_SEQUENCE = prior_sequence


if __name__ == "__main__":
    asyncio.run(main())
