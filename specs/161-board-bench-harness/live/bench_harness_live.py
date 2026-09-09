#!/usr/bin/env python
"""Opt-in fixed-model harness sweep against fake GitHub and a LiteLLM gateway.

Export LITELLM_MASTER_KEY, then run this script from the repository root. No live
board/repository is used and deployment configuration is never changed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from coordinare.bench.space import LoadedSpace, load_space
from coordinare.bench.sweep import run_sweep
from coordinare.config import CoordinareConfiguration


def configuration(image: str, routing: Path) -> CoordinareConfiguration:
    key = os.environ.get("LITELLM_MASTER_KEY")
    if not key:
        raise ValueError("export LITELLM_MASTER_KEY before running the paid live sweep")
    gateway = "https://litellm.vividynamics.com"
    model = "spark/glm-5.3-flash"
    roles = ["assessor", "architect", "implementer", "reviewer", "security", "qa", "tech_writer", "closer"]
    # One shared baseline endpoint is split by _real_point_config as roles vary.
    env = {
        "BACKEND": "claude_code", "ALLOW_INSECURE_REPO_URL": "1", "ALLOW_HOST_GATEWAY_GITHUB": "1",
        "SELFHOSTED_ROUTING_CONFIG": "/etc/coordinare/routing.yaml",
        "COORDINARE_PROXY_AUTH": key, "LITELLM_PROXY_AUTH_TOKEN": key,
        "LITELLM_PROXY_BASE_URL": gateway, "OPENAI_API_KEY": key,
        "CODEX_PROVIDER_BASE_URL": gateway + "/v1",
        "OPENCODE_PROVIDER_BASE_URL": gateway + "/v1", "OPENCLAW_PROVIDER_BASE_URL": gateway + "/v1",
        "JUNIE_PROVIDER_BASE_URL": gateway + "/v1/chat/completions",
        "JUNIE_PROVIDER_MODEL_ID": "bench", "JUNIE_PROVIDER_MODEL": model,
        "COORDINARE_INFERENCE_MAX_TOKENS": "4096",
    }
    return CoordinareConfiguration(**{
        "global_config": {
            "github_org": "bench-org", "project_name": "bench-repo", "github_project_number": 1,
            "github_token": "fake-token", "human_reviewers": ["reviewer1"],
            "endpoints": [{"name": "bench-gateway", "kind": "openai", "auth_env": "LITELLM_MASTER_KEY"}],
            "model_endpoints": [{"name": "fixed", "endpoint": "bench-gateway", "model": model}],
            "modes": [{"name": "fixed", "strategy": "single", "tool": "fixed"}],
            "performers": {r: {"backend": "claude_code", "mode": "fixed", "max_tokens": 4096} for r in roles},
            "performer_endpoints": [{
                "id": "harness-live", "mode": "ephemeral", "roles": roles, "image": image,
                "extra_hosts": ["host.docker.internal:host-gateway"], "env": env,
                "volumes": [{"host_path": str(routing.resolve()), "container_path": "/etc/coordinare/routing.yaml", "mode": "ro"}],
            }],
        },
        "symphonies": [{"name": "bench", "github_project_number": 1, "persona_scope": {}}],
    })


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seconds", type=float, default=600, help="hard wall-clock limit per run")
    parser.add_argument("--image", default="coordinare-performer:full")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.output.exists():
        parser.error("--output must be a new directory so earlier evidence is never overwritten")
    shipped = load_space("benchmarks/spaces/harness.yaml")
    cfg = configuration(args.image, Path("specs/161-board-bench-harness/live/routing.yaml"))
    # Python-mode preserves SecretStr inputs; no resolved configuration is written.
    loaded = LoadedSpace(shipped.definition, cfg, cfg.model_dump(), shipped.source_path)
    print(f"8 declared points, {args.repeats} repeat(s), {args.seconds}s/run; fixed GLM via LiteLLM", flush=True)
    result = await run_sweep(
        loaded, "ablation", args.output, stub=False, repeats=args.repeats,
        repeats_source="operator", wall_clock_budget_seconds=args.seconds,
        repeat_notes=["Live baseline uses Claude for all roles; original harness choices unchanged.",
                      "Budget exhaustion and missing role observations are evidence limits, never wins."],
    )
    print(result.render_markdown())


if __name__ == "__main__":
    asyncio.run(main())
