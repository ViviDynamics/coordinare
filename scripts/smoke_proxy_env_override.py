"""Smoke test for 073 per-job env override fix.

Constructs ClaudeCodeBackend with boot-time proxy settings, then simulates
_perform_job's per-job os.environ injection and asserts the per-job value
wins in the subprocess env that would be passed to the claude CLI.

Run from repo root: PYTHONPATH=agent/performer/src .venv/bin/python scripts/smoke_proxy_env_override.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


async def main() -> int:
    # Boot-time proxy defaults (as if coordinare set LITELLM_PROXY_* in container env)
    os.environ["LITELLM_PROXY_BASE_URL"] = "https://proxy.example.com"
    os.environ["LITELLM_PROXY_AUTH_TOKEN"] = "BOOT_DEFAULT_TOKEN"

    from performer.backends.claude_code import ClaudeCodeBackend
    from performer.config import get_settings
    from performer.models import Score, Stand

    get_settings.cache_clear()
    adapter = ClaudeCodeBackend()
    print(f"  backend _proxy_env = {{'ANTHROPIC_BASE_URL': '{adapter._proxy_env.get('ANTHROPIC_BASE_URL')}', "
          f"'ANTHROPIC_AUTH_TOKEN': '{adapter._proxy_env.get('ANTHROPIC_AUTH_TOKEN')}'}}")

    # Simulate _perform_job injecting per-job secret from JobInitPayload
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "PER_JOB_OVERRIDE_TOKEN"

    score = Score(
        title="smoke",
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
    )
    stand = Stand(path=Path("/tmp"), branch="main")

    proc = MagicMock()
    proc.pid = 1
    proc.returncode = None
    proc.stdout = MagicMock()

    async def _empty():
        return
        yield  # pragma: no cover

    proc.stdout.__aiter__ = lambda self: _empty()
    proc.stderr = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    with patch(
        "performer.backends.claude_code.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ) as mock_exec:
        await adapter.start(stand, score)

    env = mock_exec.call_args[1]["env"]
    base = env.get("ANTHROPIC_BASE_URL")
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    print(f"  subprocess env ANTHROPIC_BASE_URL  = {base}")
    print(f"  subprocess env ANTHROPIC_AUTH_TOKEN = {token}")

    ok = base == "https://proxy.example.com" and token == "PER_JOB_OVERRIDE_TOKEN"
    print(f"\n  result: {'PASS' if ok else 'FAIL'} — per-job token {'wins' if token == 'PER_JOB_OVERRIDE_TOKEN' else 'CLOBBERED by boot default'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
