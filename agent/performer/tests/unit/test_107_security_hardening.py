from __future__ import annotations

import json
import stat
import sys
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
MANY_FILE_COUNT = 128
SCANNER_COUNT = 2
CONTROL_PATHS = (
    ".semgrepignore", ".gitignore", ".bandit", "src/.semgrepignore",
    ".semgrep.yml", ".semgrep.yaml", "bandit.yaml", "pyproject.toml", "setup.cfg",
    ".gitattributes", ".git/config", "src/.gitignore/child.py",
)
INSTALLATION_MESSAGE = (
    "Install semgrep and bandit system-wide as root-owned executables in "
    "/usr/local/bin or /usr/bin, or use the container transport."
)


@pytest.fixture
def head_tree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    tracked = {}
    calls = []
    destinations = []
    original_run = security_floor.subprocess.run
    original_git = security_floor._git

    def show(cmd, **kwargs):
        if cmd[:2] != [security_floor._GIT, "show"]:
            return original_run(cmd, **kwargs)
        assert kwargs["cwd"] == root
        assert kwargs["check"]
        assert kwargs["stderr"] == security_floor.subprocess.PIPE
        assert kwargs["timeout"] == security_floor._GIT_TIMEOUT_SECONDS
        assert cmd[:2] == [security_floor._GIT, "show"]
        assert all(destination.closed for destination in destinations)
        stdout = kwargs["stdout"]
        destinations.append(stdout)
        assert not stdout.closed
        name = cmd[-1].removeprefix(f"{HEAD}:")
        stdout.write(tracked[name].encode())
        calls.append(("show", cmd[-1]))
        return SimpleNamespace(stdout=None)

    def git(root_arg, *args, **kwargs):
        assert root_arg == root
        if args[0] == "show":
            return original_git(root_arg, *args, **kwargs)
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return HEAD.encode()
        assert args == ("ls-tree", "-r", "-z", HEAD)
        return b"".join(
            f"{SOURCE_MODE} blob {HEAD}\t{name}\0".encode() for name in tracked
        )

    with (
        patch("performer.security_floor._git", side_effect=git),
        patch("performer.security_floor.subprocess.run", side_effect=show),
    ):
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
target = next(path for arg in args if (path := root / arg).is_file() and "danger()" in path.read_text())
text = target.read_text()
ignore = root / ".semgrepignore"
hidden = (ignore.exists() and "app.py" in ignore.read_text()) if tool == "semgrep" else False
flag = "--disable-nosem" if tool == "semgrep" else "--ignore-nosec"
suppression = "nosemgrep" if tool == "semgrep" else "nosec"
hidden = hidden or (suppression in text and flag not in args)
row = {"check_id": "injection", "path": target.name, "start": {"line": 1},
       "extra": {"severity": "ERROR"}} if tool == "semgrep" else {
       "test_id": "B602", "filename": str(target), "line_number": 1,
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
    assert len(report["findings"]) == SCANNER_COUNT
    assert all(finding["file"] == "src/app.py" for finding in report["findings"])
    invocations = [json.loads(line) for line in fake_scanners.read_text().splitlines()]
    assert len(invocations) == SCANNER_COUNT
    for invocation in invocations:
        export = Path(invocation["root"])
        assert not export.is_relative_to(root)
        assert invocation["text"] == tracked["src/app.py"]
        assert not invocation["repo_ignore_present"]
    assert "--disable-nosem" in invocations[0]["args"]
    assert invocations[0]["ignore_text"] is None
    assert not any(arg.startswith("--x-semgrepignore-filename") for arg in invocations[0]["args"])
    assert "--ignore-nosec" in invocations[1]["args"]
    assert ("show", f"{HEAD}:src/app.py") in calls
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
        assert len(scan.call_args.args[0]) == len(tracked)
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
@pytest.mark.parametrize("failure", ["symlink", "directory", "show_failure"])
async def test_security_107_unscannable_head_files_fail_closed(head_tree, failure):
    root, tracked, _ = head_tree
    tracked["app.py"] = "danger()\n"
    original_git = security_floor._git

    def git(root_arg, *args, **kwargs):
        data = original_git(root_arg, *args, **kwargs)
        if args[0] == "ls-tree":
            if failure == "symlink":
                return data.replace(SOURCE_MODE.encode(), b"120000")
            if failure == "directory":
                return data.replace(b"app.py", b"app.py/child.py")
        return data

    original_run = security_floor.subprocess.run

    def show(*args, **kwargs):
        if failure == "show_failure":
            raise OSError("show failed")
        return original_run(*args, **kwargs)

    with (
        patch("performer.security_floor._git", side_effect=git),
        patch("performer.security_floor.subprocess.run", side_effect=show),
        patch("performer.security_floor.scan_diff") as scan,
    ):
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


@pytest.mark.asyncio
async def test_security_107_semgrep_targets_are_explicit_files(head_tree):
    root, tracked, _ = head_tree
    tracked.update({"src/app.py": "danger()\n", "nested/deeper/other.py": "other()\n"})
    semgrep_targets = []

    def invoke(cmd, cwd, tool):
        if tool == "semgrep":
            targets = cmd[cmd.index("--no-git-ignore") + 1:]
            assert targets
            assert all((cwd / target).is_file() for target in targets)
            assert all(not (cwd / target).is_dir() for target in targets)
            assert not any(arg.startswith("--x-semgrepignore-filename") for arg in cmd)
            semgrep_targets.extend(targets)
        return {"results": []}

    with (
        patch.object(security_scanner, "_resolve_binary", side_effect=lambda tool: f"/usr/bin/{tool}"),
        patch.object(security_scanner, "_invoke", side_effect=invoke),
    ):
        report = await run_security_floor(_score(list(tracked)), Stand(path=root, branch="feature"))
    assert report["complete"] is True
    assert len(semgrep_targets) == len(tracked)


@pytest.mark.asyncio
async def test_security_107_export_contains_no_control_or_config_names(head_tree):
    root, tracked, _ = head_tree
    tracked.update(dict.fromkeys(CONTROL_PATHS, "danger()\n"))
    tracked["unchanged_config.yaml"] = "must not be exported\n"
    observed = []

    def scan(targets, export):
        paths = list(export.rglob("*"))
        assert all(path.is_file() for path in paths)
        assert set(paths) == {export / target for target in targets}
        assert all(path.name not in {Path(name).name for name in tracked} for path in paths)
        assert all(path.read_text() == "danger()\n" for path in paths)
        observed.extend(targets)
        return []

    with patch.object(security_floor, "scan_diff", side_effect=scan):
        report = await run_security_floor(_score(list(CONTROL_PATHS)), Stand(path=root, branch="feature"))
    assert report["complete"] is True
    assert len(observed) == len(CONTROL_PATHS)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".gitignore", ".bandit", ".semgrepignore"])
