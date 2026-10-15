from __future__ import annotations

import io
import json
import stat
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from performer import security_floor, security_scanner
from performer.models import Score, Stand
from performer.security_floor import run_security_floor

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
EXECUTABLE_MODE = 0o755
SOURCE_MODE = "100644"


@pytest.fixture
def head_tree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    tracked = {}
    calls = []

    def git(root_arg, *args):
        assert root_arg == root
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return HEAD.encode()
        if args[0] == "ls-tree":
            return b"".join(
                f"{SOURCE_MODE} blob {HEAD}\t{name}\0".encode() for name in tracked
            )
        assert args == ("archive", "--format=tar", HEAD)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as stream:
            for name, contents in tracked.items():
                data = contents.encode()
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                stream.addfile(entry, io.BytesIO(data))
        return archive.getvalue()

    with patch("performer.security_floor._git", side_effect=git, create=True):
        yield root, tracked, calls


def _score(files, *, head=HEAD):
    return Score(title="Card", repo_url="https://github.com/a/b", branch="feature", security_floor={
        "version": 1, "files": files, "diff_status": "ok", "head_sha": head,
    })


@pytest.fixture
def fake_scanners(tmp_path, monkeypatch):
    calls = tmp_path / "scanner-calls.jsonl"
    for tool in ("semgrep", "bandit"):
        binary = tmp_path / tool
        binary.write_text(f"#!{sys.executable}\n" + '''
import json
import pathlib
import sys

tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path.cwd()
target = root / args[-1]
text = target.read_text()
ignore_option = next((arg for arg in args if arg.startswith("--x-semgrepignore-filename=")), None)
ignore = root / (ignore_option.split("=", 1)[1] if ignore_option else ".semgrepignore")
hidden = (ignore.exists() and "app.py" in ignore.read_text()) if tool == "semgrep" else False
flag = "--disable-nosem" if tool == "semgrep" else "--ignore-nosec"
suppression = "nosemgrep" if tool == "semgrep" else "nosec"
hidden = hidden or (suppression in text and flag not in args)
row = {"check_id": "injection", "path": args[-1], "start": {"line": 1},
       "extra": {"severity": "ERROR"}} if tool == "semgrep" else {
       "test_id": "B602", "filename": args[-1], "line_number": 1,
       "issue_severity": "HIGH", "issue_confidence": "HIGH"}
record = {"root": str(root), "args": args, "text": text,
          "ignore": str(ignore), "ignore_text": ignore.read_text() if ignore.exists() else None,
          "repo_ignore_present": (root / ".semgrepignore").exists()}
with pathlib.Path(''' + repr(str(calls)) + ''').open("a") as stream:
    stream.write(json.dumps(record) + "\\n")
sys.stdout.write(json.dumps({"results": [] if hidden else [row], "errors": []}))
''')
        binary.chmod(EXECUTABLE_MODE)
    monkeypatch.setattr(security_scanner, "_resolve_binary", lambda tool: str(tmp_path / tool))
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("ignore_path", [".semgrepignore", "src/.semgrepignore"])
async def test_security_107_suppressions_cannot_hide_head_findings(head_tree, fake_scanners, ignore_path):
    root, tracked, calls = head_tree
    tracked.update({"src/app.py": "danger()  # nosec # nosemgrep\n", ignore_path: "app.py\n"})
    for name, contents in tracked.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    # The scan must read committed bytes, even if an earlier agent edited the checkout.
    (root / "src/app.py").write_text("clean_checkout()\n")
    report = await run_security_floor(_score(["src/app.py", ignore_path]), Stand(path=root, branch="feature"))
    assert report["complete"] is True
    assert len(report["findings"]) == 2
    invocations = [json.loads(line) for line in fake_scanners.read_text().splitlines()]
    assert len(invocations) == 2
    for invocation in invocations:
        export = Path(invocation["root"])
        assert not export.is_relative_to(root)
        assert invocation["text"] == tracked["src/app.py"]
        assert not invocation["repo_ignore_present"]
    assert "--disable-nosem" in invocations[0]["args"]
    assert invocations[0]["ignore_text"] == ""
    assert "--ignore-nosec" in invocations[1]["args"]
    assert ("archive", "--format=tar", HEAD) in calls
    assert not Path(invocations[0]["root"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [["deleted.py"], ["deleted.py", "app.py"]])
async def test_security_107_deletions_are_filtered_at_head(head_tree, files):
    root, tracked, _ = head_tree
    tracked["app.py"] = "print(1)\n"
    (root / "app.py").write_text(tracked["app.py"])
    # A stale checkout file must not turn a deletion into a scan target.
    (root / "deleted.py").write_text("stale()\n")
    with patch("performer.security_floor.scan_diff", return_value=[]) as scan:
        report = await run_security_floor(_score(files), Stand(path=root, branch="feature"))
    assert report["complete"] is True
    if files == ["deleted.py"]:
        assert report["reason"] == "no files to scan"
        scan.assert_not_called()
    else:
        assert scan.call_args.args[0] == ["app.py"]
        assert scan.call_args.args[1] != root


@pytest.mark.asyncio
async def test_security_107_head_mismatch_is_incomplete(head_tree):
    root, tracked, _ = head_tree
    tracked["app.py"] = "print(1)\n"
    (root / "app.py").write_text(tracked["app.py"])
    with patch("performer.security_floor.scan_diff", return_value=[]) as scan:
        report = await run_security_floor(_score(["app.py"], head=OTHER_HEAD), Stand(path=root, branch="feature"))
    assert report["complete"] is False
    scan.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["symlink", "directory", "export_ignore", "archive_failure"])
