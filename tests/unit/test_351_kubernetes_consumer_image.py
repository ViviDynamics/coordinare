"""Kubernetes clean verification must cover each configured consumer image."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from coordinare.config import CoordinareConfiguration
from coordinare.services.env_cache import verify_env_cache_clean
from tests.unit.test_265_provenance_k8s import freeze, logs, setup, summary

VERIFIED_IMAGE = "trusted/performer@sha256:" + "a" * 64
OTHER_IMAGE = "trusted/performer@sha256:" + "b" * 64


def configuration(*, consumer_image=VERIFIED_IMAGE, bootstrap_image=OTHER_IMAGE,
                  transport="kubernetes", bootstrap_enabled=True, consumer_roles=None):
    return CoordinareConfiguration.model_validate({
        "global_config": {
            "github_org": "org", "project_name": "repo", "human_reviewers": ["reviewer"],
            "github_token": "token", "agent_transport": transport,
            "kubernetes_verifier_image": VERIFIED_IMAGE,
            "performer_endpoints": [
                {"id": "bootstrap", "mode": "ephemeral", "roles": ["env_bootstrap"], "image": bootstrap_image},
                {"id": "consumer", "mode": "ephemeral", "roles": consumer_roles or ["implementer", "reviewer"],
                 "image": consumer_image},
            ],
        },
        "symphonies": [{"name": "sym", "github_project_number": 1,
                        "env_bootstrap_performer_id": "bootstrap" if bootstrap_enabled else None}],
    })


def test_kubernetes_rejects_consumer_image_outside_verified_environment():
    with pytest.raises(ValidationError, match=r"consumer.*kubernetes_verifier_image"):
        configuration(consumer_image=OTHER_IMAGE)


def test_kubernetes_accepts_verified_consumer_with_distinct_bootstrap_image():
    cfg = configuration()
    assert cfg.global_config.performer_endpoints[0].image == OTHER_IMAGE
    assert cfg.global_config.performer_endpoints[1].image == VERIFIED_IMAGE


def test_mixed_bootstrap_consumer_endpoint_still_requires_verified_image():
    with pytest.raises(ValidationError, match=r"consumer.*kubernetes_verifier_image"):
        configuration(consumer_image=OTHER_IMAGE, consumer_roles=["env_bootstrap", "implementer"])


@pytest.mark.parametrize("options", [{"transport": "subprocess"}, {"bootstrap_enabled": False}])
def test_unrelated_deployments_keep_independent_images(options):
    configuration(consumer_image=OTHER_IMAGE, **options)


@pytest.mark.asyncio
async def test_kubernetes_readiness_uses_clean_verifier_evidence_without_docker(tmp_path, monkeypatch):
    async def unavailable_docker(*args, **kwargs):
        raise AssertionError("Kubernetes readiness attempted a local subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable_docker)
    service, cache, state, daemon, performer, runtime = setup(tmp_path, [(True, logs(summary()))])
    passed, _ = await verify_env_cache_clean(state, "sym", performer, performer_mounts=())
    assert passed is False
    await freeze(service, state, daemon, performer)
    passed, _ = await verify_env_cache_clean(state, "sym", performer, performer_mounts=())
    assert passed is True
    assert runtime.specs[0].image == "trusted/global@sha256:" + "a" * 64
    cache.provenance_digest = None
    passed, _ = await verify_env_cache_clean(state, "sym", performer, performer_mounts=())
    assert passed is False
