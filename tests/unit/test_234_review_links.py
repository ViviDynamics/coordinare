"""Spec 234 review round: link containment resolves through the cache's own links.

The old check was lexical: ``a/q/L -> ../x/y/S/../../../..`` passes it, while the kernel resolves it outside the
cache, because ``a/x/y/S`` is itself a link. Each target is now resolved component by component, following the
links the manifest records, with a hop bound.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinare.services.env_provenance import ProvenanceRefusedError, ProvenanceScope, freeze_cache

ACTIVATE = "export X=1\n"


def _cache(tmp_path: Path) -> tuple[Path, tuple[Path, ...]]:
    root = tmp_path / "caches"
    cache = root / "sym-abc"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "ruby").write_text("#!/bin/sh\n")
    (cache / "activate.sh").write_text(ACTIVATE)
    return cache, (root,)


def _freeze(cache: Path, mounts: tuple[Path, ...]) -> None:
    freeze_cache(cache, symphony="sym", cache_key="k", activate_sh=ACTIVATE, scope=ProvenanceScope(mounts))


def test_the_reviewer_counterexample_is_refused(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "a" / "x" / "y").mkdir(parents=True)
    (cache / "a" / "q").mkdir()
    os.symlink("../../..", cache / "a" / "x" / "y" / "S")
    os.symlink("../x/y/S/../../../..", cache / "a" / "q" / "L")
    # The kernel resolves a/q/L outside the cache; the freeze must refuse it.
    with pytest.raises(ProvenanceRefusedError, match="outside the cache"):
        _freeze(cache, mounts)


def test_a_link_through_another_link_that_stays_inside_is_accepted(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "lib" / "deep").mkdir(parents=True)
    (cache / "lib" / "libssl.so.3").write_text("x")
    os.symlink("libssl.so.3", cache / "lib" / "libssl.so")
    os.symlink("../lib/libssl.so", cache / "bin" / "via")
    _freeze(cache, mounts)


def test_a_link_loop_is_refused_by_the_hop_bound(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.symlink("L2", cache / "bin" / "L1")
    os.symlink("L1", cache / "bin" / "L2")
    with pytest.raises(ProvenanceRefusedError, match="loop"):
        _freeze(cache, mounts)


def test_an_absolute_target_is_still_refused(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.symlink("/bin/sh", cache / "bin" / "sh")
    with pytest.raises(ProvenanceRefusedError, match="outside the cache"):
        _freeze(cache, mounts)
