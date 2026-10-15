"""Spec 265: a wiped devenv claim on Kubernetes must not be trusted from the daemon's own files.

The daemon's rendered activate.sh lives on the daemon's state volume. On Kubernetes that file says nothing about the
claim the consumer mounts. The Kubernetes consumer mount is therefore gated on the bootstrap's verified success, and
docker keeps the daemon-side check. The performer refuses a wiped claim before any work (see
agent/performer/tests/unit/test_265_wiped_claim.py), and that refusal re-bootstraps the cache.
"""

from __future__ import annotations

from pathlib import Path

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import (
    get_env_volume_for_symphony,
    get_kubernetes_consumer_volume,
)

RENDERED_ACTIVATE = "# AUTHORITATIVE activate.sh (daemon-rendered)\n"


def _state(cache: Path, *, succeeded: bool | None, ready: bool) -> EnvCacheState:
    return EnvCacheState(
        symphony_name="sym",
        sanitised_name="sym-abc",
        cache_dir=cache,
        rendered_activate_sh=RENDERED_ACTIVATE,
        last_bootstrap_succeeded=succeeded,
        cache_dir_ready=ready,
        provenance_digest="a" * 64 if succeeded else None,
        provenance_stat_digest="b" * 64 if succeeded else None,
        provenance_inputs={} if succeeded else None,
    )


def _daemon_copy(cache: Path) -> None:
    """The daemon's own rendered copy on the daemon's volume. It survives a wiped claim."""
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "activate.sh").write_text(RENDERED_ACTIVATE)


def test_kubernetes_mount_needs_the_verified_bootstrap_not_the_daemon_file(tmp_path: Path) -> None:
    cache = tmp_path / "daemon" / "sym-abc"
    _daemon_copy(cache)

    not_verified = {"sym": _state(cache, succeeded=False, ready=False)}
    verified = {"sym": _state(cache, succeeded=True, ready=True)}

    assert get_kubernetes_consumer_volume("sym", not_verified, performer_mounts=()) is None
    volume = get_kubernetes_consumer_volume("sym", verified, performer_mounts=(), container_devenv_root="/devenv")
    assert volume is not None
    assert volume[1] == "/devenv/sym-abc"
    assert volume[0].mode == "ro"


def test_a_wiped_claim_with_a_daemon_copy_is_granted_and_left_to_the_performer(tmp_path: Path) -> None:
    """The residual on Kubernetes: the daemon cannot see the claim, so it grants the mount. The performer refuses it.

    The daemon-side copy stands for the daemon's own state, which survives a wiped claim. Nothing on the daemon volume
    proves the claim still holds the toolchain, so the performer's check is the one that catches it.
    """
    cache = tmp_path / "daemon" / "sym-abc"
    _daemon_copy(cache)

    volume = get_kubernetes_consumer_volume(
        "sym", {"sym": _state(cache, succeeded=True, ready=True)}, performer_mounts=(),
    )

    assert volume is not None


def test_docker_keeps_the_daemon_side_check(tmp_path: Path) -> None:
    """Docker: the daemon and the performer share the directory, so the daemon's own check is the truth, as before."""
    cache = tmp_path / "daemon" / "sym-abc"
    cache.mkdir(parents=True)
    states = {"sym": _state(cache, succeeded=True, ready=True)}

    # No activate.sh in the directory the consumer would mount: refused, as before.
    assert get_env_volume_for_symphony(
        "sym", states, is_bootstrap=False, performer_mounts=(), container_devenv_root="/devenv",
    ) is None

    _daemon_copy(cache)
    assert get_env_volume_for_symphony(
        "sym", states, is_bootstrap=False, performer_mounts=(), container_devenv_root="/devenv",
    ) is not None
