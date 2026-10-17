"""Spec 265: a consumer whose cache mount was wiped or recreated is refused before any work.

On Kubernetes the daemon cannot see the devenv claim, so its own record can say "ready" after the claim is wiped.
The consumer then mounts an empty subPath. The performer refuses it, and the response carries
env_cache_health_failed, so the daemon re-bootstraps the cache.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import performer.main as main
from performer.env_provenance import PROVENANCE_ROOT_ENV, provenance_slug
from performer.server.models import EnvProvenance, JobInitPayload


def _consumer(cache: Path, *, provenance: bool = True) -> JobInitPayload:
    prov = None
    if provenance:
        prov = EnvProvenance(
            slug=provenance_slug(cache.name),
            cache_path=str(cache),
            activate_sh="# frozen activate\n",
            debs={},
            digest="0" * 64,
        )
    return JobInitPayload(
        job_id="job-1",
        card_id="card-1",
        role="implementer",
        backend="claude_code",
        persona="implement",
        repo_url="https://github.com/org/repo.git",
        branch="feat/x",
        env_cache_path=str(cache),
        env_provenance=prov,
    )


def _bootstrap(cache: Path) -> JobInitPayload:
    return JobInitPayload(
        job_id="job-2",
        card_id="env_bootstrap:sym",
        role="env_bootstrap",
        backend="claude_code",
        persona="install",
        repo_url="https://github.com/org/repo.git",
        branch="env-bootstrap-1",
        env_cache_path=str(cache),
    )


def test_a_populated_mount_with_activate_is_not_a_gap(tmp_path: Path) -> None:
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    (cache / "activate.sh").write_text("# a\n")
    assert main._consumer_mount_gap(_consumer(cache)) is None


def test_an_empty_mount_is_a_gap_with_a_clear_reason(tmp_path: Path) -> None:
    """The wiped-claim case: the subPath exists and is empty."""
    cache = tmp_path / "sym-abc"
    cache.mkdir()

    reason = main._consumer_mount_gap(_consumer(cache))

    assert reason is not None
    assert "empty" in reason
    assert "re-bootstrap" in reason


def test_a_mount_without_activate_is_a_gap(tmp_path: Path) -> None:
    cache = tmp_path / "sym-abc"
    (cache / ".rbenv").mkdir(parents=True)

    reason = main._consumer_mount_gap(_consumer(cache))

    assert reason is not None
    assert "no activate.sh" in reason


def test_an_absent_mount_is_a_gap(tmp_path: Path) -> None:
    reason = main._consumer_mount_gap(_consumer(tmp_path / "gone"))
    assert reason is not None
    assert "not present" in reason


def test_the_bootstrap_writer_and_jobs_without_a_cache_are_never_gapped(tmp_path: Path) -> None:
    empty = tmp_path / "sym-abc"
    empty.mkdir()
    assert main._consumer_mount_gap(_bootstrap(empty)) is None
    no_cache = JobInitPayload(
        job_id="j", card_id="c", role="implementer", backend="claude_code",
        persona="p", repo_url="https://github.com/org/repo.git", branch="b",
    )
    assert main._consumer_mount_gap(no_cache) is None


@pytest.mark.asyncio
async def test_a_wiped_claim_refuses_the_job_and_asks_for_a_rebootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reviewed_environment_metadata,
) -> None:
    """End to end through _perform_job: no agent work runs, and the summary carries the health flag."""
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    monkeypatch.setattr(main, "PROVENANCE_ROOT", tmp_path / "provenance")
    monkeypatch.setenv(PROVENANCE_ROOT_ENV, "")

    payload = _consumer(cache)
    payload.metadata.update(reviewed_environment_metadata)
    result = await main._perform_job(payload)

    assert result.success is False
    assert result.error_code == "env_cache_missing"
    summary = json.loads(result.summary)
    assert summary["status"] == "error"
    assert summary["env_cache_health_failed"] is True
    assert "re-bootstrap" in summary["reason"]
