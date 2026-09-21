"""LiteLLM binding for the scenario eval (spec 164).

Eval-only. Production model traffic goes through the performer's own transport;
this exists so the eval can drive the real QA workflow against the real gateway
from the host, without a container.

All traffic goes through the LiteLLM gateway — the standing rule for this
project — and never directly at a model host.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import httpx
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.qa.dom import read_dom
from performer.workflows.toolkit import Toolkit

_GIT = shutil.which("git") or "git"  # 440: resolve the real git path once; fallback preserves prior behavior

if TYPE_CHECKING:
    from pathlib import Path


def _base_url() -> str:
    url = (os.getenv("LITELLM_BASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "LITELLM_BASE_URL is unset. Run `set -a && source .env && set +a` "
            "first: config placeholders expand at load time and silently become "
            "empty strings otherwise.",
        )
    return url.rstrip("/")


def _model() -> str:
    """The model the eval scores against.

    Read from the environment with no default. Spec 145 forbids internal model
    identifiers in shipped source, and a hardcoded fallback would also silently
    score a different model than the deployment uses -- which for a measurement
    harness is worse than failing.
    """
    model = (os.getenv("COORDINARE_INFERENCE_MODEL") or "").strip()
    if not model:
        raise RuntimeError(
            "COORDINARE_INFERENCE_MODEL is unset. Run "
            "`set -a && source .env && set +a` before the eval: config "
            "placeholders expand at load time and silently become empty strings.",
        )
    return model


async def _call_model(persona: str, content: list[dict], max_tokens: int) -> ModelReply:
    key = os.getenv("LITELLM_MASTER_KEY", "")
    body = {
        "model": _model(),
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": persona},
                     {"role": "user", "content": content}],
    }
    # 900s read: the same ceiling the performer HTTP path uses (#264); a large
    # blueprint on a slow self-hosted model can take minutes to stream.
    async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=30)) as client:
        resp = await client.post(
            f"{_base_url()}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            content=json.dumps(body),
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]
    return ModelReply(
        content=choice["message"].get("content") or "",
        finish_reason=choice.get("finish_reason"),
        reasoning_content=choice["message"].get("reasoning_content"),
    )


async def _run_command(cmd: str, cwd, timeout_s: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout_s}s"
    return proc.returncode or 0, (out or b"").decode(errors="replace")


def gateway_toolkit(repo: Path) -> Toolkit:
    """A Toolkit bound to the live gateway and the local filesystem."""
    return Toolkit(
        metrics=WorkflowMetrics(),
        model_call=_call_model,
        command_runner=_run_command,
        screenshot_capture=lambda **_: None,
        dom_reader=read_dom,
    )


def git_available() -> bool:
    return subprocess.run([_GIT, "--version"], capture_output=True, timeout=30).returncode == 0
