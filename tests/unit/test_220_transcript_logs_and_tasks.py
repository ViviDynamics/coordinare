"""Spec 220 items 5, 6 and 7: transcript opens, ``prune_run_logs`` and the SIGUSR1 task dump.

Item 5: the token ledger opens transcripts through the mounted-file reader, so a link is refused.
Item 6: ``prune_run_logs`` is live (the run-log retention loop calls it), so a link at its root is
refused rather than followed into a host directory.
Item 7: the task dump goes to a private directory through ``O_NOFOLLOW``, never to a fixed name
in ``/tmp`` that another user could plant.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import pytest

from coordinare.__main__ import task_dump_directory, write_task_dump
from coordinare.services import run_transcript
from coordinare.services.mounted_file import open_mounted_file
from coordinare.services.run_log import prune_run_logs
from coordinare.services.run_transcript import prune_expired
from coordinare.services.token_usage import TokenLedger, _header_symphony, _scan_usage

if TYPE_CHECKING:
    from collections.abc import Callable

_HOST_TEXT = '{"usage": {"input_tokens": 999}}\n'


def _host_transcript(tmp: Path) -> Path:
    host = tmp / "host" / "run.jsonl"
    host.parent.mkdir()
    host.write_text(_HOST_TEXT)
    return host


def _guarded(root: Path) -> Callable[[Path], BinaryIO | None]:
    return lambda path: open_mounted_file(path, mounts=(root,))


def test_transcript_scan_refuses_a_link_inside_the_transcript_root(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    root.mkdir()
    host = _host_transcript(tmp_path)
    link = root / "run.jsonl"
    link.symlink_to(host)

    assert _scan_usage(link, _guarded(root)) is None


def test_transcript_scan_still_reads_a_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    root.mkdir()
    run = root / "run.jsonl"
    run.write_text('{"type": "usage", "usage": {"input_tokens": 3}}\n')

    scanned = _scan_usage(run, _guarded(root))

    assert scanned is not None
    records, _nbytes, _oversize = scanned
    assert len(records) == 1


def test_transcript_header_is_read_through_the_same_reader(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    root.mkdir()
    host = tmp_path / "host" / "header.jsonl"
    host.parent.mkdir()
    host.write_text('{"conductor": {"symphony": "planted"}}\n')
    link = root / "header.jsonl"
    link.symlink_to(host)

    assert _header_symphony(link, _guarded(root)) == ""


def test_the_ledger_rebuild_takes_the_guarded_reader(tmp_path: Path) -> None:
    root = tmp_path / "transcripts"
    (root / "sym" / "card").mkdir(parents=True)
    host = _host_transcript(tmp_path)
    (root / "sym" / "card" / "run.jsonl").symlink_to(host)

    ledger = TokenLedger()
    ledger.load_from_transcripts(root, now=datetime.now(UTC), open_file=_guarded(root))

    assert ledger.run_ids() == []


def test_prune_does_not_follow_a_link_at_the_root(tmp_path: Path) -> None:
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    old = host_dir / "run-old.log"
    old.write_text("host log\n")
    now = datetime.now(UTC)
    stale = (now - timedelta(days=90)).timestamp()
    os.utime(old, (stale, stale))
    root = tmp_path / "run-logs"
    root.symlink_to(host_dir)

    assert prune_run_logs(root, retention_days=30, now=now) == 0
    assert old.read_text() == "host log\n"


def _aged_run_file(path: Path, when: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("run\n")
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def test_prune_expired_does_not_descend_a_linked_root(tmp_path: Path) -> None:
    """Defense in depth (220 review): a link at the transcript root must delete nothing behind it."""
    now = datetime.now(UTC)
    host = tmp_path / "host-transcripts"
    old = host / "sym" / "card" / "run-old.jsonl"
    _aged_run_file(old, now - timedelta(days=90))
    root = tmp_path / "transcripts"
    root.symlink_to(host)

    assert prune_expired(root, retention_days=30, now=now) == 0
    assert old.exists()


def test_prune_expired_does_not_descend_a_linked_card(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    root = tmp_path / "transcripts"
    (root / "sym").mkdir(parents=True)
    host_card = tmp_path / "host-card"
    old = host_card / "run-old.jsonl"
    _aged_run_file(old, now - timedelta(days=90))
    (root / "sym" / "card").symlink_to(host_card)

    assert prune_expired(root, retention_days=30, now=now) == 0
    assert old.exists()


def test_prune_expired_removes_an_old_run_file_by_name(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    root = tmp_path / "transcripts"
    old = root / "sym" / "card" / "run-old.jsonl"
    _aged_run_file(old, now - timedelta(days=90))

    assert prune_expired(root, retention_days=30, now=now) == 1
    assert not old.exists()


def test_task_directory_is_made_on_first_use_only(tmp_path: Path) -> None:
    """The dump directory is not created at startup: the first SIGUSR1 makes it, later ones reuse it."""
    holder: list[Path] = []

    first = task_dump_directory(holder)
    second = task_dump_directory(holder)

    assert first == second
    assert first.is_dir()
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    assert holder == [first]
    first.rmdir()


def test_task_dump_writes_a_private_file(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)

    path = write_task_dump(private, "task dump\n")

    assert path.read_text() == "task dump\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_task_dump_refuses_a_planted_link(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    host = tmp_path / "host.txt"
    host.write_text("host\n")
    (private / "coordinare-tasks.log").symlink_to(host)

    with pytest.raises(OSError):
        write_task_dump(private, "task dump\n")

    assert host.read_text() == "host\n"


def test_symphony_moved_out_after_its_fd_is_open_deletes_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review BLOCK: a symphony renamed out of the root after its descriptor is open.

    The walk held a descriptor to the moved inode and unlinked its expired files, outside the
    root. Each level must still be at its name before anything under it is removed.
    """
    now = datetime.now(UTC)
    old = now - timedelta(days=90)
    root = tmp_path / "root"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _aged_run_file(root / "S1" / "C1" / "run-a.jsonl", old)
    _aged_run_file(root / "S1" / "C2" / "run-b.jsonl", old)
    real_layout = run_transcript._layout_names
    calls = {"n": 0}

    def move_symphony_then_list(dir_fd: int) -> list[str]:
        # Call 1 lists the root; call 2 lists the symphony, whose descriptor is already open.
        calls["n"] += 1
        if calls["n"] == 2:
            (root / "S1").rename(outside / "S1")
        return real_layout(dir_fd)

    monkeypatch.setattr(run_transcript, "_layout_names", move_symphony_then_list)

    assert prune_expired(root, retention_days=30, now=now) == 0
    assert (outside / "S1" / "C1" / "run-a.jsonl").exists()
    assert (outside / "S1" / "C2" / "run-b.jsonl").exists()


def test_card_moved_out_after_its_fd_is_open_deletes_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review BLOCK: a card renamed out of its symphony after its descriptor is open."""
    now = datetime.now(UTC)
    old = now - timedelta(days=90)
    root = tmp_path / "root"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _aged_run_file(root / "S1" / "C1" / "run-a.jsonl", old)
    _aged_run_file(root / "S1" / "C1" / "run-c.jsonl", old)
    real_expired = run_transcript._expired_at
    moved = {"done": False}

    def move_card_then_check(
        dir_fd: int, name: str, entry_path: Path, keep: frozenset[Path], older_than: float,
    ) -> bool:
        if not moved["done"]:
            moved["done"] = True
            entry_path.parent.rename(outside / "C1")
        return real_expired(dir_fd, name, entry_path, keep, older_than)

    monkeypatch.setattr(run_transcript, "_expired_at", move_card_then_check)

    assert prune_expired(root, retention_days=30, now=now) == 0
    assert (outside / "C1" / "run-a.jsonl").exists()
    assert (outside / "C1" / "run-c.jsonl").exists()
