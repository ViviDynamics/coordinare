"""101: run_service_readiness — the env-bootstrap service-readiness gate.

A required declared service must be started AND connectable (services-health.sh
exit 0) for the bootstrap to be considered ready. A rejected/empty manifest with
required services, or a required service that won't start/connect, → not ok.
No declared services → no-op ok. Optional service failing → warn, still ok.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from performer.workspace import run_service_readiness


def _script(path: Path, exit_code: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _cache_with_services(tmp: Path, *, start_rc: int, health_rc: int, manifest: bool = True) -> Path:
    svc = tmp / "services"
    _script(svc / "services-start.sh", start_rc)
    _script(svc / "services-health.sh", health_rc)
    if manifest:
        (svc / "services.json").write_text('{"services":[{"name":"postgres"}]}')
    return tmp


PG = [{"name": "postgres", "kind": "postgres"}]
INF_OK = {"inference_succeeded": True, "inference_services": ["postgres"]}
INF_REJECTED = {"inference_succeeded": False, "inference_skipped_reason": "rejected"}
INF_TIMEOUT = {"inference_succeeded": False, "inference_skipped_reason": "timeout"}


@pytest.mark.asyncio
async def test_no_declared_services_is_noop_ok(tmp_path: Path) -> None:
    ok, failures = await run_service_readiness(str(tmp_path), {}, [], INF_OK)
    assert ok is True and failures == []


@pytest.mark.asyncio
async def test_required_service_rejected_manifest_fails(tmp_path: Path) -> None:
    # required service declared but inference rejected → not ok, reason names it
    ok, failures = await run_service_readiness(str(tmp_path), {}, PG, INF_REJECTED)
    assert ok is False
    assert any("postgres" in f["service"] for f in failures)
    assert any("manifest" in f["reason"].lower() or "reject" in f["reason"].lower() for f in failures)


@pytest.mark.asyncio
async def test_required_service_connectable_ok(tmp_path: Path) -> None:
    _cache_with_services(tmp_path, start_rc=0, health_rc=0)
    ok, failures = await run_service_readiness(str(tmp_path), {}, PG, INF_OK)
    assert ok is True and failures == []


@pytest.mark.asyncio
async def test_required_service_health_nonzero_fails(tmp_path: Path) -> None:
    _cache_with_services(tmp_path, start_rc=0, health_rc=1)
    ok, failures = await run_service_readiness(str(tmp_path), {}, PG, INF_OK)
    assert ok is False
    assert any("postgres" in f["service"] for f in failures)


@pytest.mark.asyncio
async def test_required_service_start_nonzero_fails(tmp_path: Path) -> None:
    _cache_with_services(tmp_path, start_rc=1, health_rc=0)
    ok, failures = await run_service_readiness(str(tmp_path), {}, PG, INF_OK)
    assert ok is False


@pytest.mark.asyncio
async def test_optional_service_failure_warns_not_blocks(tmp_path: Path) -> None:
    _cache_with_services(tmp_path, start_rc=0, health_rc=1)
    optional = [{"name": "mailhog", "kind": "mailhog", "required": False}]
    ok, failures = await run_service_readiness(str(tmp_path), {}, optional, INF_OK)
    assert ok is True


@pytest.mark.asyncio
async def test_timeout_with_valid_manifest_defers_to_health(tmp_path: Path) -> None:
    # inference timed out (succeeded=False) BUT a manifest exists + services
    # connectable → defer to start+health (don't auto-reject on the flag).
    _cache_with_services(tmp_path, start_rc=0, health_rc=0)
    ok, failures = await run_service_readiness(str(tmp_path), {}, PG, INF_TIMEOUT)
    assert ok is True and failures == []


@pytest.mark.asyncio
async def test_timeout_no_required_services_is_noop(tmp_path: Path) -> None:
    ok, failures = await run_service_readiness(str(tmp_path), {}, [], INF_TIMEOUT)
    assert ok is True and failures == []


@pytest.mark.asyncio
async def test_timeout_required_no_manifest_rejects(tmp_path: Path) -> None:
    # timeout + required service + NO manifest at all → reject (nothing to act on)
    ok, failures = await run_service_readiness(str(tmp_path), {}, PG, INF_TIMEOUT)
    assert ok is False
    assert any("postgres" in f["service"] for f in failures)


@pytest.mark.asyncio
async def test_reason_is_secret_free(tmp_path: Path) -> None:
    _cache_with_services(tmp_path, start_rc=0, health_rc=1)
    os.environ["POSTGRESQL_PASSWORD"] = "s3cr3t-should-not-appear"
    try:
        ok, failures = await run_service_readiness(str(tmp_path), {"POSTGRESQL_PASSWORD": "s3cr3t-should-not-appear"}, PG, INF_OK)
    finally:
        os.environ.pop("POSTGRESQL_PASSWORD", None)
    assert ok is False
    assert all("s3cr3t-should-not-appear" not in f["reason"] for f in failures)