async def test_security_107_unscannable_head_files_fail_closed(head_tree, failure):
    root, tracked, _ = head_tree
    tracked["app.py"] = "danger()\n"
    original_git = security_floor._git

    def git(root_arg, *args):
        data = original_git(root_arg, *args)
        if args[0] == "ls-tree":
            if failure == "symlink":
                return data.replace(SOURCE_MODE.encode(), b"120000")
            if failure == "directory":
                return data.replace(b"app.py", b"app.py/child.py")
        if args[0] == "archive":
            if failure == "archive_failure":
                raise OSError("archive failed")
            if failure == "export_ignore":
                empty = io.BytesIO()
                with tarfile.open(fileobj=empty, mode="w"):
                    pass
                return empty.getvalue()
        return data

    with patch("performer.security_floor._git", side_effect=git), patch("performer.security_floor.scan_diff") as scan:
        report = await run_security_floor(_score(["app.py"]), Stand(path=root, branch="feature"))
    assert report["complete"] is False
    scan.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".", "../app.py", "/app.py", "src/../app.py", "bad\0path"])
async def test_security_107_invalid_paths_still_fail_closed(head_tree, name):
    root, _, _ = head_tree
    with patch("performer.security_floor.scan_diff") as scan:
        report = await run_security_floor(_score([name]), Stand(path=root, branch="feature"))
    assert report["complete"] is False
    scan.assert_not_called()


@pytest.mark.parametrize("tool", ["semgrep", "bandit"])
def test_security_107_workspace_scanners_are_never_resolved(tmp_path, monkeypatch, tool):
    binary = tmp_path / tool
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(EXECUTABLE_MODE)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    monkeypatch.setenv("PATH", str(tmp_path))
    with patch.object(Path, "is_file", return_value=False), pytest.raises(security_scanner.ScannerError):
        security_scanner._resolve_binary(tool)


@pytest.mark.parametrize("uid,mode,trusted", [
    (0, stat.S_IRUSR | stat.S_IXUSR, True),
    (1, stat.S_IRUSR | stat.S_IXUSR, False),
    (0, stat.S_IRUSR | stat.S_IXUSR | stat.S_IWGRP, False),
    (0, stat.S_IRUSR | stat.S_IXUSR | stat.S_IWOTH, False),
])
def test_security_107_image_scanner_must_be_root_owned_and_protected(uid, mode, trusted):
    with (
        patch.object(Path, "resolve", side_effect=lambda: Path("/usr/bin/semgrep")),
        patch.object(Path, "is_file", return_value=True),
        patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=uid, st_mode=mode)),
        patch("performer.security_scanner.os.access", return_value=True),
    ):
        assert security_scanner._trusted_executable(Path("/usr/bin/semgrep")) is trusted


def test_security_107_image_scanner_symlink_cannot_point_into_workspace(tmp_path):
    with patch.object(Path, "resolve", return_value=tmp_path / "semgrep"):
        assert not security_scanner._trusted_executable(Path("/usr/local/bin/semgrep"))
