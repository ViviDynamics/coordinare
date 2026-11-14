"""A slow cache removal is unconfirmed cleanup, never an aborted run.

Dettmore e2e-88: removing a kept 1.2 GB trial generation outlasted the cleanup's 60 s bound, and the raised
TimeoutError aborted the run as validation_stage_failed with five repairs and 3.6 hours left.
"""
from __future__ import annotations

import asyncio

import pytest
import scripts.validate_environment_repositories as harness


class Hung:
    returncode = None

    async def wait(self):
        await asyncio.sleep(3600)

    def kill(self):
        self.returncode = -9


@pytest.mark.asyncio
async def test_a_removal_that_outlasts_its_bound_is_unconfirmed(tmp_path, monkeypatch):
    root = tmp_path / "kept-cache"
    root.mkdir()
    process = Hung()

    async def spawn(*argv, **kwargs):
        return process

    async def removed(name):
        return True

    async def stopped(proc, timeout):
        proc.kill()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(harness, "_remove_container", removed)
    monkeypatch.setattr(harness, "_bounded_wait", stopped)
    assert await harness._cleanup_roots([root], "image") is False
    assert root.exists()


def test_cleanup_allows_for_multi_gigabyte_caches():
    assert harness.CLEANUP_SECONDS >= 600
