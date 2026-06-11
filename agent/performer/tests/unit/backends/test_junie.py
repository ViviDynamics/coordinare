"""Unit tests for the Junie (native CLI) and opencode_compat backends."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from performer.backends.junie import (
    JunieBackend,
    _build_task_prompt as _junie_build_task_prompt,
    _maybe_write_custom_profile,
)
from performer.backends.opencode_compat import (
    OpenCodeCompatAdapter,
    _build_task_prompt as _compat_build_task_prompt,
)
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
        # The local profile id (filename / --model custom:<id>) comes from MODEL_ID.
        assert profile_id == "vivi"

        profile_path = Path(tmp_path) / "models" / "vivi.json"
        assert profile_path.exists()
        data = json.loads(profile_path.read_text())
        # The `id` FIELD is the WIRE model name (JUNIE_PROVIDER_MODEL), per Junie's
        # schema — NOT the local profile id. Junie sends it verbatim as `model`.
        assert data["id"] == "gpt-4o-mini"
        assert data["baseUrl"] == "https://litellm.example/v1"
        assert data["apiType"] == "OpenAICompletion"
        assert "model" not in data  # not part of Junie's schema; id carries the wire model
        assert data["apiKey"] == "sk-abc"

    def test_custom_profile_id_field_falls_back_to_profile_id_when_no_wire_model(
        self, monkeypatch, tmp_path
    ) -> None:
        """When JUNIE_PROVIDER_MODEL is unset, the id field falls back to the
        profile id (best-effort) rather than being empty."""
        monkeypatch.setenv("JUNIE_HOME", str(tmp_path))
        monkeypatch.setenv("JUNIE_PROVIDER_BASE_URL", "https://litellm.example/v1/chat/completions")
        monkeypatch.setenv("JUNIE_PROVIDER_MODEL_ID", "vivi")
        monkeypatch.delenv("JUNIE_PROVIDER_MODEL", raising=False)
        assert _maybe_write_custom_profile() == "vivi"
        data = json.loads((Path(tmp_path) / "models" / "vivi.json").read_text())
        assert data["id"] == "vivi"

    def test_custom_profile_falls_back_to_writable_home_when_source_readonly(
        self, monkeypatch, tmp_path
    ) -> None:
        """077 live-fix: a read-only JUNIE_HOME (the ~/.junie ro creds mount) must
        NOT crash dispatch with EROFS. The profile is written to a writable
        job-scoped home seeded from the creds, and JUNIE_HOME is re-exported."""
        ro_home = tmp_path / "ro_junie"
        models = ro_home / "models"
        models.mkdir(parents=True)  # pre-existing models dir (as a warm creds mount)
        (ro_home / "auth.token").write_text("license-token")  # a creds file to carry over
        models.chmod(0o500)  # existing models dir read-only → write_text raises OSError
        ro_home.chmod(0o500)  # read + execute, no write
        monkeypatch.setenv("JUNIE_HOME", str(ro_home))
        monkeypatch.setenv("JUNIE_PROVIDER_BASE_URL", "https://litellm.example/v1")
        monkeypatch.setenv("JUNIE_PROVIDER_MODEL_ID", "vivi")
        try:
            profile_id = _maybe_write_custom_profile()
            assert profile_id == "vivi"
            # JUNIE_HOME was relocated to a writable dir (not the read-only mount).
            new_home = Path(os.environ["JUNIE_HOME"])
            assert new_home != ro_home
            assert (new_home / "models" / "vivi.json").exists()
            # Creds were seeded into the writable home so junie stays authenticated.
            assert (new_home / "auth.token").read_text() == "license-token"
        finally:
            ro_home.chmod(0o700)  # restore so pytest can clean tmp_path
            (ro_home / "models").chmod(0o700)


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


class TestJunieLaunchEnv:
    @pytest.mark.asyncio
    async def test_launch_appends_cache_path_after_image_path(self, tmp_path) -> None:
        """088 B1: shared env policy — junie is a Node CLI, so launching it
        under the env-cache's project-pinned node (e.g. 18.12.1) crashes at
        startup. The CLI must launch on the IMAGE's node, but the cache
        toolchain dirs must stay REACHABLE: image PATH first, cache dirs
        appended deduplicated. Every other cache var must survive.
        """
        backend = JunieBackend()
        backend._stand = Stand(path=tmp_path, branch="main")
        backend._stand.cache_env = {
            "PATH": "/devenv/foo/node-v18.12.1/bin:/usr/bin:/bin",
            "RBENV_ROOT": "/devenv/foo/rbenv",
        }
        backend._cache_env = backend._stand.cache_env
        backend._git_env = {"GIT_AUTHOR_NAME": "performer"}
        backend._score = Score(title="t", repo_url="https://github.com/o/r", branch="main")
        backend._original_prompt = "prompt"

        captured: dict = {}

        async def _fake_exec(*argv, **kwargs):
            captured["env"] = kwargs.get("env")
            proc = AsyncMock()
            proc.pid = 4321
            proc.returncode = 0
            return proc

        with patch(
            "performer.backends.junie.asyncio.create_subprocess_exec",
            _fake_exec,
        ), patch(
            "performer.backends.junie._maybe_write_custom_profile",
            return_value=None,
        ), patch.object(
            JunieBackend, "_wait_and_parse", AsyncMock(return_value=None)
        ):
            await backend._launch("prompt")

        env = captured["env"]
        image_path = os.environ["PATH"]
        # Image dirs FIRST — the CLI's interpreter resolves to the image's node.
        assert env["PATH"].startswith(image_path)
        # Cache toolchain dirs APPENDED — reachable, never shadowing the image.
        assert env["PATH"].index(image_path) < env["PATH"].index("node-v18.12.1")
        assert env.get("RBENV_ROOT") == "/devenv/foo/rbenv"
        assert env["GIT_AUTHOR_NAME"] == "performer"


class TestOpenCodeCompatStartEnv:
    @pytest.mark.asyncio
    async def test_start_appends_cache_path_after_image_path(self, tmp_path) -> None:
        """088 B1: shared env policy — the opencode_compat CLI must launch on
        the IMAGE's node, not the env-cache's project-pinned node (e.g.
        18.12.1, which crashes modern Node CLIs at startup), but the cache
        toolchain dirs must stay REACHABLE: image PATH first, cache dirs
        appended deduplicated. Every other cache var must survive.
        """
        adapter = OpenCodeCompatAdapter(base_url="https://litellm.example/v1", api_key="sk")
        stand = Stand(
            path=tmp_path,
            branch="main",
            git_env={"GIT_AUTHOR_NAME": "performer"},
            cache_env={"PATH": "/devenv/foo/node-v18.12.1/bin:/usr/bin", "VIRTUAL_ENV": "/devenv/foo/.venv"},
        )
        score = Score(title="t", repo_url="https://github.com/o/r", branch="main", github_token="tok")

        proc = AsyncMock()
        # Short-circuit after launch so we only exercise env construction.
        with patch(
            "performer.backends.opencode_compat.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ) as mock_exec, patch(
            "performer.backends.opencode_compat._find_free_port", return_value=12345
        ), patch.object(
            OpenCodeCompatAdapter, "_wait_for_ready", AsyncMock(side_effect=RuntimeError("stop"))
        ), patch.object(
            OpenCodeCompatAdapter, "_drain_logs", AsyncMock(return_value=None)
        ):
            with pytest.raises(RuntimeError):
                await adapter.start(stand, score)

        env = mock_exec.call_args[1]["env"]
        image_path = os.environ["PATH"]
        # Image dirs FIRST — the CLI's interpreter resolves to the image's node.
        assert env["PATH"].startswith(image_path)
        # Cache toolchain dirs APPENDED — reachable, never shadowing the image.
        assert env["PATH"].index(image_path) < env["PATH"].index("node-v18.12.1")
        assert env["VIRTUAL_ENV"] == "/devenv/foo/.venv"
        assert env["GIT_AUTHOR_NAME"] == "performer"
        # compat env still pinned
        assert env["OPENAI_BASE_URL"] == "https://litellm.example/v1"


# ---------------------------------------------------------------------------
# FR-018 regression guards: prompt-body persona wiring is retained for
# backends with no job-isolated native persona slot.
# ---------------------------------------------------------------------------


def _persona_score(marker: str) -> Score:
    return Score(
        title="T",
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
        persona_instructions=marker,
    )


class TestProsonaPromptBodyRegression:
    def test_junie_keeps_persona_in_prompt_body(self) -> None:
        """Junie has no job-isolated persona slot — persona stays in the prompt."""
        prompt = _junie_build_task_prompt(_persona_score("PERSONA_MARKER_JUNIE"))
        assert "PERSONA_MARKER_JUNIE" in prompt
        assert "## Role Instructions" in prompt

    def test_opencode_compat_keeps_persona_in_prompt_body(self) -> None:
        """opencode_compat has no job-isolated persona slot — persona stays in the prompt."""
        prompt = _compat_build_task_prompt(_persona_score("PERSONA_MARKER_COMPAT"))
        assert "PERSONA_MARKER_COMPAT" in prompt
        assert "## Role Instructions" in prompt


# ---------------------------------------------------------------------------
# ## Card Documentation section threads through both backends
# ---------------------------------------------------------------------------


def _doc_score() -> Score:
    return Score(
        title="T",
        issue_number=70,
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
    )


class TestCardDocsSection:
    def test_junie_emits_card_docs_section(self, tmp_path: Path) -> None:
        (tmp_path / "docs" / "cards" / "70-t").mkdir(parents=True)
        prompt = _junie_build_task_prompt(_doc_score(), stand_path=tmp_path)
        assert "## Card Documentation" in prompt
        assert "docs/cards/70-t/" in prompt

    def test_junie_omits_section_when_folder_missing(self, tmp_path: Path) -> None:
        prompt = _junie_build_task_prompt(_doc_score(), stand_path=tmp_path)
        assert "## Card Documentation" not in prompt

    def test_opencode_compat_emits_card_docs_section(self, tmp_path: Path) -> None:
        (tmp_path / "docs" / "cards" / "70-t").mkdir(parents=True)
        prompt = _compat_build_task_prompt(_doc_score(), stand_path=tmp_path)
        assert "## Card Documentation" in prompt
        assert "docs/cards/70-t/" in prompt

    def test_opencode_compat_omits_section_when_folder_missing(self, tmp_path: Path) -> None:
        prompt = _compat_build_task_prompt(_doc_score(), stand_path=tmp_path)
        assert "## Card Documentation" not in prompt
