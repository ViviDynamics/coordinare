"""Spec 234: the attacks a bootstrap performer can mount after the daemon verified its cache.

Each test freezes a verified cache, then plants one change the way a writer that outlives the
verification would, and checks that the daemon refuses it. The consumer-side attacks (a planted deb, a
swapped activate.sh, a services script added to the cache) are checked against the payload the daemon hands
a consumer; ``test_234_profile.py`` checks the same attacks against the profile itself.
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
    ProvenanceCheck,
    ProvenanceManifest,
    build_payload,
    check_cache,
    freeze_cache,
)

ACTIVATE = "# daemon-rendered activate\nexport PATH=$DEVENV/bin:$PATH\n"


def _verified_cache(tmp_path: Path) -> tuple[Path, tuple[Path, ...], ProvenanceManifest]:
    root = tmp_path / "caches"
    cache = root / "sym-abc"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "ruby").write_text("#!/bin/sh\necho ruby 3.4.2\n")
    (cache / "bin" / "ruby").chmod(0o755)
    (cache / "activate.sh").write_text(ACTIVATE)
    (cache / "debs").mkdir()
    (cache / "debs" / "libicu.deb").write_bytes(b"real-deb")
    mounts = (root,)
    manifest = freeze_cache(
        cache, symphony="sym", cache_key="k", activate_sh=ACTIVATE, performer_mounts=mounts,
    )
    return cache, mounts, manifest


def _rewrite_keeping_stat(path: Path, data: bytes) -> None:
    """Change bytes, keep the size, and put back the old mtime. A process can do this."""
    st = path.stat()
    path.write_bytes(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_file_changed_after_verification_refuses_dispatch(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified_cache(tmp_path)
    original = (cache / "bin" / "ruby").read_bytes()
    _rewrite_keeping_stat(cache / "bin" / "ruby", original.replace(b"echo", b"ECHO"))
    fast = check_cache(cache, manifest, full=False, performer_mounts=mounts)
    assert fast.ok is False
    assert "bin/ruby" in (fast.reason or "")
    full = check_cache(cache, manifest, full=True, performer_mounts=mounts)
    assert full.ok is False


def test_planted_deb_not_in_manifest_is_refused_and_never_listed(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified_cache(tmp_path)
    (cache / "debs" / "evil.deb").write_bytes(b"planted-fake-ruby")
    check = check_cache(cache, manifest, full=False, performer_mounts=mounts)
    assert check.ok is False
    assert "debs/evil.deb" in (check.reason or "")
    # The payload a consumer receives lists only the debs the manifest froze.
    payload = build_payload(manifest, cache_path="/devenv/sym-abc")
    assert set(payload["debs"]) == {"debs/libicu.deb"}


def test_swapped_activate_sh_is_refused_and_the_consumer_gets_the_rendered_text(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified_cache(tmp_path)
    (cache / "activate.sh").write_text("echo PWNED > /tmp/x\n")
    check = check_cache(cache, manifest, full=False, performer_mounts=mounts)
    assert check.ok is False
    assert "activate.sh" in (check.reason or "")
    payload = build_payload(manifest, cache_path="/devenv/sym-abc")
    assert payload["activate_sh"] == ACTIVATE


def test_services_start_added_to_the_cache_is_refused_and_not_handed_out(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified_cache(tmp_path)
    (cache / "services").mkdir()
    (cache / "services" / "services-start.sh").write_text("touch /tmp/pwned\n")
    check = check_cache(cache, manifest, full=False, performer_mounts=mounts)
    assert check.ok is False
    assert "services/services-start.sh" in (check.reason or "")
    payload = build_payload(manifest, cache_path="/devenv/sym-abc")
    assert payload["services_start_sh"] is None


def test_link_planted_after_verification_is_refused(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified_cache(tmp_path)
    os.symlink("/bin/sh", cache / "bin" / "ruby-shim")
    check = check_cache(cache, manifest, full=False, performer_mounts=mounts)
    assert check.ok is False
    assert "bin/ruby-shim" in (check.reason or "")


def test_link_retargeted_after_verification_is_refused(tmp_path: Path) -> None:
    cache, mounts, _manifest = _verified_cache(tmp_path)
    os.symlink("ruby", cache / "bin" / "rb")
    manifest = freeze_cache(
        cache, symphony="sym", cache_key="k", activate_sh=ACTIVATE, performer_mounts=mounts,
    )
    os.unlink(cache / "bin" / "rb")
    os.symlink("/bin/sh", cache / "bin" / "rb")
    check = check_cache(cache, manifest, full=False, performer_mounts=mounts)
    assert check.ok is False
    assert "bin/rb" in (check.reason or "")


def test_cache_bound_is_enforced_on_every_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, mounts, manifest = _verified_cache(tmp_path)
    monkeypatch.setattr(env_provenance, "PROVENANCE_MAX_ENTRIES", 2)
    check = check_cache(cache, manifest, full=False, performer_mounts=mounts)
    assert check.ok is False
    assert "entries" in (check.reason or "")


class _ProvenanceService:
    """Stands in for EnvCacheService: a real check against the frozen cache, and a recorded re-bootstrap."""

    def __init__(self, cache: Path, manifest: ProvenanceManifest, mounts: tuple[Path, ...]) -> None:
        self._cache = cache
        self._manifest = manifest
        self._mounts = mounts
        self.marked: list[str] = []

    async def check_provenance(self, symphony: str, state: EnvCacheState) -> ProvenanceCheck:
        return check_cache(self._cache, self._manifest, full=False, performer_mounts=self._mounts)

    def mark_runtime_health_failed(self, symphony: str, state: dict[str, Any]) -> None:
        self.marked.append(symphony)


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
    cache, mounts, manifest = _verified_cache(tmp_path)
    (cache / "bin" / "planted").write_text("x")
    service = _ProvenanceService(cache, manifest, mounts)
    released: list[bool] = []
    state: dict[str, Any] = {"env_cache_service": service}

    terminal = asyncio.run(
        dispatch_performer._env_provenance_gate(
            state, "card-1", "qa", "sym", _consumer_state(cache), False,
            lambda: released.append(True),
        ),
    )

    assert terminal is state, "a drifted cache refuses dispatch"
    assert released == [True], "the slot is released, not held"
    assert service.marked == ["sym"], "the next cycle re-bootstraps the current spec"


def test_dispatch_gate_lets_a_verified_cache_through(tmp_path: Path) -> None:
    cache, mounts, manifest = _verified_cache(tmp_path)
    service = _ProvenanceService(cache, manifest, mounts)
    released: list[bool] = []
    state: dict[str, Any] = {"env_cache_service": service}

    terminal = asyncio.run(
        dispatch_performer._env_provenance_gate(
            state, "card-1", "qa", "sym", _consumer_state(cache), False,
            lambda: released.append(True),
        ),
    )

    assert terminal is None
    assert released == []
    assert service.marked == []


def test_dispatch_gate_is_a_pass_through_for_the_bootstrap(tmp_path: Path) -> None:
    cache, _mounts, _manifest = _verified_cache(tmp_path)
    released: list[bool] = []
    terminal = asyncio.run(
        dispatch_performer._env_provenance_gate(
            {}, "card-1", "env_bootstrap", "sym", _consumer_state(cache), True,
            lambda: released.append(True),
        ),
    )
    assert terminal is None
    assert released == []
