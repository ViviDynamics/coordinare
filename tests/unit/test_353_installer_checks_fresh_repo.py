"""Installer-phase services and checks see {repo} as the clean consumer will: as it was before any install.

Dettmore e2e-83 and e2e-86: npm ci wrote node_modules into {repo}, every installer check passed, and the clean
consumer, which gets a fresh {repo} and keeps only {cache}, failed with "tailwindcss: not found".
"""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.runner import run_manifest

from tests.unit.test_351_environment_runner import contract


def _dirs(tmp_path):
    roots = {name: tmp_path / name for name in ("cache", "repo", "scratch")}
    for path in roots.values():
        path.mkdir()
    (roots["repo"] / "source.txt").write_text("tracked\n")
    return roots


@pytest.mark.asyncio
async def test_an_install_into_repo_is_not_visible_to_installer_checks(tmp_path):
    roots = _dirs(tmp_path)
    manifest = contract([{"id": "baseline", "argv": ["bash", "-c", "test -f source.txt && test -f node_modules/tool"],
                          "cwd": "{repo}"}],
                        install=[{"id": "install-deps", "argv": ["bash", "-c", "mkdir -p node_modules && touch node_modules/tool"],
                                  "cwd": "{repo}"}])
    report = await run_manifest(manifest, **roots, install=True)
    statuses = {result.id: result.status for result in report.results}
    assert statuses["install-deps"] == "pass"
    assert statuses["baseline"] == "fail"
    assert not list(tmp_path.glob("coordinare-consumer-repo-*"))


@pytest.mark.asyncio
async def test_an_install_into_cache_is_what_installer_checks_use(tmp_path):
    roots = _dirs(tmp_path)
    manifest = contract([{"id": "baseline", "argv": ["bash", "-c", "test -f source.txt && test -f {cache}/tool"],
                          "cwd": "{repo}"}],
                        install=[{"id": "install-deps", "argv": ["touch", "{cache}/tool"]}])
    report = await run_manifest(manifest, **roots, install=True)
    assert report.passed


@pytest.mark.asyncio
async def test_installer_checks_see_cache_read_only_and_its_modes_come_back(tmp_path):
    # Dettmore e2e-83, e2e-92 and e2e-94: bundler wrote Gemfile.lock into the clean consumer's read-only {cache}.
    roots = _dirs(tmp_path)
    manifest = contract([{"id": "baseline", "argv": ["bash", "-c", "test -f {cache}/tool && ! touch {cache}/lock"]}],
                        install=[{"id": "install-deps", "argv": ["touch", "{cache}/tool"]}])
    before = (roots["cache"].stat().st_mode & 0o777)
    report = await run_manifest(manifest, **roots, install=True)
    assert report.passed
    assert not (roots["cache"] / "lock").exists()
    assert roots["cache"].stat().st_mode & 0o777 == before
    assert (roots["cache"] / "tool").stat().st_mode & 0o200
