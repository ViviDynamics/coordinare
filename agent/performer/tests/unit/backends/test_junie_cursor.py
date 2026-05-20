"""Unit tests for Junie (native CLI) and Cursor (OpenCode-compatible) backends."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from performer.backends.cursor import CursorBackend
from performer.backends.junie import JunieBackend, _maybe_write_custom_profile
from performer.models import Score, Stand


class TestJunieBackend:
    def test_default_executable(self) -> None:
        adapter = JunieBackend()
        assert adapter._executable == "junie"

    def test_env_override_executable(self, monkeypatch) -> None:
        monkeypatch.setenv("JUNIE_EXECUTABLE", "junie-cli")
        adapter = JunieBackend()
        assert adapter._executable == "junie-cli"

    def test_custom_profile_skipped_when_no_base_url(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("JUNIE_PROVIDER_BASE_URL", raising=False)
        monkeypatch.setenv("JUNIE_HOME", str(tmp_path))
        assert _maybe_write_custom_profile() is None
        assert not (tmp_path / "models").exists()

    @pytest.mark.parametrize(
        "bad_id",
        ["../evil", "a/b", "foo.bar", "name with space", "id;rm -rf /", ".."],
    )
    def test_custom_profile_rejects_unsafe_id(self, monkeypatch, tmp_path, bad_id) -> None:
        monkeypatch.setenv("JUNIE_HOME", str(tmp_path))
        monkeypatch.setenv("JUNIE_PROVIDER_BASE_URL", "https://litellm.example/v1")
        monkeypatch.setenv("JUNIE_PROVIDER_MODEL_ID", bad_id)
        with pytest.raises(ValueError, match="unsafe JUNIE_PROVIDER_MODEL_ID"):
            _maybe_write_custom_profile()

    def test_custom_profile_written_from_env(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("JUNIE_HOME", str(tmp_path))
        monkeypatch.setenv("JUNIE_PROVIDER_BASE_URL", "https://litellm.example/v1")
        monkeypatch.setenv("JUNIE_PROVIDER_MODEL_ID", "vivi")
        monkeypatch.setenv("JUNIE_PROVIDER_API_TYPE", "OpenAICompletion")
        monkeypatch.setenv("JUNIE_PROVIDER_MODEL", "gpt-4o-mini")
        monkeypatch.setenv("JUNIE_PROVIDER_API_KEY_ENV", "LITELLM_MASTER_KEY")
        monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-abc")

        profile_id = _maybe_write_custom_profile()
        assert profile_id == "vivi"

        profile_path = Path(tmp_path) / "models" / "vivi.json"
        assert profile_path.exists()
        data = json.loads(profile_path.read_text())
        assert data["id"] == "vivi"
        assert data["baseUrl"] == "https://litellm.example/v1"
        assert data["apiType"] == "OpenAICompletion"
        assert data["model"] == "gpt-4o-mini"
        assert data["apiKey"] == "sk-abc"


class TestJunieLaunchFailureCleanup:
    @pytest.mark.asyncio
    async def test_temp_json_unlinked_when_subprocess_exec_raises(self, tmp_path) -> None:
        backend = JunieBackend()
        backend._stand = Stand(path=tmp_path, branch="main")
        backend._score = Score(title="t", repo_url="https://github.com/o/r", branch="main")
        backend._original_prompt = "prompt"

        with patch(
            "performer.backends.junie.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=OSError("boom")),
        ):
            with pytest.raises(OSError):
                await backend._launch("prompt")

        # The json_output tempfile should not leak to /tmp after a failed launch.
        assert backend._json_output_path is None


class TestCursorBackend:
    def test_default_executable(self) -> None:
        adapter = CursorBackend()
        assert adapter._executable == "cursor"
        assert adapter._adapter_name == "cursor"

    def test_env_override_executable(self, monkeypatch) -> None:
        monkeypatch.setenv("CURSOR_EXECUTABLE", "cursor-cli")
        adapter = CursorBackend()
        assert adapter._executable == "cursor-cli"
