"""Env-cache routing reads the transport through the real root configuration wrapper."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from coordinare.config import CoordinareConfiguration, ProjectConfiguration, SymphonyConfig
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import EnvCacheService, daemon_sees_performer_cache
from coordinare.services.env_provenance import ProvenanceCheck

COMMIT = "e" * 40
DIGEST = "a" * 64
STAT = "b" * 64


def _config(tmp_path: Path, transport: Literal["kubernetes", "subprocess"] = "kubernetes") -> CoordinareConfiguration:
    return CoordinareConfiguration(
        global_config=ProjectConfiguration(
            project_name="repo", github_org="org", github_project_number=1,
            github_token="test-token", human_reviewers=["alice"], agent_transport=transport,
            env_cache_root=tmp_path / "cache", state_file_path=tmp_path / "state.json",
        ),
        symphonies=[SymphonyConfig(name="sym", github_project_number=1)],
    )


def _cache(tmp_path: Path) -> EnvCacheState:
    return EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=tmp_path / "cache" / "sym-abc",
        provenance_digest=DIGEST, provenance_stat_digest=STAT,
        provenance_inputs={"activate_sh": "export PATH=$DEVENV/bin:$PATH\n", "debs": {}},
    )


@pytest.mark.parametrize(("transport", "visible"), [("kubernetes", False), ("subprocess", True)])
@pytest.mark.parametrize("wrapped", [True, False])
def test_daemon_cache_visibility(tmp_path: Path, transport: Literal["kubernetes", "subprocess"],
                                 visible: bool, wrapped: bool) -> None:
    cfg = _config(tmp_path, transport)
    assert daemon_sees_performer_cache({"coordinare_config": cfg if wrapped else cfg.global_config}) is visible


@pytest.mark.asyncio
async def test_bootstrap_dispatch_pins_commit_with_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    service = EnvCacheService(cfg)
    ec = _cache(tmp_path)
    github = AsyncMock()
    github.get_branch_head_sha.return_value = COMMIT
    github.get_file_content.return_value = "Environment"
    async def reviewed_artifacts(**kwargs):
        from tests.unit.env_manifest_fixture import reviewed_cache

        reviewed_cache(ec, source_commit=COMMIT)
        return "", False, False

    monkeypatch.setattr(service, "_build_manifest_artifacts", AsyncMock(side_effect=reviewed_artifacts))
    monkeypatch.setattr(service, "_load_test_env_vars", AsyncMock(return_value={}))
    dispatch = AsyncMock()
    sym = cfg.symphonies[0]

    await service._do_dispatch(
        symphony_name="sym", symphony_config=sym, github_service=github,
        eff_config=sym.effective_config(cfg.global_config), current_sha="cache-key",
        cache_state=ec, dispatch_fn=dispatch,
    )

    assert ec.bootstrap_commit_sha == COMMIT
    github.get_branch_head_sha.assert_awaited_once_with("main")
    assert github.get_file_content.await_count > 0
    assert all(call.kwargs["ref"] == COMMIT for call in github.get_file_content.await_args_list)
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_content_check_uses_claim_with_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = EnvCacheService(_config(tmp_path))
    ec = _cache(tmp_path)
    expected = ProvenanceCheck(True, hashed=True)
    claim = AsyncMock(return_value=expected)
    walk = AsyncMock(side_effect=AssertionError("must not walk the daemon cache"))
    monkeypatch.setattr(service, "_check_claim", claim)
    monkeypatch.setattr(service, "_run_walk", walk)

    assert await service._content_check("sym", ec, DIGEST) is expected
    claim.assert_awaited_once_with("sym", ec, DIGEST)
    walk.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_uses_frozen_kubernetes_provenance_with_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EnvCacheService(_config(tmp_path))
    ec = _cache(tmp_path)
    expected = ProvenanceCheck(True, hashed=True)
    check = AsyncMock(return_value=expected)
    local = AsyncMock(side_effect=AssertionError("must not verify the daemon cache"))
    monkeypatch.setattr(service, "_content_check", check)
    monkeypatch.setattr(service, "_verify_local", local)

    assert await service._verify("sym", ec) == (expected, DIGEST)
    check.assert_awaited_once_with("sym", ec, DIGEST)
    local.assert_not_awaited()


@pytest.mark.asyncio
async def test_payload_uses_frozen_inputs_with_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = EnvCacheService(_config(tmp_path))
    ec = _cache(tmp_path)
    local = AsyncMock(side_effect=AssertionError("must not open a daemon manifest"))
    monkeypatch.setattr(service, "_open", local)

    payload = await service._payload_for("sym", ec, DIGEST, "/devenv/sym-abc")

    assert payload == {
        **ec.provenance_inputs, "version": 2, "slug": "sym-abc", "cache_path": "/devenv/sym-abc",
        "digest": DIGEST, "stat_digest": STAT, "max_entries": 1_000_000, "max_bytes": 16 * 1024**3,
    }
    local.assert_not_awaited()
