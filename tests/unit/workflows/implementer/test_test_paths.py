"""Tests for changed_test_files gate function (spec 167 FR-005).

Mutation check protocol (FR-019): The mutation header documents a change to the
rule that must make the test fail. Run the test with the mutation to verify.
- Mutation: remove a test pattern -> test fails for that pattern
- Mutation: remove the filtering logic -> non-test files are included
"""

from performer.workflows.implementer.cycle import changed_test_files


class TestChangedTestFiles:
    """Test changed_test_files gate: identifies which files are test files (FR-005, FR-008)."""

    def test_changed_test_files_pytest_files(self):
        """Changed test files: identifies pytest-style test files."""
        changed = {
            "tests/test_signin.py": "added",
            "tests/test_logout.py": "modified",
            "src/auth.py": "modified",
        }

        result = changed_test_files(changed, "pytest")
        assert "tests/test_signin.py" in result
        assert "tests/test_logout.py" in result
        assert "src/auth.py" not in result

    def test_changed_test_files_suffix_style(self):
        """Changed test files: identifies *_test.py suffix style."""
        changed = {
            "tests/auth_test.py": "added",
            "tests/login_test.py": "modified",
            "src/auth.py": "modified",
        }

        result = changed_test_files(changed, "pytest")
        assert "tests/auth_test.py" in result
        assert "tests/login_test.py" in result
        assert "src/auth.py" not in result

    def test_changed_test_files_jest_files(self):
        """Changed test files: identifies jest .test.js files."""
        changed = {
            "src/signin.test.js": "added",
            "src/login.test.ts": "modified",
            "src/auth.js": "modified",
        }

        result = changed_test_files(changed, "jest")
        assert "src/signin.test.js" in result
        assert "src/login.test.ts" in result
        assert "src/auth.js" not in result

    def test_changed_test_files_rspec_files(self):
        """Changed test files: identifies rspec *_spec.rb files."""
        changed = {
            "spec/signin_spec.rb": "added",
            "spec/auth/login_spec.rb": "modified",
            "app/auth.rb": "modified",
        }

        result = changed_test_files(changed, "rspec")
        assert "spec/signin_spec.rb" in result
        assert "spec/auth/login_spec.rb" in result
        assert "app/auth.rb" not in result

    def test_changed_test_files_empty_changed(self):
        """Changed test files: returns empty list when no files changed."""
        changed = {}

        result = changed_test_files(changed, "pytest")
        assert result == []

    def test_changed_test_files_mixed_files(self):
        """Changed test files: correctly identifies only test files in mixed changes."""
        changed = {
            "tests/test_feature.py": "added",
            "src/feature.py": "added",
            "tests/test_bugfix.py": "modified",
            "lib/bugfix.py": "modified",
            "README.md": "modified",
        }

        result = changed_test_files(changed, "pytest")
        assert len(result) == 2
        assert "tests/test_feature.py" in result
        assert "tests/test_bugfix.py" in result
        assert "src/feature.py" not in result
        assert "lib/bugfix.py" not in result
        assert "README.md" not in result

    def test_changed_test_files_sorted_output(self):
        """Changed test files: returns sorted list."""
        changed = {
            "tests/z_test.py": "added",
            "tests/a_test.py": "added",
            "tests/m_test.py": "added",
        }

        result = changed_test_files(changed, "pytest")
        assert result == sorted(result)

    def test_changed_test_files_nested_test_dirs(self):
        """Changed test files: finds tests in nested directories."""
        changed = {
            "tests/unit/test_auth.py": "added",
            "tests/integration/test_api.py": "added",
            "src/auth.py": "modified",
        }

        result = changed_test_files(changed, "pytest")
        assert "tests/unit/test_auth.py" in result
        assert "tests/integration/test_api.py" in result
        assert "src/auth.py" not in result

    def test_changed_test_files_minitest_files(self):
        """Changed test files: identifies minitest test_*.rb files."""
        changed = {
            "test/test_auth.rb": "added",
            "test/auth_test.rb": "added",
            "lib/auth.rb": "modified",
        }

        result = changed_test_files(changed, "pytest")
        assert "test/test_auth.rb" in result
        assert "test/auth_test.rb" in result
        assert "lib/auth.rb" not in result
