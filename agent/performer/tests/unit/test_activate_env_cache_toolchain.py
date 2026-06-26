"""Spec 120 US2: env-cache activation must resolve the project toolchain.

Covers the deterministic ``$DEVENV`` fix (the toolchain resolves even when the
performer process inherited ``_DEVENV_SOURCED=1``) and the advertised-toolchain
resolution check + observability.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from performer.workspace import (
    _activate_env_cache,
    _unresolved_advertised_toolchain,
)


# --- pure helper -----------------------------------------------------------


def test_helper_no_advertisement_asserts_nothing():
    assert _unresolved_advertised_toolchain("export PATH=/x:$PATH", "/x") is None


def test_helper_advertised_but_unresolved(tmp_path: Path):
    # activate.sh mentions rbenv but no `ruby` on the given PATH → flagged.
    reason = _unresolved_advertised_toolchain(
        'export PATH="$DEVENV/.rbenv/shims:$PATH"  # rbenv', str(tmp_path)
    )
    assert reason is not None
    assert "ruby" in reason
    assert "advertised_toolchain_unresolved" in reason


def test_helper_advertised_and_resolved(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ruby = bindir / "ruby"
    ruby.write_text("#!/bin/sh\n")
    ruby.chmod(ruby.stat().st_mode | stat.S_IXUSR)
    reason = _unresolved_advertised_toolchain(
        'export RBENV_ROOT="$DEVENV/.rbenv"', str(bindir)
    )
    assert reason is None


def test_helper_bare_mention_does_not_advertise(tmp_path: Path):
    # A passing mention of "rbenv" in a comment must NOT advertise (tightened
    # markers require a path segment or env-var assignment).
    assert _unresolved_advertised_toolchain(
        "# this is not an rbenv project, just mentioning it", str(tmp_path)
    ) is None


def test_helper_empty_path_returns_none():
    assert _unresolved_advertised_toolchain("RBENV_ROOT", "") is None


# --- $DEVENV resolution (the core T012 fix) --------------------------------


@pytest.mark.asyncio
async def test_devenv_resolves_even_with_devenv_sourced_guard_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """activate.sh resolves a $DEVENV-relative bin onto PATH even when the
    calling process has _DEVENV_SOURCED=1 (the guard the old code tripped)."""
    toolchain = tmp_path / ".toolchain" / "bin"
    toolchain.mkdir(parents=True)
    activate = tmp_path / "activate.sh"
    activate.write_text('export PATH="$DEVENV/.toolchain/bin:$PATH"\n')

    # Simulate the performer process having already sourced the devenv profile.
    monkeypatch.setenv("_DEVENV_SOURCED", "1")

    result = await _activate_env_cache(str(tmp_path))

    resolved = result.get("PATH", "")
    assert str(toolchain) in resolved, (
        f"expected {toolchain} on activated PATH, got {resolved!r}"
    )
    # DEVENV itself is a bootstrap pointer and must not leak into the delta.
    assert "DEVENV" not in result
    assert "_DEVENV_SOURCED" not in result


# The two tests below verify the WIRING (helper verdict → env-failure signal)
# deterministically by patching the helper. The helper's own resolve/unresolve
# logic is covered by the pure ``test_helper_*`` tests above — testing it through
# _activate_env_cache is unreliable on a dev host that has a system ruby (which
# the performer container does not).


@pytest.mark.asyncio
async def test_unresolved_toolchain_flags_health_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the helper reports an unresolved advertised toolchain, activation
    sets the env-cache health-failure signal (drained onto the response)."""
    from performer import workspace

    activate = tmp_path / "activate.sh"
    activate.write_text('export PATH="$DEVENV/.rbenv/shims:$PATH"\n')
    monkeypatch.setattr(
        workspace,
        "_unresolved_advertised_toolchain",
        lambda _text, _path: "advertised_toolchain_unresolved: ruby",
    )
    workspace.consume_env_cache_health_failure()  # drain pre-existing

    await _activate_env_cache(str(tmp_path))

    assert workspace.consume_env_cache_health_failure() is True
    assert workspace.consume_services_start_failure() is not None


@pytest.mark.asyncio
async def test_resolved_toolchain_does_not_flag_health_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from performer import workspace

    activate = tmp_path / "activate.sh"
    activate.write_text('export PATH="$DEVENV/.rbenv/shims:$PATH"\n')
    monkeypatch.setattr(
        workspace, "_unresolved_advertised_toolchain", lambda _text, _path: None
    )
    workspace.consume_env_cache_health_failure()

    await _activate_env_cache(str(tmp_path))

    assert workspace.consume_env_cache_health_failure() is False
