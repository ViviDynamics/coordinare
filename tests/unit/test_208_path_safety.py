"""Spec 208, PR #217 re-review: nested mounts, operator aliases, attempt paths, mount roots.

Each test reproduces one reviewer probe (see /tmp/r208/lead/) and asserts the host file the
performer pointed at is never read or written.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from coordinare import routing_config_service as rcs
from coordinare.attempt_log import AttemptLog
from coordinare.services.mounted_file import (
    mounted_file_refusal,
    read_mounted_file,
    replace_mounted_file,
)

HOST_TEXT = "HOST SECRET\n"
ROUTING_BODY = (
    "entries:\n"
    "  - backend: vllm\n"
    "    model: m1\n"
    "    target:\n"
    "      base_url: http://localhost:8000/v1\n"
    "      wire_format: openai\n"
    "      strategy: normalize\n"
    "      normalizers: [harmony_tool_calls]\n"
)
NEW_ENTRY = {
    "backend": "vllm",
    "model": "m2",
    "target": {
        "base_url": "http://localhost:8000/v1",
        "wire_format": "openai",
        "strategy": "normalize",
        "normalizers": ["harmony_tool_calls"],
    },
}


def _write_replace(path: Path, data: bytes, mounts: Any) -> None:
    """A write attempt. A refusal may raise; the test only cares that the host file is untouched."""
    try:
        replace_mounted_file(path, data, mounts=mounts)
    except OSError:
        return


# ---------------------------------------------------------------------------
# BLOCK 1: a nested mount root is redirected by the performer.
# ---------------------------------------------------------------------------


def test_nested_mount_renamed_and_linked_by_the_performer_is_refused(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    cache = base / "envcache"
    logs = cache / "logs"          # performer_log_dir lives inside env_cache_root
    victim = base / "host-home"
    logs.mkdir(parents=True)
    victim.mkdir()
    (victim / "attempt.jsonl").write_text(HOST_TEXT)
    (logs / "attempt.jsonl").write_text("real log\n")
    # The performer (mounted into env_cache_root) renames the nested mount and links it out.
    os.rename(logs, cache / "logs_moved")
    os.symlink(victim, logs)

    path = logs / "attempt.jsonl"
    mounts = [cache, logs]
    assert read_mounted_file(path, max_bytes=10**6, mounts=mounts) is None
    _write_replace(path, b"PERFORMER WROTE\n", mounts)
    assert (victim / "attempt.jsonl").read_text() == HOST_TEXT


# ---------------------------------------------------------------------------
# BLOCK 2: classification must follow an operator alias into a mount.
# ---------------------------------------------------------------------------


def test_operator_alias_into_a_mount_still_refuses_a_planted_leaf_link(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    mount = base / "mnt"
    mount.mkdir()
    victim = base / "victim"
    victim.mkdir()
    (victim / "secret.yaml").write_text("entries: []\n")
    alias = base / "alias"
    os.symlink(mount, alias)             # operator alias, lexically outside the mount
    os.symlink(victim / "secret.yaml", mount / "routing.yaml")   # performer's link, inside

    path = alias / "routing.yaml"
    mounts = [mount]
    assert read_mounted_file(path, max_bytes=10**6, mounts=mounts) is None
    _write_replace(path, b"PERFORMER WROTE\n", mounts)
    assert (victim / "secret.yaml").read_text() == "entries: []\n"


def test_operator_leaf_link_into_a_mount_is_resolved_and_refused(tmp_path: Path) -> None:
    """An operator-side link whose target is a performer-planted link inside a mount."""
    base = tmp_path.resolve()
    mount = base / "mnt"
    mount.mkdir()
    victim = base / "victim"
    victim.mkdir()
    (victim / "secret.yaml").write_text("entries: []\n")
    os.symlink(victim / "secret.yaml", mount / "routing.yaml")
    operator = base / "operator"
    operator.mkdir()
    os.symlink(mount / "routing.yaml", operator / "routing.yaml")

    path = operator / "routing.yaml"
    mounts = [mount]
    assert read_mounted_file(path, max_bytes=10**6, mounts=mounts) is None


# ---------------------------------------------------------------------------
# BLOCK 3: a persisted attempt path under a performer-writable dir is not reused.
# ---------------------------------------------------------------------------


def test_persisted_attempt_path_under_a_performer_dir_is_not_reused(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    perf = base / "perf-logs"
    (perf / "attempts").mkdir(parents=True)
    state = base / "state" / "logs" / "attempts"
    state.mkdir(parents=True)
    victim = base / "home-dir"
    victim.mkdir()
    name = datetime.now(UTC).strftime("%Y-%m-%d") + ".jsonl"
    persisted = perf / "attempts" / name
    # Performer swaps the attempts directory for a link to a host directory.
    os.rename(perf / "attempts", base / "attempts-moved")
    os.symlink(victim, perf / "attempts")

    log = AttemptLog(log_dir=state)
    log.reopen_attempt("a1", "card1", persisted)
    assert log.log_path_for("a1") is not None
    assert Path(cast("str", log.log_path_for("a1"))).is_relative_to(state)
    log.close_attempt("a1", "pass", "daemon")
    assert os.listdir(victim) == []


def test_persisted_attempt_path_under_the_daemon_log_dir_is_still_reused(tmp_path: Path) -> None:
    """The restart case: the same daemon directory, so the end row lands in the saved file."""
    log_dir = tmp_path / "daemon" / "logs" / "attempts"
    first = AttemptLog(log_dir=log_dir)
    attempt_id = first.open_attempt("card-r", "default_policy")
    saved = Path(cast("str", first.log_path_for(attempt_id)))
    restored = AttemptLog(log_dir=log_dir)
    restored.reopen_attempt(attempt_id, "card-r", saved)
    restored.close_attempt(attempt_id, "pass", "human", "merged")
    assert len(saved.read_text().splitlines()) == 2


def test_persisted_attempt_path_with_dot_dot_is_not_reused(tmp_path: Path) -> None:
    log_dir = tmp_path / "daemon" / "logs" / "attempts"
    victim = tmp_path / "victim"
    victim.mkdir()
    crafted = log_dir / ".." / ".." / ".." / "victim" / "x.jsonl"
    log = AttemptLog(log_dir=log_dir)
    log.reopen_attempt("a2", "card2", crafted)
    log.close_attempt("a2", "pass", "daemon")
    assert os.listdir(victim) == []


# ---------------------------------------------------------------------------
# Follow-up: a mount root that is a file must refuse, not raise, on save.
# ---------------------------------------------------------------------------


def test_mount_root_that_is_a_file_refuses_without_raising(tmp_path: Path) -> None:
    mount_file = tmp_path / "mount-root"
    mount_file.write_text("not a directory\n")
    host_path = mount_file / "sub" / "routing.yaml"
    assert mounted_file_refusal(host_path, mounts=[mount_file]) is not None
    location = rcs.RoutingLocation(
        endpoint_id="e", host_path=host_path, container_path="/r.yaml", mounts=(mount_file,),
    )
    result = rcs.create_routing_entry(location, NEW_ENTRY, "sha256:" + "0" * 64)
    assert result.ok is False
    assert result.errors[0].code == "forbidden"
    assert rcs.read_routing(location).routing_available is False


# ---------------------------------------------------------------------------
# Second re-review: a state dir inside a performer mount disables attempt telemetry, and the
# live append refuses a link or a non-directory at the attempts directory itself.
# ---------------------------------------------------------------------------


def test_state_dir_inside_a_performer_mount_disables_attempt_telemetry(tmp_path: Path) -> None:
    """Probe /tmp/r208b/probe_resolve_state_in_mount.py: the state dir sits under performer_log_dir.

    The daemon must write nothing there. The performer renames the attempts directory and links
    it to a host directory; no row may reach the host."""
    from types import SimpleNamespace
    from typing import cast as _cast

    import coordinare.__main__ as app_main

    base = tmp_path.resolve()
    perf_logs = base / "performer-logs"
    state_file = perf_logs / "state" / "coordinare.state.json"
    state_file.parent.mkdir(parents=True)
    victim = base / "host-victim"
    victim.mkdir()
    cfg = SimpleNamespace(
        state_file_path=state_file,
        performer_log_dir=perf_logs,
        performer_endpoints=[],
        env_cache_root=base / "caches",
    )
    assert app_main._resolve_attempt_log_dir(_cast("Any", cfg), None) is None
    assert app_main._build_attempt_log(_cast("Any", cfg), None) is None
    assert list(victim.iterdir()) == []
    assert not any(perf_logs.rglob("*.jsonl"))


def test_a_link_at_the_attempts_directory_is_refused_on_append(tmp_path: Path) -> None:
    """The performer renames the daemon's attempts directory and links it out. The append must
    not follow the link, even though the directory was created by the daemon."""
    log_dir = tmp_path / "daemon" / "logs" / "attempts"
    log = AttemptLog(log_dir=log_dir)
    log.open_attempt(task_id="first", routing_reason="x")
    victim = tmp_path / "host-victim"
    victim.mkdir()
    os.rename(log_dir, tmp_path / "daemon" / "moved")
    os.symlink(victim, log_dir)
    aid = log.open_attempt(task_id="second", routing_reason="x")
    log.close_attempt(aid, "pass", "daemon")
    assert list(victim.iterdir()) == []


def test_a_file_at_the_attempts_directory_is_refused_on_append(tmp_path: Path) -> None:
    log_dir = tmp_path / "daemon" / "logs" / "attempts"
    log_dir.parent.mkdir(parents=True)
    log_dir.write_text("a file, not a directory\n")
    AttemptLog(log_dir=log_dir).open_attempt(task_id="x", routing_reason="x")
    assert log_dir.read_text() == "a file, not a directory\n"
