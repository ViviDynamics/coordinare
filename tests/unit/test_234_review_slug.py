"""Spec 234 review round: the provenance slug accepts every sanitised symphony name, on both sides.

``sanitise_symphony_name`` keeps a leading underscore and does not cap the length, so the performer model's old
pattern refused real symphonies. The slug is now derived from the cache directory name by one function, which the
daemon and the performer each implement. The parity test pins them to the same answers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from performer.env_provenance import ProvenanceMaterialiseError, materialise
from performer.env_provenance import provenance_slug as perf_slug
from performer.server.models import EnvProvenance
from pydantic import ValidationError

from coordinare.services.env_cache import sanitise_symphony_name
from coordinare.services.env_provenance import ProvenanceManifest, build_payload, provenance_slug

DIGEST = "0" * 64
NAMES = [
    "plain",
    "Plain Name With Spaces",
    "_scratch-pad",
    "x" * 200,
    "--leading-dashes--",
    "../../etc/passwd",
    "unicode-é-name",
]


@pytest.mark.parametrize("name", NAMES)
def test_daemon_and_performer_derive_the_same_slug(name: str) -> None:
    cache_dir_name = sanitise_symphony_name(name)
    assert provenance_slug(cache_dir_name) == perf_slug(cache_dir_name)


@pytest.mark.parametrize("name", NAMES)
def test_every_slug_is_accepted_by_the_performer_model(name: str) -> None:
    slug = provenance_slug(sanitise_symphony_name(name))
    EnvProvenance(slug=slug, cache_path="/devenv/" + slug, activate_sh="", debs={}, digest=DIGEST)


def test_a_leading_underscore_is_accepted() -> None:
    slug = provenance_slug("_scratch-pad-0a1b2c")
    assert slug == "_scratch-pad-0a1b2c"
    EnvProvenance(slug=slug, cache_path="/devenv/" + slug, activate_sh="", debs={}, digest=DIGEST)


def test_a_two_hundred_character_name_keeps_a_bounded_slug_and_a_hash() -> None:
    cache_dir_name = sanitise_symphony_name("x" * 200)
    slug = provenance_slug(cache_dir_name)
    assert len(slug) <= 120
    assert slug != cache_dir_name, "a truncated name is distinguished by its hash"
    assert slug.endswith(provenance_slug(cache_dir_name)[-8:])


def test_the_performer_still_refuses_traversal_in_the_slug() -> None:
    with pytest.raises(ValidationError):
        EnvProvenance(slug="../escape", cache_path="/devenv/x", activate_sh="", debs={}, digest=DIGEST)
    with pytest.raises(ValidationError):
        EnvProvenance(slug="a/b", cache_path="/devenv/x", activate_sh="", debs={}, digest=DIGEST)


def test_materialise_refuses_a_slug_that_does_not_match_the_cache_path(tmp_path: Path) -> None:
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    payload = EnvProvenance(slug="other-abc", cache_path=str(cache), activate_sh="", debs={}, digest=DIGEST)
    with pytest.raises(ProvenanceMaterialiseError, match="slug"):
        materialise(payload, root=tmp_path / "prov")


def test_the_daemon_payload_carries_the_derived_slug() -> None:
    cache_dir_name = sanitise_symphony_name("_scratch-pad")
    manifest = ProvenanceManifest(
        slug=cache_dir_name,
        symphony="_scratch-pad",
        cache_key="k",
        frozen_at=datetime.now(UTC),
        activate_sh="",
        entries=[],
    )
    payload = build_payload(manifest, cache_path="/devenv/" + cache_dir_name, digest=DIGEST)
    assert payload["slug"] == provenance_slug(cache_dir_name)
