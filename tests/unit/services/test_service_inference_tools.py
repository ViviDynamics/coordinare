"""Unit tests for spec 063 T010 read-only sandboxed tools."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from coordinare_service_inference.tools import SandboxViolation, ToolSandbox


@pytest.fixture()
def sandbox(tmp_path: Path) -> ToolSandbox:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")
    (tmp_path / "README.md").write_text("# project\n")
    return ToolSandbox.for_root(tmp_path)


class TestPathSandbox:
    def test_rejects_traversal(self, sandbox: ToolSandbox) -> None:
        with pytest.raises(SandboxViolation):
            sandbox.read_file("../etc/passwd")

    def test_rejects_absolute(self, sandbox: ToolSandbox) -> None:
        with pytest.raises(SandboxViolation):
            sandbox.read_file("/etc/passwd")

    def test_rejects_symlink_escape(self, sandbox: ToolSandbox, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret")
        link = tmp_path / "leak.txt"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        with pytest.raises(SandboxViolation):
            sandbox.read_file("leak.txt")

    def test_rejects_empty_path(self, sandbox: ToolSandbox) -> None:
        with pytest.raises(SandboxViolation):
            sandbox.read_file("")

    def test_for_root_requires_existing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ToolSandbox.for_root(tmp_path / "nonexistent")


class TestReadFile:
    def test_reads_text(self, sandbox: ToolSandbox) -> None:
        result = sandbox.read_file("src/main.py")
        assert result["exists"] is True
        assert result["content"] == "print('hi')\n"
        assert result["truncated"] is False

    def test_missing_returns_exists_false(self, sandbox: ToolSandbox) -> None:
        result = sandbox.read_file("nope.txt")
        assert result["exists"] is False

    def test_truncates_large_files(self, sandbox: ToolSandbox, tmp_path: Path) -> None:
        big = tmp_path / "big.txt"
        big.write_text("x" * (sandbox.read_max_bytes + 100))
        result = sandbox.read_file("big.txt")
        assert result["truncated"] is True
        assert len(result["content"]) == sandbox.read_max_bytes  # type: ignore[arg-type]

    def test_binary_file_flagged(self, sandbox: ToolSandbox, tmp_path: Path) -> None:
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01\xff\xfe")
        result = sandbox.read_file("blob.bin")
        assert result["binary"] is True
        assert "content" not in result


class TestListDir:
    def test_lists_entries(self, sandbox: ToolSandbox) -> None:
        result = sandbox.list_dir(".")
        names = {e["name"] for e in result["entries"]}  # type: ignore[union-attr]
        assert {"src", "README.md"}.issubset(names)

    def test_missing_dir_returns_exists_false(self, sandbox: ToolSandbox) -> None:
        result = sandbox.list_dir("nowhere")
        assert result["exists"] is False


class TestWhich:
    def test_rejects_path_separator(self, sandbox: ToolSandbox) -> None:
        with pytest.raises(SandboxViolation):
            sandbox.which("/bin/sh")

    def test_rejects_whitespace(self, sandbox: ToolSandbox) -> None:
        with pytest.raises(SandboxViolation):
            sandbox.which("foo bar")

    def test_finds_python(self, sandbox: ToolSandbox) -> None:
        result = sandbox.which("python3")
        # Some CI containers ship `python` not `python3`; either way the schema holds.
        assert result["binary"] == "python3"
        assert "found" in result


class TestProbeVersion:
    def test_missing_binary_returns_none(self, sandbox: ToolSandbox) -> None:
        result = sandbox.probe_version("definitely-not-installed-xyzzy")
        assert result["found"] is False
        assert result["version"] is None

    def test_probes_python(self, sandbox: ToolSandbox) -> None:
        import shutil
        if not shutil.which("python3"):
            pytest.skip("python3 unavailable")
        result = sandbox.probe_version("python3")
        assert result["found"] is True
        assert result["version"] and "Python" in result["version"]  # type: ignore[operator]


class TestGrepRepo:
    def test_finds_match(self, sandbox: ToolSandbox) -> None:
        result = sandbox.grep_repo("print", "src")
        matches = result["matches"]
        assert isinstance(matches, list) and len(matches) == 1
        assert matches[0]["file"] == "src/main.py"

    def test_invalid_regex_raises(self, sandbox: ToolSandbox) -> None:
        with pytest.raises(SandboxViolation):
            sandbox.grep_repo("(unclosed")

    def test_caps_matches(self, tmp_path: Path) -> None:
        for i in range(50):
            (tmp_path / f"f{i}.txt").write_text("needle\n" * 10)
        sb = ToolSandbox.for_root(tmp_path, grep_max_lines=20)
        result = sb.grep_repo("needle")
        assert len(result["matches"]) == 20  # type: ignore[arg-type]
        assert result["truncated"] is True

    def test_symlink_loop_does_not_hang(self, tmp_path: Path) -> None:
        """A self-referential symlink must not send the walker into an infinite loop."""
        (tmp_path / "f.txt").write_text("needle\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        # Symlink that points back at the parent — classic loop.
        (sub / "back").symlink_to(tmp_path, target_is_directory=True)
        sb = ToolSandbox.for_root(tmp_path)
        result = sb.grep_repo("needle")
        # Should resolve, find the single file, return; not hang.
        assert any(m["file"] == "f.txt" for m in result["matches"])  # type: ignore[index]


class TestWebSearch:
    def test_gated_off_by_default(self, sandbox: ToolSandbox) -> None:
        result = sandbox.web_search("rails postgres setup")
        assert result["enabled"] is False
        assert result["results"] == []

    def test_empty_query_rejected(self, sandbox: ToolSandbox) -> None:
        with pytest.raises(SandboxViolation):
            sandbox.web_search("   ")

    def test_enabled_returns_results_list(self, tmp_path: Path) -> None:
        sb = ToolSandbox.for_root(tmp_path, web_search_enabled=True)
        result = sb.web_search("anything")
        assert result["enabled"] is True
        assert isinstance(result["results"], list)
