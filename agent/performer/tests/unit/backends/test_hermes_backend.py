"""Unit tests for HermesBackend — one-shot CLI Hermes adapter (spec 068)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends import UnsupportedBackendError, get_backend
from performer.backends.base import BackendAdapter, BackendStatus
from performer.backends.hermes import (
    ALLOWED_TOOLSETS,
    DISABLED_TOOLSETS,
    SUPPORTED_ROLES,
    HermesBackend,
    _build_task_prompt,
    _extract_json_object,
)
from performer.models import BackendEvent, BackendEventType, Score, Stand


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _score(**kwargs) -> Score:
    defaults = dict(
        title="Test Task",
        description="A short description.",
        acceptance_criteria=["Criterion A", "Criterion B"],
        repo_url="https://github.com/org/repo",
        branch="feature/x",
        github_token="tok",
        persona_instructions="PERSONA_MARKER act as a careful coder.",
        role="implementer",
    )
    defaults.update(kwargs)
    return Score(**defaults)


def _stand(tmp_path: Path) -> Stand:
    return Stand(path=tmp_path, branch="feature/x")


def _fake_proc(pid: int = 4242, returncode: int | None = 0,
               stdout_b: bytes = b"", stderr_b: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout_b, stderr_b))
    proc.wait = AsyncMock(return_value=returncode if returncode is not None else 0)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


@pytest.fixture
def hermes_env(monkeypatch):
    """Provide the three required Hermes env vars."""
    monkeypatch.setenv("HERMES_PROVIDER", "openai")
    monkeypatch.setenv("HERMES_API_KEY", "sk-test-12345")
    monkeypatch.setenv("HERMES_MODEL", "gpt-4o")
    monkeypatch.delenv("HERMES_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    return {
        "HERMES_PROVIDER": "openai",
        "HERMES_API_KEY": "sk-test-12345",
        "HERMES_MODEL": "gpt-4o",
    }


# ---------------------------------------------------------------------------
# US1: factory + start basics
# ---------------------------------------------------------------------------


class TestUS1FactoryAndStart:
    def test_backend_factory_registers_hermes(self) -> None:
        adapter = get_backend("hermes")
        assert isinstance(adapter, HermesBackend)
        assert isinstance(adapter, BackendAdapter)

    def test_unknown_backend_message_includes_hermes(self) -> None:
        with pytest.raises(UnsupportedBackendError) as exc:
            get_backend("does-not-exist")
        assert "hermes" in str(exc.value)

    async def test_start_spawns_hermes_cli_with_isolated_home(
        self, tmp_path: Path, hermes_env
    ) -> None:
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        try:
            mock_exec.assert_awaited_once()
            args = list(mock_exec.call_args[0])
            # Required CLI flags.
            assert "chat" in args
            assert "-q" in args
            assert "--quiet" in args
            assert "--toolsets" in args
            ts_idx = args.index("--toolsets")
            assert args[ts_idx + 1] == ",".join(ALLOWED_TOOLSETS)
            # Env carries an isolated HERMES_HOME.
            env = mock_exec.call_args[1]["env"]
            assert "HERMES_HOME" in env
            assert env["HERMES_HOME"].rsplit("/", 1)[-1].startswith("hermes-job-")
            assert env["HERMES_HOME"] != str(Path.home() / ".hermes")
        finally:
            await adapter.stop()

    async def test_start_fails_fast_on_missing_env(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("HERMES_PROVIDER", "openai")
        monkeypatch.delenv("HERMES_API_KEY", raising=False)
        monkeypatch.setenv("HERMES_MODEL", "gpt-4o")
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        mock_exec.assert_not_awaited()
        status = adapter.get_status()
        assert status.state == "error"
        assert status.error_reason == "missing_env:HERMES_API_KEY"
        # _finalize ran: profile dir cleared.
        assert adapter._profile_dir is None

    async def test_persona_written_to_soul_md_and_card_context_in_prompt(
        self, tmp_path: Path, hermes_env
    ) -> None:
        """FR-015: persona lands in $HERMES_HOME/SOUL.md, not the prompt body."""
        proc = _fake_proc(returncode=None)
        score = _score(
            persona_instructions="PERSONA_MARKER_XYZ — be careful.",
            acceptance_criteria=["AC_FIRST_TOKEN", "AC_SECOND_TOKEN"],
            clarifications=[{"questions": ["Q_TOKEN?"], "answer": "A_TOKEN"}],
        )
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), score)
        try:
            args = list(mock_exec.call_args[0])
            q_idx = args.index("-q")
            prompt = args[q_idx + 1]
            # Persona is NOT in the prompt body — it's in SOUL.md.
            assert "PERSONA_MARKER_XYZ" not in prompt
            assert "## Role Instructions" not in prompt
            soul = adapter._profile_dir / "SOUL.md"
            assert soul.exists()
            assert "PERSONA_MARKER_XYZ" in soul.read_text()
            # Card context still flows through the prompt.
            assert "AC_FIRST_TOKEN" in prompt
            assert "AC_SECOND_TOKEN" in prompt
            assert prompt.index("AC_FIRST_TOKEN") < prompt.index("AC_SECOND_TOKEN")
            assert "Q_TOKEN?" in prompt
            assert "A_TOKEN" in prompt
        finally:
            await adapter.stop()

    async def test_no_soul_md_written_when_persona_empty(
        self, tmp_path: Path, hermes_env
    ) -> None:
        """FR-015: empty persona leaves SOUL.md absent (hermes uses its own starter)."""
        proc = _fake_proc(returncode=None)
        score = _score(persona_instructions="")
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), score)
        try:
            assert not (adapter._profile_dir / "SOUL.md").exists()
        finally:
            await adapter.stop()

    @pytest.mark.parametrize("role", list(SUPPORTED_ROLES))
    def test_role_parity_prompt_contains_role_block(self, role: str) -> None:
        score = _score(role=role)
        prompt = _build_task_prompt(score, [])
        # Each role gets an output-requirements block referencing the role.
        assert f"Role Output Requirements ({role})" in prompt

    def test_architecture_plan_inlined_when_file_present(
        self, tmp_path: Path
    ) -> None:
        plan = tmp_path / "specs" / "099-test" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("PLAN_BODY_MARKER\n\nstep 1\nstep 2\n")
        score = _score(architecture_plan_path="specs/099-test/plan.md")
        prompt = _build_task_prompt(score, [], stand_path=tmp_path)
        assert "PLAN_BODY_MARKER" in prompt
        assert "## Architecture Plan" in prompt
        # Falls back to "See `<path>`" wording only when read fails.
        assert "See `specs/099-test/plan.md` on this branch." not in prompt

    def test_architecture_plan_falls_back_to_path_when_unreadable(
        self, tmp_path: Path
    ) -> None:
        score = _score(architecture_plan_path="does/not/exist.md")
        prompt = _build_task_prompt(score, [], stand_path=tmp_path)
        assert "See `does/not/exist.md` on this branch." in prompt

    def test_architecture_plan_recovered_from_convention_when_path_missing(
        self, tmp_path: Path
    ) -> None:
        # plan_path lost from coordinare state, but the architect's commit
        # still lives at docs/cards/{issue}-{slug}/plan.md on the branch.
        plan = tmp_path / "docs" / "cards" / "70-ability-for-an-emplo" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("RECOVERED_PLAN_BODY\n")
        score = _score(
            title="Ability for an employee to enter hours",
            issue_number=70,
            architecture_plan_path="",
        )
        prompt = _build_task_prompt(score, [], stand_path=tmp_path)
        assert "RECOVERED_PLAN_BODY" in prompt
        assert "## Architecture Plan" in prompt

    def test_architecture_plan_section_omitted_when_no_plan_anywhere(
        self, tmp_path: Path
    ) -> None:
        score = _score(architecture_plan_path="", issue_number=99)
        prompt = _build_task_prompt(score, [], stand_path=tmp_path)
        assert "## Architecture Plan" not in prompt

    def test_card_docs_section_emitted_when_folder_exists(
        self, tmp_path: Path
    ) -> None:
        folder = tmp_path / "docs" / "cards" / "70-ability-for-an-emplo"
        folder.mkdir(parents=True)
        score = _score(
            title="Ability for an employee to enter hours", issue_number=70
        )
        prompt = _build_task_prompt(score, [], stand_path=tmp_path)
        assert "## Card Documentation" in prompt
        assert "docs/cards/70-ability-for-an-emplo/" in prompt

    def test_card_docs_section_omitted_when_folder_missing(
        self, tmp_path: Path
    ) -> None:
        score = _score(title="brand new", issue_number=999)
        prompt = _build_task_prompt(score, [], stand_path=tmp_path)
        assert "## Card Documentation" not in prompt

    def test_persist_job_artifacts_when_log_dir_set(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        log_dir = tmp_path / "perf-logs"
        monkeypatch.setenv("PERFORMER_LOG_DIR", str(log_dir))
        adapter = HermesBackend()
        adapter._job_log_id = "20260521T000000_implementer_deadbeef"
        adapter._persist_job_artifact("prompt", "PROMPT_BODY")
        adapter._persist_job_artifact("stdout", "STDOUT_BODY")
        assert (log_dir / f"{adapter._job_log_id}.prompt").read_text() == "PROMPT_BODY"
        assert (log_dir / f"{adapter._job_log_id}.stdout").read_text() == "STDOUT_BODY"

    def test_persist_job_artifact_redacts_api_key(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("PERFORMER_LOG_DIR", str(tmp_path))
        adapter = HermesBackend()
        adapter._api_key = "sk-secret-xyz"
        adapter._job_log_id = "j1"
        adapter._persist_job_artifact("stdout", "leaked sk-secret-xyz here")
        body = (tmp_path / "j1.stdout").read_text()
        assert "sk-secret-xyz" not in body
        assert "***" in body


# ---------------------------------------------------------------------------
# US2: capability gating
# ---------------------------------------------------------------------------


class TestUS2CapabilityGating:
    async def test_disabled_toolsets_written_into_profile_config(
        self, tmp_path: Path, hermes_env
    ) -> None:
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())
        try:
            cfg_path = adapter._profile_dir / "config.yaml"
            text = cfg_path.read_text()
            for name in DISABLED_TOOLSETS:
                assert f"- {name}" in text
        finally:
            await adapter.stop()

    async def test_allowed_toolsets_passed_on_cli(
        self, tmp_path: Path, hermes_env
    ) -> None:
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        try:
            args = list(mock_exec.call_args[0])
            ts_idx = args.index("--toolsets")
            assert args[ts_idx + 1] == "terminal,file,search,browser,todo"
            joined = " ".join(map(str, args))
            for forbidden in DISABLED_TOOLSETS:
                assert forbidden not in joined, f"{forbidden!r} leaked into argv"
        finally:
            await adapter.stop()

    async def test_yolo_flag_passed_on_cli(
        self, tmp_path: Path, hermes_env
    ) -> None:
        """`--yolo` must be on the argv: without it, hermes-agent's
        dangerous-command approval gate fails closed in a non-interactive
        subprocess (no TTY → "BLOCKED: User denied")."""
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        try:
            args = list(mock_exec.call_args[0])
            assert "--yolo" in args
        finally:
            await adapter.stop()

    async def test_approvals_mode_off_written_into_profile_config(
        self, tmp_path: Path, hermes_env
    ) -> None:
        """Belt-and-suspenders for `--yolo`: per-job config.yaml must also
        set `approvals.mode: off` so a future CLI change that drops the
        flag still leaves the gate disabled."""
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())
        try:
            cfg = (adapter._profile_dir / "config.yaml").read_text()
            assert "approvals:" in cfg
            # Must be the YAML string "off", not the bare token (which
            # YAML 1.1 coerces to boolean False — hermes-agent would then
            # silently fall back to the manual gate).
            assert 'mode: "off"' in cfg
            import yaml
            parsed = yaml.safe_load(cfg)
            assert parsed["approvals"]["mode"] == "off"
        finally:
            await adapter.stop()

    async def test_operator_hermes_home_is_ignored(
        self, tmp_path: Path, hermes_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("HERMES_HOME", "/tmp/operator-hermes")
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        try:
            env = mock_exec.call_args[1]["env"]
            assert env["HERMES_HOME"] != "/tmp/operator-hermes"
            assert "hermes-job-" in env["HERMES_HOME"]
        finally:
            await adapter.stop()

    async def test_base_url_forwarded_when_set(
        self, tmp_path: Path, hermes_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("HERMES_BASE_URL", "https://provider.example/v1")
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        try:
            args = list(mock_exec.call_args[0])
            # The installed hermes CLI does not accept --base-url on `chat`;
            # the endpoint is routed via the HERMES_BASE_URL env var instead.
            assert "--base-url" not in args
            env = mock_exec.call_args[1]["env"]
            assert env.get("HERMES_BASE_URL") == "https://provider.example/v1"
        finally:
            await adapter.stop()

    async def test_custom_provider_written_when_base_url_set(
        self, tmp_path: Path, hermes_env, monkeypatch
    ) -> None:
        """077: when HERMES_BASE_URL is set, the per-job config.yaml must use
        hermes-agent 0.15.2's `model:` schema (provider=custom + base_url +
        default + api_key_env) — the older `providers:` block is ignored by
        0.15.2 ("no providers found"). CLI must pass --provider custom."""
        monkeypatch.setenv("HERMES_PROVIDER", "litellm")
        monkeypatch.setenv("HERMES_BASE_URL", "https://provider.example/v1")
        monkeypatch.setenv("HERMES_MODEL", "spark/qwen3.6:35b")
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        try:
            cfg = (adapter._profile_dir / "config.yaml").read_text()
            assert "model:" in cfg
            assert "provider: custom" in cfg
            assert "base_url: https://provider.example/v1" in cfg
            assert "default: spark/qwen3.6:35b" in cfg
            # Key referenced via env, never embedded.
            assert "api_key_env: HERMES_API_KEY" in cfg
            assert "sk-test-12345" not in cfg
            # The stale providers: schema must be gone (note: api_key_env is
            # the NEW field — don't false-match on the "key_env" substring).
            assert "providers:" not in cfg
            assert "api_mode:" not in cfg
            # CLI passes the built-in custom provider, not the label "litellm".
            args = list(mock_exec.call_args[0])
            p_idx = args.index("--provider")
            assert args[p_idx + 1] == "custom"
        finally:
            await adapter.stop()

    async def test_no_model_block_when_base_url_unset(
        self, tmp_path: Path, hermes_env
    ) -> None:
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())
        try:
            cfg = (adapter._profile_dir / "config.yaml").read_text()
            assert "model:" not in cfg
            assert "providers:" not in cfg
        finally:
            await adapter.stop()

    async def test_base_url_absent_when_unset(
        self, tmp_path: Path, hermes_env
    ) -> None:
        # hermes_env fixture delenv's HERMES_BASE_URL — verify nothing leaks.
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        try:
            args = list(mock_exec.call_args[0])
            assert "--base-url" not in args
            env = mock_exec.call_args[1]["env"]
            assert "HERMES_BASE_URL" not in env
        finally:
            await adapter.stop()

    async def test_forbidden_env_vars_stripped(
        self, tmp_path: Path, hermes_env, monkeypatch
    ) -> None:
        forbidden = {
            "HERMES_GATEWAY_URL": "https://nope",
            "HERMES_MESSAGING_TOKEN": "abc",
            "HERMES_CRON_SPEC": "0 * * * *",
            "HERMES_USER_MEMORY_PATH": "/tmp/leak",
        }
        for k, v in forbidden.items():
            monkeypatch.setenv(k, v)
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        try:
            env = mock_exec.call_args[1]["env"]
            for k in forbidden:
                assert k not in env, f"{k!r} should be stripped"
        finally:
            await adapter.stop()

    def test_api_key_redacted_in_events(self) -> None:
        adapter = HermesBackend()
        adapter._api_key = "sk-test-secret-abc"
        adapter._event_buffer.append(
            BackendEvent(
                type=BackendEventType.progress,
                text="leaked sk-test-secret-abc here",
                detail="also sk-test-secret-abc",
            )
        )
        events = adapter.drain_events()
        assert len(events) == 1
        assert "sk-test-secret-abc" not in events[0].text
        assert "sk-test-secret-abc" not in events[0].detail


# ---------------------------------------------------------------------------
# US3: lifecycle parity
# ---------------------------------------------------------------------------


class TestUS3Lifecycle:
    async def test_relay_feedback_queues_without_subprocess_call(self) -> None:
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(),
        ) as mock_exec:
            await adapter.relay_feedback("hi-1")
            await adapter.relay_feedback("hi-2")
        mock_exec.assert_not_awaited()
        assert adapter._feedback_queue == ["hi-1", "hi-2"]
        assert adapter.get_status().state == "working"

    async def test_relay_feedback_drains_into_next_invocation_prompt(
        self, tmp_path: Path, hermes_env
    ) -> None:
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        await adapter.relay_feedback("FEEDBACK_ALPHA")
        await adapter.relay_feedback("FEEDBACK_BETA")
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await adapter.start(_stand(tmp_path), _score())
        try:
            args = list(mock_exec.call_args[0])
            q_idx = args.index("-q")
            prompt = args[q_idx + 1]
            assert "FEEDBACK_ALPHA" in prompt
            assert "FEEDBACK_BETA" in prompt
            assert prompt.index("FEEDBACK_ALPHA") < prompt.index("FEEDBACK_BETA")
            assert adapter._feedback_queue == []
        finally:
            await adapter.stop()

    async def test_stop_terminates_subprocess_and_cleans_profile(
        self, tmp_path: Path, hermes_env
    ) -> None:
        proc = _fake_proc(returncode=None)
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score())
        profile = adapter._profile_dir
        assert profile is not None and profile.exists()

        # Mock os.killpg + os.getpgid so we don't actually signal.
        with patch("performer.backends.hermes.os.getpgid", return_value=proc.pid), \
             patch("performer.backends.hermes.os.killpg") as kpg:
            # Once stop SIGTERMs, mark the process as exited so wait() resolves.
            def _set_exit(*_args, **_kwargs):
                proc.returncode = 143
            kpg.side_effect = _set_exit
            await adapter.stop()

        status = adapter.get_status()
        assert status.state == "error"
        assert status.error_reason == "stopped"
        assert not profile.exists()
        # Idempotent.
        await adapter.stop()

    async def test_malformed_output_resolves_to_error_not_loop(
        self, tmp_path: Path, hermes_env
    ) -> None:
        proc = _fake_proc(returncode=0, stdout_b=b"", stderr_b=b"")
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score(role="qa"))
        # The reader task will wake up, see empty/missing JSON output, and
        # transition to error. Await it.
        if adapter._reader_task is not None:
            await adapter._reader_task

        status = adapter.get_status()
        assert status.state == "error"
        assert status.error_reason == "malformed_output"
        assert adapter._profile_dir is None  # cleaned

    async def test_prose_role_accepts_non_json_stdout(
        self, tmp_path: Path, hermes_env
    ) -> None:
        # Prose-contract roles (implementer, env_bootstrap, architect) must
        # NOT be classified as malformed when Hermes emits a prose recap
        # rather than JSON — the wrapper consumes side effects, not stdout.
        for role in ("implementer", "env_bootstrap", "architect"):
            proc = _fake_proc(
                returncode=0,
                stdout_b=b"installed python 3.11, configured uv, cached deps",
            )
            adapter = HermesBackend()
            with patch(
                "performer.backends.hermes.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ):
                await adapter.start(_stand(tmp_path), _score(role=role))
            if adapter._reader_task is not None:
                await adapter._reader_task

            status = adapter.get_status()
            assert status.state == "done", (
                f"role={role} expected done, got {status.state} "
                f"({status.error_reason!r})"
            )
            assert status.output and "installed python" in status.output

    async def test_json_role_output_carries_full_stdout(
        self, tmp_path: Path, hermes_env
    ) -> None:
        # Regression: JSON-role replies (e.g. assessor) rarely carry summary/
        # result/message keys, so `output` must surface the full stdout for
        # main.py's _extract_json to re-parse.
        assess_json = (
            'Here is my assessment:\n'
            '{"sufficient": true, "questions": [], "dependencies": []}\n'
        )
        proc = _fake_proc(returncode=0, stdout_b=assess_json.encode())
        adapter = HermesBackend()
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score(role="assessor"))
        if adapter._reader_task is not None:
            await adapter._reader_task

        status = adapter.get_status()
        assert status.state == "done", (
            f"expected done, got {status.state} ({status.error_reason!r})"
        )
        assert status.output is not None
        assert '"sufficient": true' in status.output
        assert '"questions"' in status.output

    async def test_env_bootstrap_prompt_has_dedicated_output_block(self) -> None:
        prompt = _build_task_prompt(_score(role="env_bootstrap"), [])
        assert "Role Output Requirements (env_bootstrap)" in prompt
        # Must NOT ask for JSON-only output — env_bootstrap has no JSON contract.
        assert "ONLY a valid JSON" not in prompt

    @pytest.mark.parametrize(
        "scenario",
        ["done", "malformed", "subprocess_exit", "stopped"],
    )
    async def test_profile_cleanup_on_every_terminal_outcome(
        self, tmp_path: Path, hermes_env, scenario: str
    ) -> None:
        if scenario == "done":
            payload = json.dumps({"summary": "ok", "tokens": 12}).encode()
            proc = _fake_proc(returncode=0, stdout_b=payload)
        elif scenario == "malformed":
            proc = _fake_proc(returncode=0)
        elif scenario == "subprocess_exit":
            proc = _fake_proc(returncode=2, stderr_b=b"boom")
        else:  # stopped
            proc = _fake_proc(returncode=None)

        adapter = HermesBackend()
        # Use a JSON-only role so the "malformed" scenario (empty stdout)
        # actually trips the malformed_output branch — prose roles tolerate it.
        role = "qa" if scenario == "malformed" else "implementer"
        with patch(
            "performer.backends.hermes.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            await adapter.start(_stand(tmp_path), _score(role=role))

        profile = adapter._profile_dir
        assert profile is not None and profile.exists()

        if scenario == "done":
            if adapter._reader_task is not None:
                await adapter._reader_task
            assert adapter.get_status().state == "done"
        elif scenario in {"malformed", "subprocess_exit"}:
            if adapter._reader_task is not None:
                await adapter._reader_task
            assert adapter.get_status().state == "error"
        else:  # stopped
            with patch("performer.backends.hermes.os.getpgid", return_value=proc.pid), \
                 patch("performer.backends.hermes.os.killpg") as kpg:
                def _set_exit(*_a, **_k):
                    proc.returncode = 143
                kpg.side_effect = _set_exit
                await adapter.stop()
            assert adapter.get_status().state == "error"

        assert not profile.exists()

    def test_extract_json_object_handles_noisy_stdout(self) -> None:
        # Step 1: clean JSON parses directly.
        assert _extract_json_object('{"a": 1}') == {"a": 1}
        # Step 2: outer-trim recovers JSON wrapped in log noise.
        text = 'INFO: starting\n{"summary": "ok", "tokens": 5}\nINFO: done\n'
        assert _extract_json_object(text) == {"summary": "ok", "tokens": 5}
        # Step 3: an unrelated brace in footer must not poison the result —
        # the largest balanced span containing real JSON still wins.
        text = (
            "{starting up}\n"
            '{"summary": "real payload", "tokens": 9}\n'
            "session_id={abc}\n"
        )
        assert _extract_json_object(text) == {
            "summary": "real payload",
            "tokens": 9,
        }
        # Empty / non-dict / unparseable inputs return None.
        assert _extract_json_object("") is None
        assert _extract_json_object("no braces here") is None
        assert _extract_json_object("[1, 2, 3]") is None  # not a dict

    def test_no_hermes_internal_state_leaks(self) -> None:
        """BackendStatus.state stays in the shared vocabulary regardless of inputs."""
        allowed = {"working", "done", "error"}
        adapter = HermesBackend()
        # Initial state.
        assert adapter.get_status().state in allowed
        # Reason strings the adapter sets are well-known short tokens.
        for reason in ("missing_env:HERMES_API_KEY", "malformed_output",
                       "subprocess_exit:2", "stopped"):
            status = BackendStatus(state="error", error_reason=reason)
            assert status.state in allowed
            assert ":" in reason or reason in {"malformed_output", "stopped"}


# ---------------------------------------------------------------------------
# 077: card docs written as CARD.md (commit-safe) for tech_writer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_docs_written_excluded_and_referenced(tmp_path, hermes_env):
    """Hermes writes the card context to CARD.md, git-excludes it (it commits,
    unlike the read-only reviewer), and points the prompt at it."""
    (tmp_path / ".git" / "info").mkdir(parents=True)  # simulate a real checkout
    proc = _fake_proc(returncode=None)
    adapter = HermesBackend()
    with patch(
        "performer.backends.hermes.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ) as mock_exec:
        await adapter.start(
            _stand(tmp_path),
            _score(role="tech_writer", title="Reduce contact-form margins",
                   description="Excessive mobile margins on the contact card.",
                   acceptance_criteria=["Margins reduced on mobile"]),
        )
    try:
        # CARD.md written with the card context.
        card_md = tmp_path / "CARD.md"
        assert card_md.is_file()
        body = card_md.read_text()
        assert "Reduce contact-form margins" in body
        assert "Excessive mobile margins" in body
        assert "Margins reduced on mobile" in body
        # git-excluded so the committing tech_writer can't leak it into the PR.
        assert "CARD.md" in (tmp_path / ".git" / "info" / "exclude").read_text().split()
        # prompt points the agent at it.
        args = list(mock_exec.call_args[0])
        q_idx = args.index("-q")
        assert "CARD.md" in args[q_idx + 1]
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_card_docs_exclude_no_duplicate_on_existing_entry(tmp_path, hermes_env):
    """Re-running with CARD.md already in exclude does not duplicate the entry."""
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("*.log\nCARD.md\n")
    adapter = HermesBackend()
    with patch(
        "performer.backends.hermes.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=_fake_proc(returncode=None)),
    ):
        await adapter.start(_stand(tmp_path), _score(role="tech_writer"))
    try:
        lines = (info / "exclude").read_text().split()
        assert lines.count("CARD.md") == 1
    finally:
        await adapter.stop()


class TestSubprocessEnvPolicy:
    """088 B1: hermes must follow the shared image-PATH-first append policy.

    Pre-fix, hermes passed the FULL cache_env into the subprocess — the
    project's pinned old node shadowed the image's node, the startup-crash
    class #110 fixed for claude_code.
    """

    def _adapter_with_envs(self, *, cache_env, git_env=None, tool_env=None):
        adapter = HermesBackend()
        adapter._cache_env = cache_env
        adapter._git_env = git_env or {}
        adapter._tool_env = tool_env or {}
        return adapter

    def test_cache_path_appended_after_image_path(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
        adapter = self._adapter_with_envs(
            cache_env={
                "PATH": "/devenv/x/node-v18.12.1/bin:/usr/bin",
                "RBENV_ROOT": "/devenv/x/rbenv",
            },
        )
        env = adapter._build_subprocess_env(
            profile_dir=tmp_path, api_key="sk", base_url=""
        )
        # Image dirs FIRST — the CLI's interpreter resolves to the image's node.
        assert env["PATH"].startswith("/usr/local/bin:/usr/bin")
        # Cache toolchain dirs APPENDED — reachable, never shadowing the image.
        assert env["PATH"].index("/usr/local/bin") < env["PATH"].index("node-v18.12.1")
        # Non-PATH cache vars still flow.
        assert env["RBENV_ROOT"] == "/devenv/x/rbenv"

    def test_forbidden_prefixes_still_filtered(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_GATEWAY_URL", "http://evil")
        adapter = self._adapter_with_envs(cache_env={})
        env = adapter._build_subprocess_env(
            profile_dir=tmp_path, api_key="sk", base_url=""
        )
        assert "HERMES_GATEWAY_URL" not in env

    def test_hermes_home_still_forced_to_profile_dir(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_HOME", "/operator/home")
        adapter = self._adapter_with_envs(cache_env={})
        env = adapter._build_subprocess_env(
            profile_dir=tmp_path, api_key="sk", base_url="http://litellm/v1"
        )
        assert env["HERMES_HOME"] == str(tmp_path)
        assert env["HERMES_API_KEY"] == "sk"
        assert env["HERMES_BASE_URL"] == "http://litellm/v1"


# ---------------------------------------------------------------------------
# large-prompt offloading (E2BIG / MAX_ARG_STRLEN guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_large_prompt_offloaded_to_task_md(
    tmp_path: Path, hermes_env
) -> None:
    """When the assembled prompt exceeds _MAX_INLINE_BYTES the backend writes it
    to TASK.md in the workspace and passes a short -q reference instead, avoiding
    OSError E2BIG (Linux MAX_ARG_STRLEN ~128 KB per CLI argument)."""
    proc = _fake_proc(returncode=None)
    big_diff = "+" + "a" * 110_000
    score = _score(pr_diff=big_diff)
    adapter = HermesBackend()
    with patch(
        "performer.backends.hermes.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ) as mock_exec:
        await adapter.start(_stand(tmp_path), score)
    try:
        mock_exec.assert_awaited_once()
        args = list(mock_exec.call_args[0])
        q_idx = args.index("-q")
        inline_msg = args[q_idx + 1]

        # The -q arg must be short (a TASK.md reference, not the full prompt).
        assert len(inline_msg.encode("utf-8")) < 1000
        assert "TASK.md" in inline_msg

        # TASK.md must exist at the workspace root with the full prompt content.
        task_md = tmp_path / "TASK.md"
        assert task_md.is_file(), "TASK.md must be written for large prompts"
        assert big_diff[:50] in task_md.read_text()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_hermes_small_prompt_not_offloaded(
    tmp_path: Path, hermes_env
) -> None:
    """Prompts under the threshold are passed inline — TASK.md must NOT be created."""
    proc = _fake_proc(returncode=None)
    score = _score(pr_diff="+one line change")
    adapter = HermesBackend()
    with patch(
        "performer.backends.hermes.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ) as mock_exec:
        await adapter.start(_stand(tmp_path), score)
    try:
        mock_exec.assert_awaited_once()
        assert not (tmp_path / "TASK.md").exists(), "TASK.md must NOT exist for small prompts"
    finally:
        await adapter.stop()