async def test_security_107_code_renamed_to_control_file_is_still_scanned(head_tree, name):
    root, tracked, _ = head_tree
    contents = "danger()\n"
    tracked[name] = contents
    observed = []

    def scan(targets, export):
        assert len(targets) == len(tracked)
        target = targets[0]
        assert target != name
        assert (export / target).read_text() == contents
        observed.append(target)
        return [{"file": target}]

    with patch.object(security_floor, "scan_diff", side_effect=scan):
        report = await run_security_floor(_score([name]), Stand(path=root, branch="feature"))
    assert report["complete"] is True
    assert report["findings"] == [{"file": name}]
    assert observed


@pytest.mark.asyncio
async def test_security_107_many_head_files_stream_one_at_a_time(head_tree):
    root, tracked, calls = head_tree
    tracked.update({f"src/file_{index}.py": f"value = {index}\n" for index in range(MANY_FILE_COUNT)})
    observed = []

    def scan(targets, export):
        observed.extend((export / target).read_text() for target in targets)
        assert all(Path(target).suffix == ".py" for target in targets)
        return []

    with patch.object(security_floor, "scan_diff", side_effect=scan):
        report = await run_security_floor(_score(list(tracked)), Stand(path=root, branch="feature"))
    assert report["complete"] is True
    # The fixture requires a live output file for each git show and verifies that
    # its predecessor is closed before the next target is read.
    assert [call for call in calls if call[0] == "show"] == [
        ("show", f"{HEAD}:{name}") for name in tracked
    ]
    assert not any(call[0] == "archive" for call in calls)
    assert observed == list(tracked.values())


@pytest.mark.parametrize("tool", ["semgrep", "bandit"])
def test_security_107_missing_trusted_scanner_explains_installation(tool):
    with (
        patch.object(security_scanner, "_trusted_executable", return_value=False),
        pytest.raises(security_scanner.ScannerError) as error,
    ):
        security_scanner._resolve_binary(tool)
    assert INSTALLATION_MESSAGE in str(error.value)
    assert tool in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_tool", ["semgrep", "bandit"])
async def test_security_107_missing_trusted_scanner_report_explains_installation(head_tree, missing_tool):
    root, tracked, _ = head_tree
    tracked["app.py"] = "danger()\n"
    with (
        patch.object(security_scanner, "_trusted_executable", side_effect=lambda path: path.name != missing_tool),
        patch.object(security_scanner, "_invoke", return_value={"results": []}),
    ):
        report = await run_security_floor(_score(list(tracked)), Stand(path=root, branch="feature"))
    assert report["complete"] is False
    assert report["findings"] == []
    assert INSTALLATION_MESSAGE in report["reason"]
