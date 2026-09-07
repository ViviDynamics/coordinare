"""Tests for scope_segments, declared_test_paths and declared_source_paths (spec 171 FR-003).

Mutation check protocol (FR-015): the header names a change to the rule that
must make a test in this file fail. Apply it in the real tree to verify.
- Mutation: make declared_test_paths return every scope segment ->
  test_test_paths_exclude_source fails
- Mutation: make declared_source_paths return every scope segment ->
  test_source_paths_exclude_tests fails
- Mutation: treat "." as a named path in scope_segments ->
  test_whole_tree_scope_names_nothing fails
- Mutation: drop the _inside_repo guard from present_paths ->
  test_a_traversing_or_absolute_segment_is_never_present fails
"""

from performer.workflows.implementer.resume import (
    declared_source_paths,
    declared_test_paths,
    present_paths,
    scope_segments,
)


class TestScopeSegments:
    def test_comma_and_semicolon_separated(self):
        assert scope_segments("src/a.py, tests/test_a.py; src/b.py") == ["src/a.py", "tests/test_a.py", "src/b.py"]

    def test_whole_tree_scope_names_nothing(self):
        assert scope_segments(".") == []
        assert scope_segments("") == []
        assert scope_segments(None) == []

    def test_bounded_at_twenty(self):
        assert len(scope_segments(", ".join(f"src/f{i}.py" for i in range(40)))) == 20


class TestDeclaredPaths:
    SCOPE = "src/m1.py, tests/test_m1.py, spec/thing_spec.rb, app/thing.test.ts"

    def test_test_paths_exclude_source(self):
        assert declared_test_paths(self.SCOPE) == ["tests/test_m1.py", "spec/thing_spec.rb", "app/thing.test.ts"]

    def test_source_paths_exclude_tests(self):
        assert declared_source_paths(self.SCOPE) == ["src/m1.py"]

    def test_a_scope_of_only_source_declares_no_tests(self):
        assert declared_test_paths("src/m1.py") == []
        assert declared_source_paths("src/m1.py") == ["src/m1.py"]

    def test_a_whole_tree_scope_declares_neither(self):
        assert declared_test_paths(".") == []
        assert declared_source_paths(".") == []


class TestPresentPaths:
    def test_reports_only_what_exists(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("a\n")
        assert present_paths(tmp_path, ["src/a.py", "src/b.py"]) == frozenset({"src/a.py"})

    def test_a_traversing_or_absolute_segment_is_never_present(self, tmp_path):
        """A brief's scope is prose; it must not be resolved outside the clone."""
        (tmp_path / "outside.py").write_text("x\n")
        workspace = tmp_path / "repo"
        workspace.mkdir()
        assert present_paths(workspace, ["../outside.py", "/etc/hosts", "..\\outside.py"]) == frozenset()
