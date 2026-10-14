"""223: a format retry restarts the backend on the same instance, so start()
must clear the previous run's terminal status, and the repair prompt must not
read as a request to change code."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from performer.backends.base import BackendStatus
from performer.backends._relay_feedback import relay_feedback_prompt_section
from performer.backends.claude_code import ClaudeCodeBackend
from performer.backends.driver import DriverBackend
from performer.backends.junie import JunieBackend
from performer.models import Score, Stand


def _stand(tmp_path: Path) -> Stand:
    stand = Stand(path=tmp_path, branch="feat/test")
    stand.git_env = {}
    return stand


def _score() -> Score:
    return Score(title="Format phone", repo_url="https://github.com/acme/repo", branch="feat/test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "launcher"),
    [(ClaudeCodeBackend, "_launch"), (JunieBackend, "_launch"), (DriverBackend, "_spawn")],
)
async def test_start_clears_the_previous_runs_done_status(factory, launcher, tmp_path: Path) -> None:
    backend = factory()
    setattr(backend, launcher, AsyncMock())
    backend._status = BackendStatus(state="done", output="sufficient\": true}")
    await backend.start(_stand(tmp_path), _score())
    status = backend.get_status()
    assert status.state == "working"
    assert status.output is None


def test_format_repair_renders_apart_from_code_feedback() -> None:
    parts = relay_feedback_prompt_section([
        {"body": "Rename the helper", "author_login": "octocat"},
        {"body": "Return ONLY a valid JSON object now.", "author_login": "performer", "kind": "format_repair"},
    ])
    text = "\n".join(parts)
    assert "## Output Format Correction" in text
    feedback, repair = text.split("## Output Format Correction")
    assert "Rename the helper" in feedback
    assert "Return ONLY a valid JSON object now." in repair
    assert "Return ONLY" not in feedback
    assert "author: performer" not in text


def test_format_repair_alone_skips_the_fix_them_all_section() -> None:
    text = "\n".join(relay_feedback_prompt_section([
        {"body": "Return ONLY a valid JSON object now.", "kind": "format_repair"},
    ]))
    assert "## Output Format Correction" in text
    assert "Relayed Feedback" not in text
    assert "fix them all" not in text
