"""Diffparse tests for reviewer workflow (spec 169 T006).

Parse unified diff into ChangedFile structures, detect truncation marker.
Mutation check: test must fail when truncation detection is removed.
"""
from __future__ import annotations

from performer.workflows.reviewer.diffparse import (
    detect_truncation,
    diff_lines,
    line_in_hunks,
    parse_unified_diff,
)
from performer.workflows.reviewer.models import ChangedFile, Hunk


class TestParseUnifiedDiff:
    """parse_unified_diff parses changed files and hunks."""

    def test_single_hunk_single_file(self):
        """Parse a diff with one file and one hunk."""
        diff = """diff --git a/src/foo.py b/src/foo.py
index 1234567..abcdefg 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -10,5 +10,7 @@
 def hello():
     print("before")
+    print("after")
 return 0
"""
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert files[0].path == "src/foo.py"
        assert len(files[0].hunks) == 1
        assert files[0].hunks[0].header == "@@ -10,5 +10,7 @@"
        assert files[0].hunks[0].start_line == 10
        assert files[0].hunks[0].end_line == 16

    def test_multiple_hunks_single_file(self):
        """Parse a diff with one file and multiple hunks."""
        diff = """diff --git a/src/foo.py b/src/foo.py
@@ -1,3 +1,4 @@
-old line 1
+new line 1
 context
@@ -10,3 +11,4 @@
 context
+new line
"""
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert len(files[0].hunks) == 2
        assert files[0].hunks[0].start_line == 1
        assert files[0].hunks[0].end_line == 4
        assert files[0].hunks[1].start_line == 11
        assert files[0].hunks[1].end_line == 14

    def test_multiple_files(self):
        """Parse a diff with two files."""
        diff = """diff --git a/src/foo.py b/src/foo.py
@@ -1,3 +1,3 @@
-old
+new
diff --git a/src/bar.py b/src/bar.py
@@ -5,2 +5,3 @@
+added
"""
        files = parse_unified_diff(diff)
        assert len(files) == 2
        assert files[0].path == "src/foo.py"
        assert files[1].path == "src/bar.py"

    def test_new_file(self):
        """Parse a new file (no a/ path)."""
        diff = """diff --git a/src/new.py b/src/new.py
new file mode 100644
@@ -0,0 +1,2 @@
+first line
+second line
"""
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert files[0].path == "src/new.py"

    def test_deleted_file(self):
        """Parse a deleted file."""
        diff = """diff --git a/src/deleted.py b/src/deleted.py
deleted file mode 100644
@@ -1,3 +0,0 @@
-line 1
-line 2
-line 3
"""
        files = parse_unified_diff(diff)
        assert len(files) == 1
        assert files[0].path == "src/deleted.py"

    def test_file_rename(self):
        """Parse a file rename."""
        diff = """diff --git a/src/old_name.py b/src/new_name.py
similarity index 100%
rename from src/old_name.py
rename to src/new_name.py
"""
        files = parse_unified_diff(diff)
        # The parser extracts the "b/" path, which is the new name
        assert len(files) == 1
        assert files[0].path == "src/new_name.py"

    def test_hunk_without_counts(self):
        """Parse a hunk header without count (defaults to 1)."""
        diff = """diff --git a/src/foo.py b/src/foo.py
@@ -10 +10 @@
-old
+new
"""
        files = parse_unified_diff(diff)
        assert files[0].hunks[0].start_line == 10
        # Without count, end_line = start_line + 1 - 1 = start_line
        assert files[0].hunks[0].end_line == 10

    def test_empty_diff(self):
        """Parse an empty diff."""
        files = parse_unified_diff("")
        assert files == []

    def test_hunk_content_lines(self):
        """Hunk lines include context (+) and removed (-)."""
        diff = """diff --git a/src/foo.py b/src/foo.py
@@ -1,4 +1,3 @@
 context
-removed
+added
 context
"""
        files = parse_unified_diff(diff)
        hunk_lines = files[0].hunks[0].lines
        assert len(hunk_lines) >= 3
        assert any("-removed" in line for line in hunk_lines)
        assert any("+added" in line for line in hunk_lines)


