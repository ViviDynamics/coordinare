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

from coordinare.__main__ import write_task_dump
from coordinare.services.mounted_file import open_mounted_file
from coordinare.services.run_log import prune_run_logs
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
