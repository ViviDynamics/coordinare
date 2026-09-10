"""Workflow-lane commands must see the activated devenv (issue #351).

``asyncio.create_subprocess_shell`` runs ``/bin/sh -c``. On the performer
image ``/bin/sh`` is dash, which honours ``$ENV`` only for *interactive*
shells -- so a non-interactive command never sourced the env-cache activation
script and the spec-117 devenv was invisible. Measured in a live container
mid-run:

    sh -c   'bundle --version'  ->  sh: 1: bundle: not found
    bash -c 'bundle --version'  ->  4.0.15

``Dockerfile.full`` already sets ``BASH_ENV=/etc/devenv-activate.sh`` for
exactly this purpose; bash honours it for non-interactive shells. These tests
pin that commands run under bash, and that the activation hook actually
reaches them.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from performer.workspace import _command_shell, run_command

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to test the bash path"
)


class TestCommandShellSelection:
    def test_prefers_bash_when_available(self) -> None:
        assert Path(_command_shell()).name == "bash"

    def test_falls_back_to_sh_without_bash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No bash means the previous behaviour, not a crash."""
        monkeypatch.setattr("performer.workspace.shutil.which", lambda _name: None)
        assert _command_shell() == "/bin/sh"


class TestActivationHookReachesCommands:
    """The behaviour the fix exists for, not just the shell name."""

    @pytest.mark.asyncio
    async def test_bash_env_hook_is_sourced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A BASH_ENV script must put its exports on PATH for the command.

        This is the image's activation mechanism in miniature: the profile
        script prepends the env-cache bin dirs to PATH, and the command has to
        see them. Under the old /bin/sh path it did not.
        """
        toolbin = tmp_path / "toolbin"
        toolbin.mkdir()
        (toolbin / "vivi-marker-tool").write_text("#!/bin/sh\necho MARKER-OK\n")
        (toolbin / "vivi-marker-tool").chmod(0o755)

        hook = tmp_path / "activate-hook.sh"
        hook.write_text(f'export PATH="{toolbin}:$PATH"\n')
        monkeypatch.setenv("BASH_ENV", str(hook))

        result = await run_command("vivi-marker-tool", tmp_path)

        assert result.exit_code == 0, result.stderr
        assert "MARKER-OK" in result.stdout

    @pytest.mark.asyncio
    async def test_missing_binary_still_reports_127(self, tmp_path: Path) -> None:
        """Without the hook, a missing binary must still exit 127.

        127 is what #352's baseline guard keys on, so the two fixes have to
        agree about it.
        """
        result = await run_command("vivi-definitely-not-installed", tmp_path)

        assert result.exit_code == 127
        assert result.success is False


class TestExistingBehaviourPreserved:
    """The shell swap must not change what run_command already guaranteed."""

    @pytest.mark.asyncio
    async def test_stdout_and_exit_zero(self, tmp_path: Path) -> None:
        result = await run_command("echo hello", tmp_path)
        assert result.exit_code == 0
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    async def test_nonzero_exit_propagates(self, tmp_path: Path) -> None:
        result = await run_command("exit 3", tmp_path)
        assert result.exit_code == 3
        assert result.success is False

    @pytest.mark.asyncio
    async def test_stderr_captured(self, tmp_path: Path) -> None:
        result = await run_command("echo oops >&2", tmp_path)
        assert "oops" in result.stderr

    @pytest.mark.asyncio
    async def test_runs_in_the_given_cwd(self, tmp_path: Path) -> None:
        """A login shell would risk a profile cd'ing elsewhere; bash -c does not."""
        (tmp_path / "sentinel.txt").write_text("x")
        result = await run_command("ls sentinel.txt", tmp_path)
        assert result.exit_code == 0
        assert "sentinel.txt" in result.stdout

    @pytest.mark.asyncio
    async def test_shell_operators_still_work(self, tmp_path: Path) -> None:
        result = await run_command("echo a && echo b | tr a-z A-Z", tmp_path)
        assert result.exit_code == 0
        assert "B" in result.stdout
