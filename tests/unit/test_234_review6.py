"""Spec 234 review round 6: a manifest read error is transient, not drift; a missing manifest is still drift.

- An I/O error on the manifest open, in the background pass or in the payload build, is transient. It records no drift and
  it never reaches the gate's runtime-health marker.
- A missing manifest file (ENOENT in the daemon's state directory) is still drift.
- A persistent transient is surfaced after a streak of dispatches. No re-bootstrap is started.
"""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path
from typing import Any

import pytest

from coordinare.graph.nodes import dispatch_performer
from coordinare.services import env_cache, env_provenance
from coordinare.services.env_cache import EnvCacheService
from coordinare.services.env_provenance import ProvenanceCheck
from tests.unit.test_234_attacks import _consumer_state
from tests.unit.test_234_honest_path import _bootstrapped, _service, _state_map

CONTAINER = "/devenv/sym-abc"


def _frozen(tmp_path: Path) -> tuple[EnvCacheService, Any]:
    state, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state)))
    return service, state


def _eio(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError(errno.EIO, "Input/output error")


def test_an_io_error_on_the_manifest_open_in_the_background_pass_is_transient_not_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, state = _frozen(tmp_path)
    monkeypatch.setattr(env_provenance, "open_mounted_file_or_absent", _eio)
    check = asyncio.run(service.run_full_pass("sym", state))
    assert check is not None and check.ok is False and check.transient
    assert "sym" not in service._drift, "a read error is not drift"


def test_an_io_error_on_the_payload_build_is_transient_and_is_not_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, state = _frozen(tmp_path)
    # The first dispatch runs the content check, which opens the manifest through the environment module. Only the
    # payload build reopens it through the service, so the failure is injected there.
    monkeypatch.setattr(env_cache, "open_manifest", _eio)
    check, payload = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert check.ok is False and check.transient
    assert payload is None
    assert "sym" not in service._drift


def test_an_io_error_never_reaches_the_gates_runtime_health_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Service:
        def __init__(self) -> None:
            self.marked: list[str] = []

        async def resolve_provenance(self, symphony: str, state: Any, *, container_path: str) -> Any:
            return ProvenanceCheck(False, "I/O error reading the frozen manifest: EIO", transient=True), None

        def mark_runtime_health_failed(self, symphony: str, state: Any) -> None:
            self.marked.append(symphony)

    service = _Service()
    released: list[bool] = []
    terminal, payload = asyncio.run(
        dispatch_performer._env_provenance_gate(
            {"env_cache_service": service}, "card-1", "qa", "sym", _consumer_state(tmp_path), False,
            lambda: released.append(True), container_path=CONTAINER,
        ),
    )
    assert terminal is not None and payload is None
    assert released == [True], "the slot is released"
    assert service.marked == [], "a transient refusal does not start a re-bootstrap"


def test_a_missing_manifest_file_is_still_drift(tmp_path: Path) -> None:
    service, state = _frozen(tmp_path)
    manifest = tmp_path / "state" / "env-provenance" / "sym-abc.json"
    manifest.unlink()
    check = asyncio.run(service.run_full_pass("sym", state))
    assert check is not None and check.ok is False and not check.transient
    assert "sym" in service._drift, "ENOENT in the daemon's state directory is a real problem"


def test_a_persistent_transient_is_surfaced_after_a_streak_and_not_re_bootstrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, state = _frozen(tmp_path)
    service._provenance_full_at.pop("sym", None)  # force the content check on every dispatch
    monkeypatch.setattr(env_cache, "content_pass", _eio)
    reasons: list[str] = []
    for _ in range(env_cache.TRANSIENT_STREAK_LIMIT):
        check, _payload = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
        assert check.transient
        reasons.append(check.reason or "")
    assert "in a row" not in reasons[0], "the first refusal is not yet a streak"
    assert "in a row" in reasons[-1], "the streak is surfaced to the operator"
    assert "No re-bootstrap has been started" in reasons[-1]
    assert "sym" not in service._drift, "a streak of transient failures is still not drift"


def test_a_pass_clears_the_streak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, state = _frozen(tmp_path)
    service._provenance_full_at.pop("sym", None)
    real = env_cache.content_pass
    monkeypatch.setattr(env_cache, "content_pass", _eio)
    asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert service._transient_streak.get("sym") == 1
    monkeypatch.setattr(env_cache, "content_pass", real)
    check, _payload = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert check.ok
    assert "sym" not in service._transient_streak
