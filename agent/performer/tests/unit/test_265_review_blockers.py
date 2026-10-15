"""Spec 265 review blockers B1 and B2, and the encoding follow-ups.

B1: the scripts are written with a safe create. A link planted at the temp name is refused, not followed, and a link at
the target is replaced, never written through.

B2: the daemon's verify text never reaches the verify child on stdin. A child that reads stdin cannot swallow the script.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import performer.env_provenance as provenance
from performer.env_provenance import ProvenanceMaterialiseError, write_bootstrap_scripts
from performer.server.models import BootstrapScripts
from performer.workspace import run_env_cache_verify


def _cache(tmp_path: Path) -> Path:
    cache = tmp_path / "devenv" / "sym-abc"
    cache.mkdir(parents=True)
    return cache


def _scripts() -> BootstrapScripts:
    return BootstrapScripts(verify_sh="#!/bin/bash\nexit 0\n", activate_sh="# act\n")


def test_a_link_planted_at_the_temp_name_is_refused_and_the_victim_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _cache(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("ORIGINAL\n")
    victim.chmod(0o600)
    # The temp name is fixed here so the planted link lands on it.
    monkeypatch.setattr(provenance, "_temp_name", lambda name: f".{name}.planted.tmp")
    (cache / ".verify.sh.planted.tmp").symlink_to(victim)

    with pytest.raises(ProvenanceMaterialiseError):
        write_bootstrap_scripts(str(cache), _scripts())

    assert victim.read_text() == "ORIGINAL\n"
    assert oct(victim.stat().st_mode & 0o777) == oct(0o600)


def test_a_link_at_the_target_is_replaced_and_never_written_through(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("KEEP\n")
    (cache / "activate.sh").symlink_to(outside)

    write_bootstrap_scripts(str(cache), _scripts())

    assert outside.read_text() == "KEEP\n"
    assert not (cache / "activate.sh").is_symlink()
    assert (cache / "activate.sh").read_text() == "# act\n"


def test_the_written_script_is_never_group_or_world_writable(tmp_path: Path) -> None:
    cache = _cache(tmp_path)

    write_bootstrap_scripts(str(cache), _scripts())

    assert (cache / "verify.sh").stat().st_mode & 0o022 == 0


@pytest.mark.asyncio
async def test_a_child_reading_stdin_cannot_swallow_the_rest_of_the_verify(tmp_path: Path) -> None:
    """B2: with the script on stdin, `cat` consumed the failing check and the verify passed. From a file it cannot."""
    cache = _cache(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    text = (
        'echo "FAIL: first check"; FAILED=1\n'
        "cat >/dev/null\n"
        'if [ "$FAILED" -ne 0 ]; then echo "FAILED TRUE"; exit 1; fi\n'
        "exit 0\n"
    )

    passed, detail = await run_env_cache_verify(str(cache), None, cwd=str(checkout), verify_text=text)

    assert passed is False, detail
    assert "FAILED TRUE" in detail


@pytest.mark.asyncio
async def test_the_verify_text_is_not_kept_after_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    seen: list[str] = []
    real = provenance.write_replacing

    def _record(path: Path, data: bytes, mode: int) -> None:
        seen.append(str(path))
        real(path, data, mode)

    monkeypatch.setattr("performer.workspace.write_replacing", _record)

    await run_env_cache_verify(str(cache), None, cwd=str(checkout), verify_text="exit 0\n")

    assert seen, "the verify text must be written to a private file"
    assert not Path(seen[0]).exists()
    assert not str(seen[0]).startswith(str(cache))
    assert not str(seen[0]).startswith(str(checkout))


@pytest.mark.asyncio
async def test_text_that_is_not_utf8_is_refused_with_a_reason(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    passed, detail = await run_env_cache_verify(
        str(cache), None, cwd=str(checkout), verify_text="echo \ud800\n",
    )

    assert passed is False
    assert "UTF-8" in detail


def test_a_payload_bound_is_in_bytes_not_characters() -> None:
    """A script of 4-byte characters within the character bound is still over the byte bound."""
    four_byte = "\U0001F642"
    chars = 256 * 1024 // 2
    with pytest.raises(ValidationError):
        BootstrapScripts(verify_sh=four_byte * chars, activate_sh="# a\n")


def test_a_lone_surrogate_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        BootstrapScripts(verify_sh="exit 0\n\ud800", activate_sh="# a\n")
