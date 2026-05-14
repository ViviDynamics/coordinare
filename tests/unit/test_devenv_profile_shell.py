"""Shell-level tests for agent/performer/devenv-profile.sh.

The profile script must be safe to source across the three shell invocation
modes that agent CLI backends spawn (login bash, non-login bash, dash -c)
and must short-circuit on re-entry to avoid recursing when activate.sh runs
its own command substitutions.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "agent" / "performer" / "devenv-profile.sh"


pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="bash required for these shell tests"
)


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=10, check=False
    )


@pytest.fixture
def fake_devenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake /devenv-style tree the profile script can glob.

    The profile script hard-codes ``/devenv/*/activate.sh``, so we point it
    at a writable tmpdir by patching the glob via a wrapper script that
    re-executes the body with our path.
    """
    devenv = tmp_path / "devenv"
    (devenv / "sym").mkdir(parents=True)
    activate = devenv / "sym" / "activate.sh"
    activate.write_text(
        "export DEVENV_ACTIVATED=1\n"
        'export PATH="/fake/devenv/bin:$PATH"\n'
    )
    return devenv


def _patched_profile(tmp_path: Path, devenv_root: Path) -> Path:
    """Copy profile.sh and rewrite /devenv/* glob to point at the fixture."""
    original = PROFILE.read_text()
    patched = original.replace("/devenv/*/activate.sh", f"{devenv_root}/*/activate.sh")
    out = tmp_path / "devenv-profile.sh"
    out.write_text(patched)
    return out


def test_profile_sources_under_bash_login(
    tmp_path: Path, fake_devenv: Path
) -> None:
    """bash -lc reads /etc/profile.d/*; the profile must activate env."""
    profile = _patched_profile(tmp_path, fake_devenv)
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    env.pop("_DEVENV_SOURCED", None)

    result = _run(
        ["bash", "-c", f". {profile} && echo SOURCED=$DEVENV_ACTIVATED && echo PATH=$PATH"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "SOURCED=1" in result.stdout
    assert "/fake/devenv/bin" in result.stdout


def test_profile_sources_under_bash_env(
    tmp_path: Path, fake_devenv: Path
) -> None:
    """Non-login bash honors $BASH_ENV; verify activation via that path."""
    profile = _patched_profile(tmp_path, fake_devenv)
    env = {**os.environ, "BASH_ENV": str(profile)}
    env.pop("_DEVENV_SOURCED", None)

    result = _run(
        ["bash", "-c", "echo SOURCED=$DEVENV_ACTIVATED"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "SOURCED=1" in result.stdout


def test_profile_reentry_guard_prevents_recursion(
    tmp_path: Path, fake_devenv: Path
) -> None:
    """When activate.sh itself runs a subshell (e.g. rbenv init), bash will
    re-source BASH_ENV.  The exported _DEVENV_SOURCED sentinel must short
    out the second pass so we don't recurse infinitely."""
    devenv = fake_devenv
    # Replace activate.sh with one that runs a command substitution — this
    # mimics ``eval "$(rbenv init - bash)"`` which is the production trigger.
    activate = devenv / "sym" / "activate.sh"
    activate.write_text(
        "export DEVENV_ACTIVATED=1\n"
        # Command substitution: bash spawns a subshell which re-reads BASH_ENV
        'export REENTRY_CHECK="$(echo inner)"\n'
    )
    profile = _patched_profile(tmp_path, devenv)
    env = {**os.environ, "BASH_ENV": str(profile)}
    env.pop("_DEVENV_SOURCED", None)

    result = _run(
        ["bash", "-c", "echo OK=$DEVENV_ACTIVATED REENTRY=$REENTRY_CHECK"],
        env=env,
        # If recursion happened, the subprocess would hang and timeout.
    )

    assert result.returncode == 0, result.stderr
    assert "OK=1" in result.stdout
    assert "REENTRY=inner" in result.stdout


def test_profile_no_op_when_no_activate_files(tmp_path: Path) -> None:
    """Empty /devenv dir must not error — the glob simply matches nothing."""
    empty = tmp_path / "empty-devenv"
    empty.mkdir()
    profile = _patched_profile(tmp_path, empty)
    env = {**os.environ}
    env.pop("_DEVENV_SOURCED", None)

    result = _run(
        ["bash", "-c", f". {profile} && echo OK"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_profile_sentinel_skips_subsequent_sources(
    tmp_path: Path, fake_devenv: Path
) -> None:
    """If _DEVENV_SOURCED is already set when the profile runs, it must
    return without touching the environment a second time."""
    profile = _patched_profile(tmp_path, fake_devenv)
    env = {**os.environ, "_DEVENV_SOURCED": "1"}

    result = _run(
        ["bash", "-c", f". {profile} && echo DA=${{DEVENV_ACTIVATED:-unset}}"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    # Activate.sh should not have run, so DEVENV_ACTIVATED stays unset.
    assert "DA=unset" in result.stdout