class TestDetectTruncation:
    """detect_truncation recognizes the coordinare: marker."""

    def test_truncation_marker_detected(self):
        """Truncation marker is detected."""
        diff_with_marker = """diff --git a/src/foo.py b/src/foo.py
@@ -1,3 +1,3 @@

[coordinare: diff truncated to 60000 chars — run `gh pr diff <pr_url>` for the full changes]
"""
        assert detect_truncation(diff_with_marker) is True

    def test_truncation_marker_with_omit_note(self):
        """Truncation marker with omit note is detected."""
        diff = """[coordinare: omitted 2 tooling/vendor and 1 binary file section(s); diff truncated to 60000 chars — run `gh pr diff <pr_url>` for the full changes]"""
        assert detect_truncation(diff) is True

    def test_no_truncation_marker(self):
        """No truncation when marker is absent."""
        diff = """diff --git a/src/foo.py b/src/foo.py
@@ -1,3 +1,3 @@
-old
+new
"""
        assert detect_truncation(diff) is False

    def test_marker_in_middle(self):
        """Marker anywhere in text is detected."""
        diff = """[coordinare: diff truncated to 60000 chars — run `gh pr diff <pr_url>` for the full changes]
some content after
"""
        assert detect_truncation(diff) is True


class TestDiffLines:
    """diff_lines extracts added and context lines."""

    def test_extract_added_and_context(self):
        """Extract lines starting with + and space."""
        diff = """@@ -1,3 +1,4 @@
 context line
-removed line
+added line
 another context
"""
        lines = diff_lines(diff)
        assert "context line" in lines
        assert "added line" in lines
        assert "another context" in lines
        assert "removed line" not in lines  # removed lines start with -

    def test_skip_diff_headers(self):
        """Skip headers starting with +++/---."""
        diff = """--- a/src/foo.py
+++ b/src/foo.py
 context
+added
"""
        lines = diff_lines(diff)
        assert "context" in lines
        assert "added" in lines
        # The +++ and --- lines are skipped
        assert not any("a/src" in line or "b/src" in line for line in lines)


class TestLineInHunks:
    """line_in_hunks checks if a line is in any hunk's range."""

    def test_line_in_range(self):
        """Line within hunk range returns True."""
        cf = ChangedFile(
            path="src/foo.py",
            hunks=[Hunk(header="@@ @@", start_line=10, end_line=20)],
            fully_in_diff=True,
        )
        assert line_in_hunks(cf, 15) is True

    def test_line_at_boundary(self):
        """Lines at hunk boundaries return True."""
        cf = ChangedFile(
            path="src/foo.py",
            hunks=[Hunk(header="@@ @@", start_line=10, end_line=20)],
            fully_in_diff=True,
        )
        assert line_in_hunks(cf, 10) is True
        assert line_in_hunks(cf, 20) is True

    def test_line_outside_range(self):
        """Line outside hunk range returns False."""
        cf = ChangedFile(
            path="src/foo.py",
            hunks=[Hunk(header="@@ @@", start_line=10, end_line=20)],
            fully_in_diff=True,
        )
        assert line_in_hunks(cf, 9) is False
        assert line_in_hunks(cf, 21) is False

    def test_multiple_hunks(self):
        """Line matching any hunk returns True."""
        cf = ChangedFile(
            path="src/foo.py",
            hunks=[
                Hunk(header="@@ @@", start_line=10, end_line=20),
                Hunk(header="@@ @@", start_line=50, end_line=60),
            ],
            fully_in_diff=True,
        )
        assert line_in_hunks(cf, 15) is True
        assert line_in_hunks(cf, 55) is True
        assert line_in_hunks(cf, 30) is False

    def test_no_hunks(self):
        """Empty hunks list returns False."""
        cf = ChangedFile(
            path="src/foo.py",
            hunks=[],
            fully_in_diff=True,
        )
        assert line_in_hunks(cf, 10) is False


class TestMutationCheck:
    """Mutation tests per Constitution II.

    Test must fail when truncation detection is removed from detect_truncation.
    To verify: comment out the re.search call and confirm this test fails.
    """

    def test_mutation_detect_truncation_required(self):
        """Mutation: remove truncation detection.

        This test verifies that detect_truncation actually checks for the marker.
        If the function is mutated to always return False, this test will fail.
        """
        # Text with marker should be detected
        with_marker = "[coordinare: diff truncated to 60000 chars — ...]"
        assert detect_truncation(with_marker) is True

        # Text without marker should not be detected
        without_marker = "some random text with no marker"
        assert detect_truncation(without_marker) is False
