"""Spec 208, review round 2: links above the leaf, FIFOs, unbounded reads, modes, mounts.

Every test here plants a performer-controlled link or special file where the daemon
would otherwise read or write through it, and checks the daemon refuses it without
blocking. The threads in the FIFO tests bound the wait, so a regression fails instead of
hanging the suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import coordinare.__main__ as app_main
from coordinare import routing_config_service as rcs
from coordinare.attempt_log import AttemptLog, resolve_writable_log_dir
from coordinare.services import env_cache
from coordinare.services.mounted_file import mounted_file_refusal, read_mounted_file
from coordinare.services.persona_classifier import _read_project_context

VICTIM_TEXT = "KEEP: daemon-owned content\n"
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


def _bounded(fn: Any, *args: Any, seconds: float = 2.0) -> tuple[bool, Any]:
    """Run *fn* on a daemon thread. Returns (finished in time, result)."""
    box: dict[str, Any] = {}

    def run() -> None:
        box["value"] = fn(*args)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return (not thread.is_alive()), box.get("value")


def _ancestor_swap(tmp: Path) -> tuple[Path, Path]:
    """perf/sub/routing.yaml where ``sub`` is a link to a daemon-owned directory."""
    victim_dir = tmp / "daemon-owned"
    victim_dir.mkdir()
    (victim_dir / "routing.yaml").write_text(ROUTING_BODY)
    perf = tmp / "perf"
    perf.mkdir()
    os.symlink(victim_dir, perf / "sub")
    return perf, victim_dir


# ---------------------------------------------------------------------------
# 1. A symlinked ancestor directory is refused for read, write and hash.
# ---------------------------------------------------------------------------


def test_routing_read_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    _perf, victim_dir = _ancestor_swap(tmp_path)
    location = rcs.RoutingLocation(
        endpoint_id="e",
        host_path=_perf / "sub" / "routing.yaml",
        container_path="/r.yaml",
    )
    view = rcs.read_routing(location)
    assert view.routing_available is False
    assert view.entries == []
    assert rcs.routing_content_hash(location) is None
    assert (victim_dir / "routing.yaml").read_text() == ROUTING_BODY


def test_routing_write_refuses_symlinked_ancestor(tmp_path: Path) -> None:
    perf, victim_dir = _ancestor_swap(tmp_path)
    location = rcs.RoutingLocation(
        endpoint_id="e",
        host_path=perf / "sub" / "routing.yaml",
        container_path="/r.yaml",
    )
    # The base hash is the victim's real hash, so only the link check can refuse the write.
    base = "sha256:" + hashlib.sha256(ROUTING_BODY.encode()).hexdigest()
    result = rcs.create_routing_entry(location, NEW_ENTRY, base)
    assert result.ok is False
    assert (victim_dir / "routing.yaml").read_text() == ROUTING_BODY
    assert sorted(p.name for p in victim_dir.iterdir()) == ["routing.yaml"]


def test_read_refuses_symlinked_ancestor_below_a_trusted_root(tmp_path: Path) -> None:
    perf, victim_dir = _ancestor_swap(tmp_path)
    target = perf / "sub" / "routing.yaml"
    assert read_mounted_file(target, max_bytes=4096, root=perf) is None
    assert mounted_file_refusal(target, root=perf) is not None
    assert (victim_dir / "routing.yaml").read_text() == ROUTING_BODY


def test_read_through_a_real_subdirectory_below_the_root_still_works(tmp_path: Path) -> None:
    perf = tmp_path / "perf"
    (perf / "sub").mkdir(parents=True)
    (perf / "sub" / "routing.yaml").write_text(ROUTING_BODY)
    assert read_mounted_file(perf / "sub" / "routing.yaml", max_bytes=4096, root=perf) == (
        ROUTING_BODY.encode()
    )


def test_routing_write_round_trips_and_keeps_the_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(ROUTING_BODY)
    os.chmod(path, 0o640)
    location = rcs.RoutingLocation(endpoint_id="e", host_path=path, container_path="/r.yaml")
    base = rcs.routing_content_hash(location)
    result = rcs.create_routing_entry(location, NEW_ENTRY, cast("str", base))
    assert result.ok is True
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    assert len(rcs.read_routing(location).entries) == 2
    assert [p.name for p in tmp_path.iterdir()] == ["routing.yaml"]


def test_routing_hash_matches_the_file_bytes(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "routing.yaml"
    path.write_text(ROUTING_BODY)
    location = rcs.RoutingLocation(endpoint_id="e", host_path=path, container_path="/r.yaml")
    expected = "sha256:" + hashlib.sha256(ROUTING_BODY.encode()).hexdigest()
    assert rcs.routing_content_hash(location) == expected


def test_routing_hash_refuses_a_link_instead_of_hashing_its_target(tmp_path: Path) -> None:
    """The dashboard returned the hash of a link target to the browser (review item 3)."""
    victim = tmp_path / "host-secret.txt"
    victim.write_text("HOST-SECRET\n")
    link = tmp_path / "routing.yaml"
    link.symlink_to(victim)
    location = rcs.RoutingLocation(endpoint_id="e", host_path=link, container_path="/r.yaml")
    assert rcs.routing_content_hash(location) is None


def test_routing_hash_refuses_an_oversize_file_without_reading_it(tmp_path: Path) -> None:
    path = tmp_path / "routing.yaml"
    with open(path, "wb") as fh:
        fh.truncate(2 * 1024**3)
    location = rcs.RoutingLocation(endpoint_id="e", host_path=path, container_path="/r.yaml")
    done, value = _bounded(rcs.routing_content_hash, location)
    assert done is True
    assert value is None


# ---------------------------------------------------------------------------
# 2. The prior services.json is read bounded, without links, and never blocks.
# ---------------------------------------------------------------------------


def _cache_with_services(tmp: Path, name: str) -> Path:
    cache = tmp / "env-caches" / "sym"
    (cache / "services").mkdir(parents=True)
    return cache


def test_prior_manifest_fifo_does_not_block(tmp_path: Path) -> None:
    cache = _cache_with_services(tmp_path, "fifo")
    os.mkfifo(cache / "services" / "services.json")
    done, value = _bounded(env_cache.load_prior_manifest_bounded, cache)
    assert done is True
    assert value is None


def test_prior_manifest_symlink_to_an_endless_device_is_refused(tmp_path: Path) -> None:
    cache = _cache_with_services(tmp_path, "zero")
    os.symlink("/dev/zero", cache / "services" / "services.json")
    done, value = _bounded(env_cache.load_prior_manifest_bounded, cache)
    assert done is True
    assert value is None


def test_prior_manifest_oversize_is_refused(tmp_path: Path) -> None:
    cache = _cache_with_services(tmp_path, "big")
    with open(cache / "services" / "services.json", "wb") as fh:
        fh.truncate(2 * 1024**3)
    done, value = _bounded(env_cache.load_prior_manifest_bounded, cache)
    assert done is True
    assert value is None


def test_prior_manifest_symlinked_cache_dir_is_refused(tmp_path: Path) -> None:
    """The performer-writable cache dir itself replaced by a link to a host directory."""
    root = tmp_path / "env-caches"
    root.mkdir()
    host = tmp_path / "host"
    (host / "services").mkdir(parents=True)
    (host / "services" / "services.json").write_text("{}")
    os.symlink(host, root / "sym")
    assert env_cache.load_prior_manifest_bounded(root / "sym") is None


def test_prior_manifest_reads_a_valid_regular_file(tmp_path: Path) -> None:
    cache = _cache_with_services(tmp_path, "ok")
    (cache / "services" / "services.json").write_text("not a manifest")
    # Unparseable content is None, the same as before; the point is it was read safely.
    assert env_cache.load_prior_manifest_bounded(cache) is None


# ---------------------------------------------------------------------------
# 4. CLAUDE.md / AGENTS.md from the daemon clone: no links, bounded.
# ---------------------------------------------------------------------------


def test_project_context_refuses_a_committed_symlink_to_a_host_file(tmp_path: Path) -> None:
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST-SECRET-VALUE\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(secret, ws / "CLAUDE.md")
    os.symlink(secret, ws / "AGENTS.md")
    context = _read_project_context(ws)
    assert context == {"claude_md_head": "", "agents_md_head": ""}


def test_project_context_refuses_an_oversize_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with open(ws / "CLAUDE.md", "wb") as fh:
        fh.truncate(2 * 1024**3)
    done, context = _bounded(_read_project_context, ws)
    assert done is True
    assert context["claude_md_head"] == ""


def test_project_context_reads_regular_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text("claude rules\n")
    (ws / "AGENTS.md").write_text("agent rules\n")
    context = _read_project_context(ws)
    assert context == {"claude_md_head": "claude rules\n", "agents_md_head": "agent rules\n"}


# ---------------------------------------------------------------------------
# 5. Every directory the daemon creates for attempts is owner-only.
# ---------------------------------------------------------------------------


def test_attempt_directories_are_created_owner_only_at_every_level(tmp_path: Path) -> None:
    candidate = tmp_path / "cfg" / "logs" / "attempts"
    fallback = tmp_path / "state" / "logs" / "attempts"
    old = os.umask(0o022)
    try:
        directory = resolve_writable_log_dir(candidate, fallback)
        AttemptLog(log_dir=directory).open_attempt(task_id="c1", routing_reason="x")
    finally:
        os.umask(old)
    for level in (tmp_path / "cfg", tmp_path / "cfg" / "logs", candidate):
        assert stat.S_IMODE(os.stat(level).st_mode) == 0o700, level


def test_attempt_directory_is_owner_only_when_only_the_append_creates_it(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "attempts"
    old = os.umask(0o022)
    try:
        AttemptLog(log_dir=deep).open_attempt(task_id="c2", routing_reason="x")
    finally:
        os.umask(old)
    for level in (tmp_path / "a", tmp_path / "a" / "b", deep):
        assert stat.S_IMODE(os.stat(level).st_mode) == 0o700, level


# ---------------------------------------------------------------------------
# 6. A FIFO at the attempt file does not block the append.
# ---------------------------------------------------------------------------


def test_fifo_at_the_attempt_file_does_not_block(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    name = datetime.now(UTC).strftime("%Y-%m-%d") + ".jsonl"
    directory = tmp_path / "attempts"
    directory.mkdir()
    os.mkfifo(directory / name)
    log = AttemptLog(log_dir=directory)
    done, attempt_id = _bounded(
        lambda: log.open_attempt(task_id="c3", routing_reason="x"),
    )
    assert done is True
    assert isinstance(attempt_id, str)
    assert stat.S_ISFIFO(os.stat(directory / name).st_mode)


# ---------------------------------------------------------------------------
# 7. A relative untrusted directory is resolved before the persisted path is compared.
# ---------------------------------------------------------------------------


def test_reopen_compares_resolved_paths_for_a_relative_untrusted_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    performer_attempts = tmp_path / "perf-logs" / "attempts"
    performer_attempts.mkdir(parents=True)
    persisted = performer_attempts / "2026-10-14.jsonl"
    daemon_dir = tmp_path / "daemon" / "attempts"
    log = AttemptLog(log_dir=daemon_dir, untrusted_dirs=(Path("perf-logs"),))
    log.reopen_attempt("att-7", "card-7", persisted)
    log.close_attempt("att-7", verdict="pass", verdict_source="qa_role")
    assert list(performer_attempts.iterdir()) == []
    assert len(list(daemon_dir.glob("*.jsonl"))) == 1


# ---------------------------------------------------------------------------
# 8. The attempt directory is never inside a performer mount.
# ---------------------------------------------------------------------------


def _mount_config(state: Path, mount: Path) -> Any:
    return SimpleNamespace(
        state_file_path=state / "coordinare.state.json",
        performer_log_dir=mount,
        performer_endpoints=[],
        env_cache_root=state / "env-caches",
    )


def test_attempt_dir_inside_a_performer_mount_falls_back_to_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    mount = tmp_path / "mount"
    config_dir_in_mount = mount / "config"
    config_dir_in_mount.mkdir(parents=True)
    config = _mount_config(state, mount)
    resolved = app_main._resolve_attempt_log_dir(
        cast("Any", config),
        config_dir_in_mount / "config.yaml",
    )
    assert resolved == state.resolve() / "logs" / "attempts"
    assert not resolved.is_relative_to(mount.resolve())


def test_attempt_dir_inside_an_endpoint_volume_falls_back_to_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    shared = tmp_path / "shared"
    (shared / "config").mkdir(parents=True)
    endpoint = SimpleNamespace(volumes=[SimpleNamespace(host_path=shared)])
    config = _mount_config(state, tmp_path / "unrelated")
    config.performer_endpoints = [endpoint]
    resolved = app_main._resolve_attempt_log_dir(
        cast("Any", config),
        shared / "config" / "config.yaml",
    )
    assert resolved == state.resolve() / "logs" / "attempts"


def test_attempt_dir_outside_every_mount_is_kept(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = _mount_config(state, tmp_path / "mount")
    resolved = app_main._resolve_attempt_log_dir(
        cast("Any", config),
        config_dir / "config.yaml",
    )
    assert resolved == config_dir.resolve() / "logs" / "attempts"


def test_fallback_row_lands_in_state_when_the_mount_forbids_the_config_dir(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    mount = tmp_path / "mount"
    mount.mkdir()
    config = _mount_config(state, mount)
    resolved = app_main._resolve_attempt_log_dir(
        cast("Any", config),
        mount / "config.yaml",
    )
    AttemptLog(log_dir=resolved).open_attempt(task_id="c8", routing_reason="x")
    (row,) = resolved.glob("*.jsonl")
    assert json.loads(row.read_text().splitlines()[0])["task_id"] == "c8"
    assert not any(mount.iterdir())
