"""Spec 234: the attacks a bootstrap performer can mount after the daemon verified its cache.

Each test freezes a verified cache, then plants one change the way a writer that outlives the verification would, and
checks that the daemon refuses it. The consumer-side attacks (a planted deb, a swapped activate.sh, a services script
added to the cache) are checked against the payload the daemon hands a consumer. ``test_234_profile.py`` checks the
same attacks against the profile itself.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from coordinare.graph.nodes import dispatch_performer
from coordinare.models.env_cache import EnvCacheState
from coordinare.services import env_provenance
from coordinare.services.env_provenance import (
    ContentResult,
    ManifestFile,
    ProvenanceLimits,
    ProvenanceRefusedError,
    ProvenanceScope,
    build_payload,
    freeze_cache,
    open_manifest,
    stat_digest_of,
    verify_content,
)

ACTIVATE = "# daemon-rendered activate\nexport PATH=$DEVENV/bin:$PATH\n"


def _verified(tmp_path: Path) -> tuple[Path, tuple[Path, ...], ManifestFile]:
    root = tmp_path / "caches"
    cache = root / "sym-abc"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "ruby").write_text("#!/bin/sh\necho ruby 3.4.2\n")
    (cache / "bin" / "ruby").chmod(0o755)
    (cache / "activate.sh").write_text(ACTIVATE)
    (cache / "debs").mkdir()
    (cache / "debs" / "libicu.deb").write_bytes(b"real-deb")
    mounts = (root,)
    out = tmp_path / "state" / "sym-abc.json"
    result = freeze_cache(
        cache, identity=("sym", "k"), activate_sh=ACTIVATE,
        scope=ProvenanceScope(mounts), out_path=out,
    )
    manifest = open_manifest(out, digest=result.manifest.digest, performer_mounts=mounts, max_bytes=10**9)
    assert manifest is not None
    return cache, mounts, manifest


def _content(cache: Path, mounts: tuple[Path, ...], manifest: ManifestFile) -> ContentResult:
    return verify_content(cache, manifest, scope=ProvenanceScope(mounts))


def _rewrite_keeping_mtime(path: Path, data: bytes) -> None:
    """Change bytes, keep the size, and put back the old mtime. A process can do this."""
    st = path.stat()
    path.write_bytes(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_file_changed_after_verification_refuses_dispatch(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified(tmp_path)
    before = stat_digest_of(cache, scope=ProvenanceScope(mounts))
    original = (cache / "bin" / "ruby").read_bytes()
    _rewrite_keeping_mtime(cache / "bin" / "ruby", original.replace(b"echo", b"ECHO"))
    assert stat_digest_of(cache, scope=ProvenanceScope(mounts)) != before, "ctime moved, so the stat digest moves"
    result = _content(cache, mounts, manifest)
    assert result.ok is False
    assert "bin/ruby" in (result.reason or "")


def test_planted_deb_not_in_manifest_is_refused_and_never_listed(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified(tmp_path)
    (cache / "debs" / "evil.deb").write_bytes(b"planted-fake-ruby")
    result = _content(cache, mounts, manifest)
    assert result.ok is False
    assert "debs/evil.deb" in (result.reason or "")
    payload = build_payload(manifest.meta, cache_path="/devenv/sym-abc", digest=manifest.digest)
    assert set(payload["debs"]) == {"debs/libicu.deb"}


def test_swapped_activate_sh_is_refused_and_the_consumer_gets_the_rendered_text(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified(tmp_path)
    (cache / "activate.sh").write_text("echo PWNED > /tmp/x\n")
    result = _content(cache, mounts, manifest)
    assert result.ok is False
    assert "activate.sh" in (result.reason or "")
    payload = build_payload(manifest.meta, cache_path="/devenv/sym-abc", digest=manifest.digest)
    assert payload["activate_sh"] == ACTIVATE


def test_services_start_added_to_the_cache_is_refused_and_not_handed_out(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified(tmp_path)
    (cache / "services").mkdir()
    (cache / "services" / "services-start.sh").write_text("touch /tmp/pwned\n")
    result = _content(cache, mounts, manifest)
    assert result.ok is False
    # The walk reports the first structural difference, here the added directory that holds the script.
    assert "services" in (result.reason or "")
    payload = build_payload(manifest.meta, cache_path="/devenv/sym-abc", digest=manifest.digest)
    assert payload["services_start_sh"] is None


def test_link_planted_after_verification_is_refused(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified(tmp_path)
    os.symlink("/bin/sh", cache / "bin" / "ruby-shim")
    result = _content(cache, mounts, manifest)
    assert result.ok is False
    assert "bin/ruby-shim" in (result.reason or "")


def test_link_retargeted_after_verification_is_refused(tmp_path: Path) -> None:
    cache, mounts = _tree_with_link(tmp_path)
    out = tmp_path / "state" / "rb.json"
    result = freeze_cache(
        cache, identity=("sym", "k"), activate_sh=ACTIVATE,
        scope=ProvenanceScope(mounts), out_path=out,
    )
    manifest = open_manifest(out, digest=result.manifest.digest, performer_mounts=mounts, max_bytes=10**9)
    assert manifest is not None
    os.unlink(cache / "bin" / "rb")
    os.symlink("/bin/sh", cache / "bin" / "rb")
    check = _content(cache, mounts, manifest)
    assert check.ok is False
    assert "bin/rb" in (check.reason or "")


def _tree_with_link(tmp_path: Path) -> tuple[Path, tuple[Path, ...]]:
    root = tmp_path / "caches"
    cache = root / "sym-abc"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "ruby").write_text("#!/bin/sh\n")
    (cache / "activate.sh").write_text(ACTIVATE)
    os.symlink("ruby", cache / "bin" / "rb")
    return cache, (root,)


def test_cache_bound_is_enforced_on_every_stat_walk(tmp_path: Path) -> None:
    cache, mounts, _manifest = _verified(tmp_path)
    tight = ProvenanceScope(mounts, ProvenanceLimits(max_entries=2, max_bytes=10**9))
    with pytest.raises(ProvenanceRefusedError, match="provenance_max_entries"):
        stat_digest_of(cache, scope=tight)


class _ProvenanceService:
    """Stands in for EnvCacheService: a real content check against the frozen cache, and a recorded re-bootstrap."""

    def __init__(self, cache: Path, manifest: ManifestFile, mounts: tuple[Path, ...]) -> None:
        self._cache = cache
        self._manifest = manifest
        self._mounts = mounts
        self.marked: list[str] = []

    async def resolve_provenance(
        self, symphony: str, state: EnvCacheState, *, container_path: str,
    ) -> tuple[Any, dict[str, Any] | None]:
        result = _content(self._cache, self._mounts, self._manifest)
        check = _Check(result.ok, result.reason)
        if not result.ok:
            return check, None
        return check, build_payload(self._manifest.meta, cache_path=container_path, digest=self._manifest.digest)

    def mark_runtime_health_failed(self, symphony: str, state: dict[str, Any]) -> None:
        self.marked.append(symphony)


class _Check:
    def __init__(self, ok: bool, reason: str | None) -> None:
        self.ok = ok
        self.reason = reason
        self.transient = False


def _consumer_state(cache: Path) -> EnvCacheState:
    return EnvCacheState(
        symphony_name="sym",
        sanitised_name="sym-abc",
        cache_dir=cache,
        last_bootstrap_succeeded=True,
        cache_dir_ready=True,
        readme_sha="k",
        last_seen_spec_sha="k",
        rendered_activate_sh=ACTIVATE,
    )


def test_dispatch_gate_refuses_a_drifted_cache_and_schedules_a_rebootstrap(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified(tmp_path)
    (cache / "bin" / "planted").write_text("x")
    service = _ProvenanceService(cache, manifest, mounts)
    released: list[bool] = []
    state: dict[str, Any] = {"env_cache_service": service}

    terminal, payload = asyncio.run(
        dispatch_performer._env_provenance_gate(
            state, "card-1", "qa", "sym", _consumer_state(cache), False,
            lambda: released.append(True), container_path="/devenv/sym-abc",
        ),
    )

    assert terminal is state, "a drifted cache refuses dispatch"
    assert payload is None
    assert released == [True], "the slot is released, not held"
    assert service.marked == ["sym"], "the next cycle re-bootstraps the current spec"


def test_dispatch_gate_lets_a_verified_cache_through(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified(tmp_path)
    service = _ProvenanceService(cache, manifest, mounts)
    released: list[bool] = []
    state: dict[str, Any] = {"env_cache_service": service}

    terminal, payload = asyncio.run(
        dispatch_performer._env_provenance_gate(
            state, "card-1", "qa", "sym", _consumer_state(cache), False,
            lambda: released.append(True), container_path="/devenv/sym-abc",
        ),
    )

    assert terminal is None
    assert payload is not None
    assert released == []
    assert service.marked == []


def test_dispatch_gate_is_a_pass_through_for_the_bootstrap(tmp_path: Path) -> None:
    cache, _mounts, _manifest = _verified(tmp_path)
    released: list[bool] = []
    terminal, payload = asyncio.run(
        dispatch_performer._env_provenance_gate(
            {}, "card-1", "env_bootstrap", "sym", _consumer_state(cache), True,
            lambda: released.append(True), container_path="",
        ),
    )
    assert terminal is None
    assert payload is None
    assert released == []


def test_the_module_no_longer_offers_a_manifest_in_memory() -> None:
    assert not hasattr(env_provenance, "ProvenanceManifest")
