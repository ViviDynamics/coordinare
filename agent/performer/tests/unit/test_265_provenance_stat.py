"""Consumers check the read-only cache before materialising activation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from coordinare_service_inference.cache_provenance import stat_digest

import performer.main as main
from performer.env_provenance import ProvenanceMaterialiseError, materialise
from performer.server.models import EnvProvenance, JobInitPayload


def test_a_stat_mismatch_refuses_before_any_script_is_written(tmp_path: Path) -> None:
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    (cache / "activate.sh").write_text("exit 99\n")
    provenance = EnvProvenance.model_validate({
        "slug": "sym-abc", "cache_path": str(cache), "digest": "a" * 64,
        "activate_sh": "exit 0\n", "stat_digest": "b" * 64,
        "max_entries": 100, "max_bytes": 1024,
    })
    root = tmp_path / "private"
    with pytest.raises(ProvenanceMaterialiseError, match="stat digest"):
        materialise(provenance, root=root)
    assert not root.exists()


def test_stat_match_allows_frozen_activation(tmp_path: Path) -> None:
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    (cache / "activate.sh").write_text("exit 99\n")
    # A wrong content in the cache is never copied as activation, even on a matching stat.
    provenance = EnvProvenance.model_validate({
        "slug": "sym-abc", "cache_path": str(cache), "digest": "a" * 64,
        "activate_sh": "exit 0\n", "stat_digest": stat_digest(cache, max_entries=100, max_bytes=1024),
    })
    dest = materialise(provenance, root=tmp_path / "private")
    assert (dest / "activate.sh").read_text() == "exit 0\n"


@pytest.mark.parametrize("empty", [True, False])
def test_admission_routes_stat_refusal_to_content_recheck(tmp_path: Path, empty: bool) -> None:
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    if not empty:
        (cache / "activate.sh").write_text("exit 99\n")
    payload = JobInitPayload(job_id="j", card_id="c", role="implementer", backend="claude_code", persona="p",
                             repo_url="https://github.com/org/repo.git", branch="b", env_cache_path=str(cache),
                             env_provenance=EnvProvenance(slug="sym-abc", cache_path=str(cache), digest="a" * 64,
                                                          stat_digest="b" * 64, activate_sh="exit 0\n"))
    refusal = main._admission_refusal(payload)
    assert refusal is not None
    code, text = refusal
    response = json.loads(text)
    assert code == ("env_cache_missing" if empty else "env_provenance_refused")
    assert response["env_cache_health_failed"] is True
    assert response.get("env_cache_stat_mismatch", False) is (not empty)


def test_v2_payload_without_stat_digest_refuses_materialisation(tmp_path: Path) -> None:
    provenance = EnvProvenance.model_validate({
        "version": 2, "slug": "sym-abc", "cache_path": str(tmp_path / "sym-abc"),
        "digest": "a" * 64, "activate_sh": "exit 0\n",
    })
    root = tmp_path / "private"
    with pytest.raises(ProvenanceMaterialiseError, match="stat digest"):
        materialise(provenance, root=root)
    assert not root.exists()
