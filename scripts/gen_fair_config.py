#!/usr/bin/env python3
"""Generate a single-model "fair test" config from config.yaml (077).

For valid multi-backend benchmarking the backing MODEL must be held constant
across every backend — otherwise you're comparing models, not backends. This
rewrites every backend's model to one target, **preserving each field's route
prefix**: LiteLLM-routed backends keep the ``spark/`` prefix, Ollama-direct
backends get the bare name (the prefix must match the provider base URL).

Two model-resolution mechanisms are covered:
  1. ``performers.<role>.model`` — drives the dispatch payload (most backends).
  2. endpoint ``env.*_MODEL`` / ``*_MODEL_ID`` — backends that pin the model in
     env rather than reading the dispatch payload (junie, hermes).

The coordinare brain model (``COORDINARE_INFERENCE_MODEL``) is left untouched —
it's a ${VAR} placeholder and not under test here.

Usage: gen_fair_config.py --model gpt-oss:20b [--src config.yaml] [--out config.fair.yaml]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

# A concrete model string looks like "<family>:<tag>" for a known local family.
_MODEL_RE = re.compile(r"(gpt-oss|qwen|llama|deepseek|deepcoder|glm|mistral|laguna)", re.I)


def _swap(value: object, target: str) -> object:
    """Retarget a concrete model string to ``target``, preserving any spark/ prefix.

    Leaves ${VAR} placeholders and non-model strings unchanged.
    """
    if not isinstance(value, str) or value.startswith("${") or not _MODEL_RE.search(value):
        return value
    return f"spark/{target}" if value.startswith("spark/") else target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="target model, e.g. gpt-oss:20b")
    ap.add_argument("--src", default="config.yaml")
    ap.add_argument("--out", default="config.fair.yaml")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.src).read_text())
    changed: list[str] = []

    for role, rc in (cfg.get("performers") or {}).items():
        if isinstance(rc, dict) and "model" in rc:
            new = _swap(rc["model"], args.model)
            if new != rc["model"]:
                changed.append(f"performers.{role}.model: {rc['model']} -> {new}")
                rc["model"] = new

    for e in cfg.get("performer_endpoints", []) or []:
        env = e.get("env") or {}
        for k, v in list(env.items()):
            if k.upper().endswith(("MODEL", "MODEL_ID")):
                new = _swap(v, args.model)
                if new != v:
                    changed.append(f"{e['id']}.env.{k}: {v} -> {new}")
                    env[k] = new

    Path(args.out).write_text(
        yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False, width=200)
    )
    print(f"Wrote {args.out} targeting {args.model} ({len(changed)} overrides):")
    for c in changed:
        print("  -", c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
