"""Spec 132: the full run log writer, its root rules, retention and the read helpers.

The writer receives decoded-as-bytes chunks from a runtime, redacts each line, keeps
the head and a bounded tail under the cap, and commits the file atomically. These
tests pin the byte-level behaviour; the teardown wiring is tested separately.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coordinare.config import RunLogsConfig
from coordinare.services import run_log as rl
from coordinare.services.run_log import (
    RootGuard,
    RunLogRefusedError,
    RunLogWriter,
    iter_run_log_file,
    make_run_logs_collector,
    open_run_log,
    prune_run_logs,
    read_run_log_window,
    resolve_run_logs_root,
    run_log_path,
    run_log_size,
)

GH_TOKEN = "ghp_" + "c" * 36
EXACT_JOB_SECRET = "jobsecret-7f3a91c2"  # matches no pattern: only exact-value redaction removes it
RUN = "run-132-a"


def rl_size(root: Path, run_id: str) -> int | None:
    return run_log_size(root, run_id)


def _feed_in_pieces(writer: RunLogWriter, data: bytes, piece: int = 37) -> None:
    for start in range(0, len(data), piece):
        writer.feed(data[start:start + piece])


def _line(index: int) -> bytes:
    # 100 bytes including the newline, so byte arithmetic in the cap tests is exact.
    return f"line {index:05d} ".encode() + b"x" * 88 + b"\n"


def test_the_log_is_written_under_a_private_directory_and_file(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192)
    writer.feed(b"hello\n")

    assert writer.finish() is True

    assert run_log_path(root, RUN) == root / f"{RUN}.log"
    assert (root / f"{RUN}.log").read_bytes() == b"hello\n"
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / f"{RUN}.log").st_mode) == 0o600
    assert not (root / f"{RUN}.log.partial").exists()


def test_a_utf8_character_split_across_chunks_is_decoded_not_repr(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192)
    writer.feed(b"\xe2\x9c")  # the first two bytes of a check mark
    writer.feed(b"\x94 done\n")
    writer.finish()

    text = (root / f"{RUN}.log").read_text(encoding="utf-8")
    assert text == "✔ done\n"
    assert "b'" not in text
    assert "\\x" not in text


def test_lines_are_redacted_by_pattern_and_by_the_exact_job_secret(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192, secrets=(EXACT_JOB_SECRET,))
    writer.feed(f"clone with {GH_TOKEN} then use {EXACT_JOB_SECRET} now\n".encode())
    writer.finish()

    text = (root / f"{RUN}.log").read_text(encoding="utf-8")
    assert GH_TOKEN not in text
    assert EXACT_JOB_SECRET not in text
    assert "[REDACTED]" in text
    assert text.endswith("now\n")


def test_a_line_longer_than_the_limit_is_cut_so_memory_stays_bounded(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    writer = RunLogWriter(root, run_id=RUN, max_bytes=1024 * 1024)
    huge = b"a" * (rl.MAX_LINE_CHARS * 3)
    _feed_in_pieces(writer, huge, piece=rl.LOG_CHUNK_BYTES)
    writer.feed(b"\nafter\n")
    writer.finish()

    text = (root / f"{RUN}.log").read_text(encoding="utf-8")
    assert text.endswith("after\n")
    assert len(text) >= rl.MAX_LINE_CHARS * 3


def test_cap_keeps_the_head_and_the_tail_and_says_how_much_was_dropped(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    cap = 4096
    writer = RunLogWriter(root, run_id=RUN, max_bytes=cap)
    total = 2000 * 100
    _feed_in_pieces(writer, b"".join(_line(i) for i in range(2000)), piece=333)
    writer.finish()

    path = root / f"{RUN}.log"
    text = path.read_text(encoding="utf-8")
    assert path.stat().st_size <= cap
    assert text.startswith("line 00000 ")
    assert text.endswith("line 01999 " + "x" * 88 + "\n")
    assert writer.truncated is True
    assert writer.dropped_bytes > 0
    marker = [line for line in text.splitlines() if line.startswith("[truncated: ")]
    assert len(marker) == 1
    assert f"{writer.dropped_bytes} bytes dropped" in marker[0]
    # Head (1700) and tail (1700) are whole lines; everything between is counted.
    assert writer.dropped_bytes == total - 1700 - 1700


def test_nothing_is_marked_when_nothing_is_dropped(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    writer = RunLogWriter(root, run_id=RUN, max_bytes=4096)
    # Past the head budget but inside the tail budget: no content is dropped.
    _feed_in_pieces(writer, b"".join(_line(i) for i in range(30)))
    writer.finish()

    text = (root / f"{RUN}.log").read_text(encoding="utf-8")
    assert "[truncated" not in text
    assert writer.truncated is False
    assert writer.dropped_bytes == 0
    assert text.count("\n") == 30


def test_a_symlink_at_the_run_path_is_never_written_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    root = tmp_path / "run-logs"
    root.mkdir()
    os.symlink(outside, root / f"{RUN}.log")

    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192)
    writer.feed(b"secret line\n")

    assert writer.finish() is False
    assert writer.failed is True
    assert outside.read_text() == "keep\n"


def test_a_hard_link_at_the_run_path_is_never_written_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    root = tmp_path / "run-logs"
    root.mkdir()
    os.link(outside, root / f"{RUN}.log")

    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192)
    writer.feed(b"secret line\n")

    assert writer.finish() is False
    assert outside.read_text() == "keep\n"


def test_a_planted_partial_file_link_is_refused_by_the_exclusive_create(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    root = tmp_path / "run-logs"
    root.mkdir()
    os.symlink(outside, root / f"{RUN}.log.partial")

    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192)
    writer.feed(b"secret line\n")

    assert writer.finish() is False
    assert outside.read_text() == "keep\n"


def test_an_existing_final_log_is_never_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    root.mkdir()
    (root / f"{RUN}.log").write_text("first run\n")

    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192)
    writer.feed(b"second\n")

    assert writer.finish() is False
    assert (root / f"{RUN}.log").read_text() == "first run\n"


def test_a_root_that_cannot_be_created_fails_the_run_log_without_raising(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file\n")

    writer = RunLogWriter(blocker, run_id=RUN, max_bytes=8192)
    writer.feed(b"hello\n")  # must not raise

    assert writer.finish() is False
    assert writer.failed is True


def test_feeding_after_finish_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192)
    writer.feed(b"one\n")
    assert writer.finish() is True
    writer.feed(b"two\n")
    assert writer.finish() is False
    assert (root / f"{RUN}.log").read_text() == "one\n"


def test_abort_removes_the_partial_file(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192)
    writer.feed(b"partial\n")
    writer.abort()

    assert not (root / f"{RUN}.log.partial").exists()
    assert not (root / f"{RUN}.log").exists()


def test_an_unsafe_run_id_never_becomes_a_path(tmp_path: Path) -> None:
    assert run_log_path(tmp_path, "../../etc/passwd") is None
    assert run_log_path(tmp_path, "..") is None
    assert run_log_path(tmp_path, "") is None
    assert run_log_path(tmp_path, "a/b") is None
    assert run_log_path(tmp_path, ".hidden") is None
    assert run_log_path(tmp_path, "run-1") == tmp_path / "run-1.log"


def test_the_default_root_is_under_the_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert resolve_run_logs_root(None, config_dir=tmp_path, state_dir=state) == state / "run-logs"
    assert resolve_run_logs_root(Path("/var/lib/x"), config_dir=tmp_path, state_dir=state) == Path(
        "/var/lib/x",
    )
    assert resolve_run_logs_root(Path("rel/logs"), config_dir=tmp_path, state_dir=state) == (
        tmp_path / "rel" / "logs"
    )


def test_a_root_inside_or_equal_to_a_performer_mount_disables_run_logs(tmp_path: Path) -> None:
    mount = tmp_path / "performer-logs"
    mount.mkdir()

    inside = make_run_logs_collector(
        enabled=True, root=mount / "run-logs", max_bytes=8192, retention_days=30,
        guard=RootGuard(performer_mounts=(mount,)),
    )
    equal = make_run_logs_collector(
        enabled=True, root=mount, max_bytes=8192, retention_days=30,
        guard=RootGuard(performer_mounts=(mount,)),
    )
    assert inside.enabled is False
    assert equal.enabled is False


def test_a_performer_mount_inside_the_root_disables_run_logs(tmp_path: Path) -> None:
    root = tmp_path / "state" / "run-logs"
    mount = root / "performer"
    collector = make_run_logs_collector(
        enabled=True, root=root, max_bytes=8192, retention_days=30,
        guard=RootGuard(performer_mounts=(mount,)),
    )
    assert collector.enabled is False


def test_the_root_may_sit_inside_the_state_directory_but_not_be_it(tmp_path: Path) -> None:
    state = tmp_path / "state"
    allowed = make_run_logs_collector(
        enabled=True, root=state / "run-logs", max_bytes=8192, retention_days=30,
        guard=RootGuard(protected=(state,)),
    )
    refused = make_run_logs_collector(
        enabled=True, root=state, max_bytes=8192, retention_days=30,
        guard=RootGuard(protected=(state,)),
    )
    assert allowed.enabled is True
    assert refused.enabled is False


def test_a_disabled_collector_stays_disabled(tmp_path: Path) -> None:
    collector = make_run_logs_collector(
        enabled=False, root=tmp_path / "run-logs", max_bytes=8192, retention_days=30,
        guard=RootGuard(),
    )
    assert collector.enabled is False


def test_the_collector_hands_out_writers_with_the_run_secrets(tmp_path: Path) -> None:
    collector = make_run_logs_collector(
        enabled=True, root=tmp_path / "run-logs", max_bytes=8192, retention_days=30,
        guard=RootGuard(),
    )
    writer = collector.writer_for(run_id=RUN, secrets=(EXACT_JOB_SECRET,))
    writer.feed(f"value {EXACT_JOB_SECRET}\n".encode())
    writer.finish()

    assert "[REDACTED]" in (tmp_path / "run-logs" / f"{RUN}.log").read_text()


def test_read_window_pages_through_the_file(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    writer = RunLogWriter(root, run_id=RUN, max_bytes=8192)
    writer.feed(b"0123456789abcdef")
    writer.finish()

    first = read_run_log_window(root, RUN, offset=0, limit=10)
    assert first is not None
    assert first.text == "0123456789"
    assert first.size == 16
    assert first.next_offset == 10

    last = read_run_log_window(root, RUN, offset=10, limit=1000)
    assert last is not None
    assert last.text == "abcdef"
    assert last.next_offset is None


def test_read_window_is_none_for_a_missing_or_unsafe_run(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    root.mkdir()
    assert read_run_log_window(root, "missing", offset=0, limit=10) is None
    assert read_run_log_window(root, "../escape", offset=0, limit=10) is None
    assert open_run_log(root, "../escape") is None


def test_a_linked_run_log_is_refused_on_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n")
    root = tmp_path / "run-logs"
    root.mkdir()
    os.symlink(outside, root / f"{RUN}.log")
    with pytest.raises(RunLogRefusedError):
        read_run_log_window(root, RUN, offset=0, limit=10)

    os.unlink(root / f"{RUN}.log")
    os.link(outside, root / f"{RUN}.log")
    with pytest.raises(RunLogRefusedError):
        open_run_log(root, RUN)


def test_iter_run_log_streams_the_whole_file(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    writer = RunLogWriter(root, run_id=RUN, max_bytes=1024 * 1024)
    body = b"".join(_line(i) for i in range(500))
    writer.feed(body)
    writer.finish()

    assert rl_size(root, RUN) == len(body)
    assert b"".join(iter_run_log_file(root, RUN)) == body


def test_prune_removes_only_old_run_logs_in_the_writers_layout(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    root.mkdir()
    now = datetime.now(UTC)
    old = (now - timedelta(days=40)).timestamp()
    outside = tmp_path / "outside.log"
    outside.write_text("keep\n")
    os.utime(outside, (old, old))

    (root / "run-old.log").write_text("old\n")
    os.utime(root / "run-old.log", (old, old))
    (root / "run-young.log").write_text("young\n")
    (root / "run-stale.log.partial").write_text("half\n")
    os.utime(root / "run-stale.log.partial", (old, old))
    (root / "bad name.log").write_text("not ours\n")
    os.utime(root / "bad name.log", (old, old))
    os.symlink(outside, root / "run-link.log")
    (root / "notes.txt").write_text("keep\n")
    os.utime(root / "notes.txt", (old, old))

    removed = prune_run_logs(root, retention_days=30, now=now)

    assert removed == 2
    assert not (root / "run-old.log").exists()
    assert not (root / "run-stale.log.partial").exists()
    assert (root / "run-young.log").exists()
    assert (root / "bad name.log").exists()
    assert (root / "notes.txt").exists()
    assert (root / "run-link.log").is_symlink()
    assert outside.read_text() == "keep\n"


def test_prune_is_a_no_op_when_retention_is_zero(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    root.mkdir()
    (root / "run-old.log").write_text("old\n")
    os.utime(root / "run-old.log", (1.0, 1.0))
    assert prune_run_logs(root, retention_days=0) == 0
    assert (root / "run-old.log").exists()


def test_run_logs_config_defaults_and_the_dir_alias() -> None:
    defaults = RunLogsConfig()
    assert defaults.enabled is True
    assert defaults.retention_days == 30
    assert defaults.max_bytes == 16 * 1024 * 1024
    assert defaults.directory is None

    custom = RunLogsConfig.model_validate({"dir": "/srv/run-logs", "max_bytes": 8192})
    assert str(custom.directory) == "/srv/run-logs"
    assert custom.max_bytes == 8192
