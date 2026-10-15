"""Spec 265: the env_bootstrap verify runs the daemon's rendered text, from the repo checkout, with DEVENV set.

The bootstrap pod cannot read the daemon's cache on Kubernetes, so the daemon sends its rendered verify.sh and
activate.sh in the dispatch. These tests pin: the text is written over the agent's copies at job start, the text
(not the cache copy) decides success, the script runs with cwd set to the checkout, and an older daemon's cache
copy still runs (from the checkout) with the basis reported as cache_file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from performer.env_provenance import PROVENANCE_ROOT_ENV, ProvenanceMaterialiseError, write_bootstrap_scripts
from performer.main import _prepare_env_provenance
from performer.server.models import _SCRIPT_MAX_CHARS, BootstrapScripts, JobInitPayload
from performer.workspace import (
    VERIFY_BASIS_CACHE_FILE,
    VERIFY_BASIS_DAEMON_RENDERED,
    run_env_cache_verify,
    verify_basis_for,
)

DEVENV_BOOTSTRAP = "_DEVENV_BOOTSTRAP"


def _cache(tmp_path: Path) -> Path:
    cache = tmp_path / "devenv" / "sym-abc"
    cache.mkdir(parents=True)
    return cache


def _checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "performer-1" / "repo"
    checkout.mkdir(parents=True)
    (checkout / "Gemfile").write_text("source 'https://rubygems.org'\n")
    return checkout


def _record_text(record: Path, exit_code: int = 0) -> str:
    """A verify script that records where it ran and the env it saw, then exits."""
    return (
        f"pwd -P > {record}\n"
        f'echo "$DEVENV" >> {record}\n'
        f'echo "$COORDINARE_REPO_DIR" >> {record}\n'
        f"exit {exit_code}\n"
    )


def _same_path(recorded: str, expected: Path) -> bool:
    return Path(recorded).resolve() == expected.resolve()


@pytest.mark.asyncio
async def test_daemon_text_runs_from_the_checkout_with_devenv_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, checkout = _cache(tmp_path), _checkout(tmp_path)
    record = tmp_path / "record.txt"
    # The performer's own process is NOT in the checkout, which is the Kubernetes situation (it runs from /app).
    monkeypatch.chdir(tmp_path)

    passed, detail = await run_env_cache_verify(
        str(cache), None, cwd=str(checkout), verify_text=_record_text(record),
    )

    assert passed is True, detail
    where, devenv, repo_dir = record.read_text().splitlines()
    assert _same_path(where, checkout)
    assert devenv == str(cache)
    assert _same_path(repo_dir, checkout)


@pytest.mark.asyncio
async def test_the_cache_copy_never_decides_when_daemon_text_is_given(
    tmp_path: Path,
) -> None:
    cache, checkout = _cache(tmp_path), _checkout(tmp_path)
    # The agent's copy in the cache would pass. The daemon's text fails. Only the text may decide.
    (cache / "verify.sh").write_text("#!/bin/bash\nexit 0\n")
    passed, _ = await run_env_cache_verify(
        str(cache), None, cwd=str(checkout), verify_text="echo FAIL: real check\nexit 1\n",
    )
    assert passed is False

    # The reverse: a cache copy that fails does not fail a daemon text that passes.
    (cache / "verify.sh").write_text("#!/bin/bash\nexit 1\n")
    passed, _ = await run_env_cache_verify(
        str(cache), None, cwd=str(checkout), verify_text="exit 0\n",
    )
    assert passed is True
    assert verify_basis_for("exit 0\n", passed=passed) == VERIFY_BASIS_DAEMON_RENDERED


@pytest.mark.asyncio
async def test_without_daemon_text_the_cache_file_runs_from_the_checkout(
    tmp_path: Path,
) -> None:
    """An older daemon sends no text. The cache copy still runs, but from the checkout, and the basis says so."""
    cache, checkout = _cache(tmp_path), _checkout(tmp_path)
    record = tmp_path / "record.txt"
    (cache / "verify.sh").write_text(_record_text(record))

    passed, detail = await run_env_cache_verify(str(cache), None, cwd=str(checkout))

    assert passed is True, detail
    assert _same_path(record.read_text().splitlines()[0], checkout)
    assert verify_basis_for(None, passed=passed) == VERIFY_BASIS_CACHE_FILE


@pytest.mark.asyncio
async def test_a_bundle_check_from_the_wrong_directory_reproduces_the_gemfile_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure in #265: a fake bundle resolves the Gemfile from the cwd, as bundler does."""
    cache, checkout = _cache(tmp_path), _checkout(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    bundle = fake_bin / "bundle"
    bundle.write_text(
        "#!/bin/sh\n"
        'if [ ! -f ./Gemfile ]; then echo "Could not locate Gemfile" >&2; exit 10; fi\n'
        "exit 0\n"
    )
    bundle.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    text = "bundle show rails >/dev/null 2>&1 || { echo FAIL: gem rails; exit 1; }\nexit 0\n"
    elsewhere = tmp_path / "app"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    # The pre-fix run: no cwd, so the performer's own directory (no Gemfile) is used.
    old_passed, old_detail = await run_env_cache_verify(str(cache), None, verify_text=text)
    assert old_passed is False
    assert "FAIL: gem rails" in old_detail

    # The fix: the checkout is the cwd, so bundler finds the Gemfile.
    passed, detail = await run_env_cache_verify(str(cache), None, cwd=str(checkout), verify_text=text)
    assert passed is True, detail


@pytest.mark.asyncio
async def test_the_verify_shell_does_not_source_the_devenv_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BASH_ENV is the devenv profile in the image. The verify must not source it before reading its script."""
    cache, checkout = _cache(tmp_path), _checkout(tmp_path)
    marker = tmp_path / "profile-ran"
    profile = tmp_path / "profile.sh"
    profile.write_text(f"touch {marker}\n")
    monkeypatch.setenv("BASH_ENV", str(profile))

    passed, _ = await run_env_cache_verify(str(cache), None, cwd=str(checkout), verify_text="exit 0\n")

    assert passed is True
    assert not marker.exists()


def test_write_bootstrap_scripts_overwrites_the_agent_copies(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    (cache / "verify.sh").write_text("# the agent's own verify, which passes\nexit 0\n")
    (cache / "activate.sh").write_text("# the agent's own activation\n")

    write_bootstrap_scripts(
        str(cache),
        BootstrapScripts(verify_sh="# daemon verify\nexit 1\n", activate_sh="# daemon activate\n"),
    )

    assert (cache / "verify.sh").read_text() == "# daemon verify\nexit 1\n"
    assert (cache / "activate.sh").read_text() == "# daemon activate\n"
    # No temporary file is left behind for a reader to find.
    assert sorted(p.name for p in cache.iterdir()) == ["activate.sh", "verify.sh"]


def test_write_bootstrap_scripts_refuses_a_missing_cache(tmp_path: Path) -> None:
    scripts = BootstrapScripts(verify_sh="exit 0\n", activate_sh="# a\n")
    with pytest.raises(ProvenanceMaterialiseError):
        write_bootstrap_scripts(str(tmp_path / "absent"), scripts)


def test_bootstrap_scripts_are_strict_and_size_bounded() -> None:
    too_big = "x" * (_SCRIPT_MAX_CHARS + 1)
    with pytest.raises(ValidationError):
        BootstrapScripts(verify_sh=too_big, activate_sh="# a\n")
    with pytest.raises(ValidationError):
        BootstrapScripts(verify_sh="", activate_sh="# a\n")
    with pytest.raises(ValidationError):
        BootstrapScripts.model_validate({"verify_sh": "exit 0\n", "activate_sh": "# a\n", "extra": 1})


def _bootstrap_job(cache: Path, scripts: BootstrapScripts | None) -> JobInitPayload:
    return JobInitPayload(
        job_id="job-1",
        card_id="env_bootstrap:sym",
        role="env_bootstrap",
        backend="claude_code",
        persona="install",
        repo_url="https://github.com/org/repo.git",
        branch="env-bootstrap-1",
        env_cache_path=str(cache),
        bootstrap_scripts=scripts,
    )


def test_job_prep_overwrites_the_agent_copies_before_the_job_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _cache(tmp_path)
    (cache / "verify.sh").write_text("exit 0\n")
    monkeypatch.setenv(DEVENV_BOOTSTRAP, "")
    monkeypatch.delenv(PROVENANCE_ROOT_ENV, raising=False)
    scripts = BootstrapScripts(verify_sh="# daemon verify\nexit 1\n", activate_sh="# daemon activate\n")

    refusal = _prepare_env_provenance(_bootstrap_job(cache, scripts))

    assert refusal is None
    assert (cache / "verify.sh").read_text() == "# daemon verify\nexit 1\n"
    assert (cache / "activate.sh").read_text() == "# daemon activate\n"


def test_job_prep_without_daemon_scripts_leaves_the_cache_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _cache(tmp_path)
    (cache / "verify.sh").write_text("# the cache copy\n")
    monkeypatch.setenv(DEVENV_BOOTSTRAP, "")

    refusal = _prepare_env_provenance(_bootstrap_job(cache, None))

    assert refusal is None
    assert (cache / "verify.sh").read_text() == "# the cache copy\n"
