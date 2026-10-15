"""Spec 265: the rendered verify works against the layout the bootstrap agent installs on garden.

The layout is ``<cache>/.rbenv/versions/3.4.2`` with ``RBENV_ROOT=<cache>/.rbenv``, and the bundle under
``<cache>/project/bundle``. The verify runs from the repo checkout, not from the performer's own directory. The fake
ruby and bundle reproduce the two failures in the issue: rbenv finds no version, and bundler finds no Gemfile.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from performer.workspace import run_env_cache_verify

from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.services.env_manifest import render_activate_sh, render_verify_sh

RUBY = "3.4.2"


def _manifest() -> EnvManifest:
    return EnvManifest(
        symphony_name="dettmore",
        items=[
            ManifestItem(name="ruby", kind="runtime", version=RUBY, source=".ruby-version"),
            ManifestItem(name="bundler", kind="gem", source="Gemfile.lock"),
            ManifestItem(name="rails", kind="gem", source="Gemfile"),
        ],
    )


def _fake(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _garden_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Return (cache, checkout, elsewhere). The checkout holds the Gemfile, as it does under /tmp/performer-*.

    The gem check falls back to a host-level ``gem list``, so a gem installed on the test machine would pass the
    check for the wrong reason. A ``gem`` that always fails is put first on PATH to keep the test hermetic.
    """
    no_gem = tmp_path / "no-gem"
    no_gem.mkdir()
    _fake(no_gem / "gem", "exit 1\n")
    monkeypatch.setenv("PATH", f"{no_gem}:{os.environ.get('PATH', '')}")
    cache = tmp_path / "devenv" / "dettmore-platform-615891"
    rbenv_bin = cache / ".rbenv" / "versions" / RUBY / "bin"
    rbenv_bin.mkdir(parents=True)
    (cache / "project" / "bundle").mkdir(parents=True)
    # ruby prints the version for `ruby -e "print RUBY_VERSION"`. bundle resolves the Gemfile from the cwd.
    _fake(rbenv_bin / "ruby", f"printf {RUBY}\n")
    _fake(
        rbenv_bin / "bundle",
        'if [ ! -f ./Gemfile ]; then echo "Could not locate Gemfile" >&2; exit 10; fi\n'
        'if [ "$1" = "exec" ]; then\n'
        '  { echo "$BUNDLE_GEMFILE"; echo "${BUNDLE_PATH:-}"; } > "$COORDINARE_TEST_MARK"\n'
        "fi\n"
        "exit 0\n",
    )
    checkout = tmp_path / "performer-1" / "repo"
    checkout.mkdir(parents=True)
    (checkout / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails'\n")
    (checkout / ".ruby-version").write_text(RUBY + "\n")
    elsewhere = tmp_path / "app"
    elsewhere.mkdir()
    return cache, checkout, elsewhere


@pytest.mark.asyncio
async def test_the_rendered_verify_passes_on_the_garden_layout_from_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, checkout, elsewhere = _garden_layout(tmp_path, monkeypatch)
    mark = tmp_path / "mark.txt"
    monkeypatch.setenv("COORDINARE_TEST_MARK", str(mark))
    manifest = _manifest()
    activate = render_activate_sh(manifest, cache_mount_path=str(cache))
    verify = render_verify_sh(manifest, cache_mount_path=str(cache), activate_text=activate)
    # The performer's own process runs from a directory with no Gemfile, as it does on Kubernetes.
    monkeypatch.chdir(elsewhere)

    passed, detail = await run_env_cache_verify(
        str(cache), None, cwd=str(checkout), verify_text=verify,
    )

    assert passed is True, detail
    gemfile, bundle_path = mark.read_text().splitlines()
    assert Path(gemfile).resolve() == (checkout / "Gemfile").resolve()
    # The bundle the bootstrap installed in the cache is the one bundler uses.
    assert bundle_path == str(cache / "project" / "bundle")


@pytest.mark.asyncio
async def test_the_same_verify_run_from_the_wrong_directory_fails_as_the_issue_saw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-fix run: no checkout as the cwd. bundler finds no Gemfile, which is the failure in #265."""
    cache, _checkout, elsewhere = _garden_layout(tmp_path, monkeypatch)
    monkeypatch.setenv("COORDINARE_TEST_MARK", str(tmp_path / "mark.txt"))
    manifest = _manifest()
    activate = render_activate_sh(manifest, cache_mount_path=str(cache))
    verify = render_verify_sh(manifest, cache_mount_path=str(cache), activate_text=activate)
    monkeypatch.chdir(elsewhere)

    passed, detail = await run_env_cache_verify(str(cache), None, verify_text=verify)

    assert passed is False
    assert "FAIL: gem" in detail


@pytest.mark.asyncio
async def test_the_rails_smoke_test_runs_in_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, checkout, elsewhere = _garden_layout(tmp_path, monkeypatch)
    mark = tmp_path / "mark.txt"
    monkeypatch.setenv("COORDINARE_TEST_MARK", str(mark))
    manifest = _manifest()
    activate = render_activate_sh(manifest, cache_mount_path=str(cache))
    verify = render_verify_sh(manifest, cache_mount_path=str(cache), activate_text=activate)
    monkeypatch.chdir(elsewhere)

    passed, _detail = await run_env_cache_verify(
        str(cache), None, cwd=str(checkout), verify_text=verify,
    )

    # The smoke ran (it wrote the marker), and it ran against the checkout's Gemfile, not /repo.
    assert passed is True
    assert mark.exists()
    assert Path(mark.read_text().splitlines()[0]).resolve() == (checkout / "Gemfile").resolve()
