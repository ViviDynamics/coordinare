#!/usr/bin/env python3
"""Functional spread (spec 122 US2): probe the LiteLLM gateway for each capability
the orchestrator's per-backend workarounds patched, across the models the fleet
routes. Wire-level (no containers) → a fast verdict on which shims/normalizers are
now obsolete after a LiteLLM/Ollama update.

Capability ↔ workaround it justifies:
  served            — reroute / Ollama-direct bypass (LiteLLM didn't serve the model)
  tool_calls        — harmony_tool_calls normalizer (gpt-oss leaked tool calls as text)
  reasoning_sep     — strip_reasoning normalizer (reasoning leaked into content)
  control_clean     — strip_control_chars normalizer (raw control bytes broke parsers)
  anthropic         — translate strategy (claude_code Anthropic wire → OpenAI)

A capability that now passes ⇒ its workaround is obsolete for that model.

Usage:  scripts/litellm_spread.py [--models m1,m2] [--base URL] [--timeout S]
Reads LITELLM_MASTER_KEY from .env. Run from repo root.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = "https://litellm.example"
# Raw control bytes excluding tab/newline/carriage-return.
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Models the fleet routes today (the migration candidates).
DEFAULT_MODELS = [
    "local/gpt-oss:120b",
    "local/gpt-oss:20b",
    "local/qwen3-coder:30b",
    "local/glm-4.7-flash:latest",
    "local/qwen2.5-coder:14b-instruct-q6_K",
    "local/qwen3.6:35b",
]


def _master_key() -> str:
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def _chat(base: str, hdr: dict, model: str, timeout: float, *, tools: bool = False) -> dict:
    body: dict = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": "Use the get_weather tool for Paris." if tools else "Reply with exactly: HELLO",
        }],
        "max_tokens": 400,
    }
    if tools:
        body["tools"] = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }]
        body["tool_choice"] = "auto"
    try:
        return httpx.post(f"{base}/v1/chat/completions", headers=hdr, json=body, timeout=timeout).json()
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


def _messages(base: str, hdr: dict, model: str, timeout: float) -> dict:
    try:
        return httpx.post(
            f"{base}/v1/messages",
            headers={**hdr, "anthropic-version": "2023-06-01"},
            json={"model": model, "max_tokens": 80, "messages": [{"role": "user", "content": "say OK"}]},
            timeout=timeout,
        ).json()
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


def probe(base: str, hdr: dict, model: str, timeout: float) -> dict:
    out = {"model": model, "served": False, "tool_calls": None, "reasoning_sep": None,
           "control_clean": None, "anthropic": None, "note": ""}
    d = _chat(base, hdr, model, timeout)
    if "_error" in d:
        out["note"] = d["_error"][:90]
        return out
    if "choices" not in d:
        out["note"] = str(d.get("error", d))[:90]
        return out
    msg = (d["choices"][0] or {}).get("message", {}) or {}
    content = msg.get("content") or ""
    out["served"] = bool(content) or bool(msg.get("reasoning_content"))
    out["reasoning_sep"] = bool(msg.get("reasoning_content"))
    out["control_clean"] = CTRL.search(content) is None

    dt = _chat(base, hdr, model, timeout, tools=True)
    if "choices" in dt:
        tmsg = (dt["choices"][0] or {}).get("message", {}) or {}
        tc = tmsg.get("tool_calls")
        out["tool_calls"] = bool(tc)
        if tc:
            out["note"] = f"tc={tc[0].get('function', {}).get('name')}"
        elif (tmsg.get("content") or "").strip():
            out["note"] = "tool-call LEAKS into content"

    # The Anthropic front door only matters for the claude_code-tier model.
    if "gpt-oss:120b" in model:
        dm = _messages(base, hdr, model, timeout)
        out["anthropic"] = dm.get("type") == "message" or bool(dm.get("content"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", help="comma-separated model ids; default the routed set")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--timeout", type=float, default=240.0, help="per-request seconds (gpt-oss:120b is slow)")
    args = ap.parse_args()

    key = _master_key()
    if not key:
        print("LITELLM_MASTER_KEY not found in .env")
        return 2
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    models = args.models.split(",") if args.models else DEFAULT_MODELS

    print(f"Functional spread vs {args.base}\n")
    rows = [probe(args.base, hdr, m, args.timeout) for m in models]

    def cell(v: object) -> str:
        return "yes" if v is True else ("NO" if v is False else "-")

    print(f"{'MODEL':<42}{'SERVED':<8}{'TOOLS':<7}{'REASON':<8}{'CTRL':<7}{'ANTHRO':<8}NOTE")
    print("-" * 100)
    for r in rows:
        print(f"{r['model']:<42}{cell(r['served']):<8}{cell(r['tool_calls']):<7}"
              f"{cell(r['reasoning_sep']):<8}{cell(r['control_clean']):<7}"
              f"{cell(r['anthropic']):<8}{r['note'][:34]}")

    (REPO_ROOT / "tmp").mkdir(exist_ok=True)
    (REPO_ROOT / "tmp" / "litellm_spread.json").write_text(json.dumps(rows, indent=2))
    print("\nartifact: tmp/litellm_spread.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
