"""Spec 234 review round: the version gate and the dispatch wiring.

A dispatch that mounts an env cache must carry a frozen payload, whatever the image reports. The reviewer showed
an old image accepting a cache mount with no payload. The wiring tests drive the real provenance gate through
``_prepare_env_dispatch`` against a drifted cache, so a deleted call site fails here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.graph.nodes import dispatch_performer
from coordinare.services.env_cache import EnvCacheService
from coordinare.services.http_performer_service import HTTPPerformerService
from tests.unit.env_mount_stub import mount_config
from tests.unit.test_234_honest_path import ACTIVATE, _bootstrapped, _state_map


def _service_for_gate() -> HTTPPerformerService:
    svc = object.__new__(HTTPPerformerService)
    svc._config = SimpleNamespace(id="perf-1")
    return svc


@pytest.mark.parametrize("version", [0, 1])
def test_a_cache_mount_without_a_frozen_payload_is_refused_on_any_image(version: int) -> None:
    payload = SimpleNamespace(env_provenance=None, env_cache_path="/devenv/sym-abc", role="implementer")
    status = SimpleNamespace(env_provenance_version=version)
    with pytest.raises(ValueError, match="env cache without provenance"):
        asyncio.run(_service_for_gate()._require_provenance_capability(payload, status))


def test_no_cache_mount_needs_no_capability() -> None:
    payload = SimpleNamespace(env_provenance=None, env_cache_path=None, role="implementer")
    asyncio.run(_service_for_gate()._require_provenance_capability(payload, None))


def _wired_state(tmp_path: Path, state_ec: Any, service: EnvCacheService) -> dict[str, Any]:
    return {
        "current_symphony": "sym",
        "env_cache": {"sym": state_ec},
        "env_cache_service": service,
        "coordinare_config": mount_config(
            env_cache_root=str(tmp_path / "caches"),
            state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
        ),
    }


def _service(tmp_path: Path) -> EnvCacheService:
    return EnvCacheService(mount_config(
        env_cache_root=str(tmp_path / "caches"),
        state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
    ))


def _no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify(*_a: Any, **_k: Any) -> tuple[bool | None, str]:
        return None, "verify not run in this test"

    monkeypatch.setattr(dispatch_performer, "verify_env_cache_clean", _verify)


def test_the_real_gate_runs_in_prepare_env_dispatch_and_refuses_a_drifted_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the gate call from _prepare_env_dispatch must fail this test."""
    state_ec, cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    assert asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))[0]
    (cache / "bin" / "planted").write_text("x")  # drift after the freeze
    _no_docker(monkeypatch)
    released: list[bool] = []
    state = _wired_state(tmp_path, state_ec, service)
    card_context: dict[str, Any] = {}
    stub = SimpleNamespace(_config=SimpleNamespace(image="img"), devenv_root="/devenv")

    volumes, terminal = asyncio.run(
        dispatch_performer._prepare_env_dispatch(
            state, stub, "card-1", "qa", card_context, lambda: released.append(True),
        ),
    )

    assert terminal is state, "a drifted cache refuses dispatch"
    assert volumes is None, "no cache volume is mounted for a refused dispatch"
    assert released == [True]
    assert "env_cache_path" not in card_context
    assert "env_provenance" not in card_context


def test_an_honest_cache_is_mounted_with_the_frozen_payload_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_ec, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    _no_docker(monkeypatch)
    state = _wired_state(tmp_path, state_ec, service)
    card_context: dict[str, Any] = {}
    stub = SimpleNamespace(_config=SimpleNamespace(image="img"), devenv_root="/devenv")

    volumes, terminal = asyncio.run(
        dispatch_performer._prepare_env_dispatch(
            state, stub, "card-1", "qa", card_context, lambda: None,
        ),
    )

    assert terminal is None
    assert volumes is not None
    assert card_context["env_cache_path"] == "/devenv/sym-abc"
    assert card_context["env_provenance"]["activate_sh"] == ACTIVATE
    assert card_context["env_provenance"]["cache_path"] == "/devenv/sym-abc"
