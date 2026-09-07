"""Tests for scope_violations gate function (spec 167 FR-008).

Mutation check protocol (FR-019): The mutation header documents a change to the
rule that must make the test fail. Run the test with the mutation to verify.
- Mutation: remove the doc path check -> test_scope_violations_detects_docs_in_any_turn fails
- Mutation: remove "tests" turn restriction -> test_scope_violations_detects_source_edit_in_tests_turn fails
"""

from performer.workflows.implementer.cycle import scope_violations


class TestScopeViolations:
    """Test scope_violations gate: detects and reports out-of-scope edits (FR-008)."""

    def test_scope_violations_detects_source_edit_in_tests_turn(self):
        """Scope violations: tests turn should not edit source files (mutation: remove this check)."""
        kind = "tests"
        changed_paths = {
            "tests/test_signin.py": "added",
            "src/auth.py": "modified",
        }

        violations = scope_violations(kind, changed_paths, runner_kind="pytest")

        assert len(violations) > 0
        violation_paths = [v["path"] for v in violations]
        assert "src/auth.py" in violation_paths

    def test_scope_violations_allows_test_files_in_tests_turn(self):
        """Scope violations: tests turn can edit test files."""
        kind = "tests"
        changed_paths = {
            "tests/test_signin.py": "added",
            "tests/auth/test_email.py": "modified",
        }

        violations = scope_violations(kind, changed_paths, runner_kind="pytest")

        assert len(violations) == 0

    def test_scope_violations_detects_docs_edit_in_implement_turn(self):
        """Scope violations: implement turn should not edit docs."""
        kind = "implement"
        changed_paths = {
            "src/auth.py": "modified",
            "docs/index.md": "modified",
        }

        violations = scope_violations(kind, changed_paths, runner_kind="pytest")

        assert len(violations) > 0
        violation_paths = [v["path"] for v in violations]
        assert "docs/index.md" in violation_paths

    def test_scope_violations_allows_source_in_implement_turn(self):
        """Scope violations: implement turn can edit source."""
        kind = "implement"
        changed_paths = {
            "src/auth.py": "modified",
            "src/utils.py": "added",
        }

        violations = scope_violations(kind, changed_paths, runner_kind="pytest")

        assert len(violations) == 0

    def test_scope_violations_detects_docs_in_any_turn(self):
        """Scope violations: no turn should edit docs (mutation: remove this check to fail)."""
        for kind in ["tests", "implement", "repair"]:
            changed_paths = {
                "docs/guide.md": "modified",
            }

            violations = scope_violations(kind, changed_paths, runner_kind="pytest")

            assert len(violations) > 0, f"Should detect doc edit in {kind} turn"

    def test_scope_violations_no_violations_returns_empty_list(self):
        """Scope violations: clean turn returns empty list."""
        kind = "tests"
        changed_paths = {
            "tests/test_signin.py": "added",
        }

        violations = scope_violations(kind, changed_paths, runner_kind="pytest")

        assert violations == []

    def test_scope_violations_multiple_violations(self):
        """Scope violations: reports multiple violations."""
        kind = "tests"
        changed_paths = {
            "src/auth.py": "modified",
            "docs/index.md": "modified",
            "tests/test_signin.py": "added",
        }

        violations = scope_violations(kind, changed_paths, runner_kind="pytest")

        assert len(violations) == 2
        violation_paths = [v["path"] for v in violations]
        assert "src/auth.py" in violation_paths
        assert "docs/index.md" in violation_paths

    def test_scope_violations_violation_structure(self):
        """Scope violations: violation has correct structure."""
        kind = "tests"
        changed_paths = {
            "src/auth.py": "modified",
        }

        violations = scope_violations(kind, changed_paths, runner_kind="pytest")

        assert len(violations) == 1
        violation = violations[0]
        assert "path" in violation
        assert "kind" in violation
        assert "reason" in violation
        assert violation["path"] == "src/auth.py"

    def test_scope_violations_various_doc_paths(self):
        """Scope violations: detects docs in various paths."""
        kind = "implement"
        doc_paths = [
            "docs/index.md",
            "docs/guide/setup.md",
            "doc/api.md",
        ]

        for doc_path in doc_paths:
            changed_paths = {doc_path: "modified"}
            violations = scope_violations(kind, changed_paths, runner_kind="pytest")
            if any(marker in doc_path for marker in ["docs/", "doc/"]):
                assert any(v["path"] == doc_path for v in violations), f"Should detect {doc_path}"
