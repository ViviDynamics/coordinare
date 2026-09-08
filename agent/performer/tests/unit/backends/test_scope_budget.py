"""#273: the configured CLI hook enforces admission, including parallel calls."""
from __future__ import annotations

import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from performer.backends.claude_code import ClaudeCodeBackend
from performer.models import Score, Stand
from tests.unit.backends.test_claude_code import _fake_proc


@pytest.mark.asyncio
async def test_claude_scope_cap_reaches_cli_and_blocks_excess_calls(tmp_path):
    backend = ClaudeCodeBackend()
    score = Score(title="Skim review", repo_url="https://github.com/o/r", branch="review", role="reviewing",
                  max_tool_calls=2, scope_addon="Skim validation")
    proc = _fake_proc()
    proc.returncode = 0
    with patch("performer.backends.claude_code.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as launch:
        await backend.start(Stand(path=tmp_path, branch="review"), score)
    argv = launch.call_args.args
    settings = json.loads(argv[argv.index("--settings") + 1])
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert b"Skim validation" in proc.stdin.write.call_args.args[0]

    def invoke(_):
        result = subprocess.run(shlex.split(hook["command"]), input='{"tool_name":"Read"}', capture_output=True, text=True, check=True)
        return json.loads(result.stdout) if result.stdout else None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(invoke, range(6)))
    assert results.count(None) == 2
    for denied in filter(None, results):
        assert denied["continue"] is False
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert backend.get_status().state == "error"
    assert "TOOL_BUDGET_EXHAUSTED" in backend.get_status().error_reason
    await backend.stop()
    backend._tool_budget_dir.cleanup()


@pytest.mark.asyncio
async def test_no_cap_leaves_cli_settings_unchanged(tmp_path):
    backend = ClaudeCodeBackend()
    proc = _fake_proc()
    proc.returncode = 0
    with patch("performer.backends.claude_code.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as launch:
        await backend.start(Stand(path=tmp_path, branch="review"), Score(title="Review", repo_url="https://github.com/o/r", branch="review"))
    assert "--settings" not in launch.call_args.args
    await backend.stop()
