"""Behavior tests for agent/performer/entrypoint.sh.

The entrypoint is shell, so we exercise it as a subprocess with stubbed
external commands (rtk, npm, curl, python) on PATH. Each stub logs its
invocation to a temp file; assertions check what was called.

Covers spec 062 Fix 2 — RTK_ENABLED gate behaviour.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "agent" / "performer" / "entrypoint.sh"


def _make_stubs(tmp_path: Path) -> tuple[Path, Path]:
    """Create a stub-bin dir on PATH; return (bindir, logfile)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    logfile = tmp_path / "calls.log"
    for name in ("rtk", "npm", "curl", "python"):
        stub = bindir / name
        stub.write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{logfile}"\nexit 0\n'
        )
        stub.chmod(0o755)
    return bindir, logfile


def _run(bindir: Path, env_extra: dict[str, str]) -> tuple[int, str, str]:
    # HOME points at tmp_path so the entrypoint's `mkdir -p $HOME/.claude`
    # lands in the test sandbox, not the real user home.
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(bindir.parent)}
    env.update(env_extra)
    result = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


def _read_log(logfile: Path) -> str:
    return logfile.read_text() if logfile.exists() else ""


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present"
)
def test_entrypoint_skips_rtk_when_disabled(tmp_path):
    """Default (RTK_ENABLED unset) MUST NOT call rtk."""
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, _err = _run(bindir, {"BACKEND": "codex"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk" not in log, f"rtk should not have been called; log:\n{log}"
    assert "python -m performer" in log


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present"
)
def test_entrypoint_skips_rtk_init_for_codex_with_info_log(tmp_path):
    """Codex has no rtk auto-hook upstream; we log INFO and skip init."""
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, err = _run(bindir, {"BACKEND": "codex", "RTK_ENABLED": "1"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk init" not in log, f"rtk init must not run for codex; log:\n{log}"
    assert "no codex auto-hook" in err


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present"
)
def test_entrypoint_runs_rtk_init_for_claude(tmp_path):
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, _err = _run(bindir, {"BACKEND": "claude", "RTK_ENABLED": "1"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk init -g" in log, f"expected rtk init -g; log:\n{log}"


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present"
)
def test_entrypoint_creates_claude_dir_before_rtk_init(tmp_path):
    """rtk init -g requires $HOME/.claude to pre-exist; entrypoint must mkdir."""
    bindir, _logfile = _make_stubs(tmp_path)
    home = bindir.parent
    claude_dir = home / ".claude"
    assert not claude_dir.exists()
    rc, _out, _err = _run(bindir, {"BACKEND": "claude", "RTK_ENABLED": "1"})
    assert rc == 0
    assert claude_dir.is_dir(), "entrypoint should have created $HOME/.claude"


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present"
)
def test_entrypoint_warns_and_skips_rtk_for_unsupported_backend(tmp_path):
    """Unsupported backends with RTK_ENABLED=1 warn but don't call rtk."""
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, err = _run(bindir, {"BACKEND": "opencode", "RTK_ENABLED": "1"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk init" not in log, f"rtk should not be called; log:\n{log}"
    assert "no supported rtk hook" in err


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present"
)
def test_entrypoint_warns_when_rtk_enabled_without_backend(tmp_path):
    bindir, logfile = _make_stubs(tmp_path)
    rc, _out, err = _run(bindir, {"RTK_ENABLED": "1"})
    assert rc == 0
    log = _read_log(logfile)
    assert "rtk init" not in log
    assert "BACKEND unset" in err


@pytest.mark.skipif(
    not ENTRYPOINT.exists(), reason="entrypoint.sh not present"
)
def test_entrypoint_rtk_failure_is_fatal_when_enabled(tmp_path):
    """If rtk init exits non-zero with RTK_ENABLED=1, entrypoint MUST fail.

    Silent fallback to no-compression defeats the explicit opt-in and makes
    A/B comparisons meaningless.
    """
    bindir, logfile = _make_stubs(tmp_path)
    # Replace rtk stub with one that fails.
    (bindir / "rtk").write_text(
        f'#!/bin/sh\necho "rtk $*" >> "{logfile}"\nexit 1\n'
    )
    (bindir / "rtk").chmod(0o755)

    rc, _out, err = _run(bindir, {"BACKEND": "claude", "RTK_ENABLED": "1"})
    assert rc != 0
    log = _read_log(logfile)
    assert "python -m performer" not in log
    assert "rtk init failed" in err
