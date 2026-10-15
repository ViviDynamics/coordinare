"""Spec 234 (performer side): the frozen steps are materialised from the payload, and only listed debs are read.

``materialise`` copies each deb the daemon listed out of the cache, hashes the bytes it read, and writes them only
on a match. A deb the manifest does not list is never opened. The payload model refuses a relative path that
escapes the cache, or a digest that is not a sha256.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from performer.env_provenance import (
    ProvenanceMaterialiseError,
    frozen_scripts_dir,
    materialise,
)
from performer.server.models import EnvProvenance

GOOD_DEB = b"real-deb-bytes"
DIGEST = hashlib.sha256(GOOD_DEB).hexdigest()


def _payload(cache: Path, *, debs: dict[str, str] | None = None, start: str | None = None) -> EnvProvenance:
    return EnvProvenance(
        slug="sym-abc",
        cache_path=str(cache),
        activate_sh="export PATH=$DEVENV/bin:$PATH\n",
        services_start_sh=start,
        services_health_sh=None,
        debs=debs if debs is not None else {"debs/libicu.deb": DIGEST},
        digest="f" * 64,
    )


def test_materialise_writes_the_frozen_scripts_and_the_listed_deb(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / "debs").mkdir(parents=True)
    (cache / "debs" / "libicu.deb").write_bytes(GOOD_DEB)
    root = tmp_path / "provenance"

    dest = materialise(_payload(cache, start="echo started\n"), root=root)

    assert dest == root / "sym-abc"
    assert (dest / "activate.sh").read_text() == "export PATH=$DEVENV/bin:$PATH\n"
    assert (dest / "services" / "services-start.sh").read_text() == "echo started\n"
    assert (dest / "debs" / "libicu.deb").read_bytes() == GOOD_DEB


def test_an_unlisted_deb_in_the_cache_is_never_copied(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / "debs").mkdir(parents=True)
    (cache / "debs" / "libicu.deb").write_bytes(GOOD_DEB)
    (cache / "debs" / "evil.deb").write_bytes(b"planted")
    root = tmp_path / "provenance"

    dest = materialise(_payload(cache), root=root)

    assert sorted(p.name for p in (dest / "debs").iterdir()) == ["libicu.deb"]


def test_a_deb_whose_bytes_changed_refuses_and_writes_nothing(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / "debs").mkdir(parents=True)
    (cache / "debs" / "libicu.deb").write_bytes(b"swapped-after-freeze")
    root = tmp_path / "provenance"

    with pytest.raises(ProvenanceMaterialiseError, match="libicu.deb"):
        materialise(_payload(cache), root=root)
    assert not (root / "sym-abc" / "debs" / "libicu.deb").exists()


def test_frozen_scripts_dir_follows_the_provenance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_DEVENV_PROVENANCE_DIR", "/var/lib/devenv-provenance")
    assert frozen_scripts_dir("/devenv/sym-abc") == Path("/var/lib/devenv-provenance/sym-abc")


def test_frozen_scripts_dir_is_the_cache_for_the_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("_DEVENV_PROVENANCE_DIR", raising=False)
    assert frozen_scripts_dir("/devenv/sym-abc") == Path("/devenv/sym-abc")


def test_a_deb_path_that_escapes_the_cache_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        EnvProvenance(
            slug="sym-abc",
            cache_path="/devenv/sym-abc",
            activate_sh="",
            debs={"debs/../../etc/passwd": DIGEST},
            digest="f" * 64,
        )


def test_a_digest_that_is_not_sha256_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        EnvProvenance(
            slug="sym-abc",
            cache_path="/devenv/sym-abc",
            activate_sh="",
            debs={"debs/libicu.deb": "not-a-digest"},
            digest="f" * 64,
        )
