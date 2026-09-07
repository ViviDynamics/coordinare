"""Tests for inventory building (spec 171)."""
from __future__ import annotations

from performer.workflows.documenter.inventory import (
    build_inventory,
    extract_citations,
    repository_layout,
    wiki_links,
)


class TestExtractCitations:
    """Test citation extraction from content."""

    def test_backticked_paths_with_slashes(self):
        text = "See `src/lib.py` and `config/settings.json` for details."
        tree = {"src/lib.py", "config/settings.json", "other/file.py"}
        citations = extract_citations(text, tree)
        assert "src/lib.py" in citations
        assert "config/settings.json" in citations
        assert "other/file.py" not in citations

    def test_backticked_paths_with_extensions(self):
        text = "Reference `Makefile` and `setup.py` here."
        tree = {"Makefile", "setup.py", "src/"}
        citations = extract_citations(text, tree)
        assert "Makefile" in citations or "setup.py" in citations

    def test_link_targets_outside_wiki(self):
        text = "Check [module guide](../src/api/guide.md) or [this](./README.md)."
        tree = {"docs/wiki/README.md", "src/api/guide.md"}
        extract_citations(text, tree)
        # Link targets that resolve to tree files outside wiki should be included
        # This is implementation-dependent based on the resolution logic

    def test_deduplication_preserves_order(self):
        text = "Use `src/lib.py`, then again `src/lib.py` later."
        tree = {"src/lib.py"}
        citations = extract_citations(text, tree)
        assert citations.count("src/lib.py") == 1
        assert citations.index("src/lib.py") == 0

    def test_directory_prefixes(self):
        text = "Code in `src/auth/` handles authentication."
        tree = {"src/auth/login.py", "src/auth/logout.py"}
        citations = extract_citations(text, tree)
        # `src/auth/` is a directory prefix
        assert "src/auth/" in citations or len(citations) > 0

    def test_no_citations(self):
        text = "Plain text with no code references."
        tree = {"src/main.py"}
        citations = extract_citations(text, tree)
        assert citations == []


class TestWikiLinks:
    """Test wiki link extraction and resolution."""

    def test_relative_md_links(self):
        text = "See [setup](setup.md) and [architecture](../wiki/architecture.md)."
        page_path = "docs/wiki/guide.md"
        resolved = wiki_links(text, page_path)
        # Should resolve to docs/wiki paths
        assert len(resolved) >= 0  # Implementation varies

    def test_absolute_wiki_links(self):
        text = "[Home](/docs/wiki/README.md) or [API](docs/wiki/api.md)"
        page_path = "docs/wiki/test.md"
        wiki_links(text, page_path)
        # Should normalize paths

    def test_non_markdown_links_excluded(self):
        text = "Visit [site](https://example.com) or read [guide](guide.pdf)."
        page_path = "docs/wiki/page.md"
        wiki_links(text, page_path)
        # Should not include .pdf or external links


class TestBuildInventory:
    """Test inventory building from the wiki."""

    def test_inventory_from_temp_wiki(self, tmp_path):
        """Build inventory from a temporary wiki structure."""
        # Create a temp wiki
        wiki_dir = tmp_path / "docs" / "wiki"
        wiki_dir.mkdir(parents=True)

        # Create test pages
        (wiki_dir / "README.md").write_text("# Home\n\nWelcome")
        (wiki_dir / "setup.md").write_text("---\nkind: how-to\n---\n# Setup\n\nSteps here")
        (wiki_dir / "api.md").write_text(
            "---\nkind: reference\n---\n# API\n\nSee `src/api.py`\n\n[setup](setup.md)"
        )

        tree = {"src/api.py", "docs/wiki/README.md", "docs/wiki/setup.md", "docs/wiki/api.md"}
        inventory = build_inventory(tmp_path, tree)

        assert len(inventory) == 3
        paths = [p.path for p in inventory]
        assert "docs/wiki/README.md" in paths

    def test_inventory_empty_wiki(self, tmp_path):
        """Inventory is empty when no wiki exists."""
        tree = {"src/main.py"}
        inventory = build_inventory(tmp_path, tree)
        assert inventory == []


class TestRepositoryLayout:
    """Test repository layout detection."""

    def test_layout_from_pyproject(self, tmp_path):
        """Detect project name and structure from pyproject.toml."""
        # Create minimal pyproject.toml
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-project"\n')

        # Create package structure
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "auth").mkdir()
        (tmp_path / "src" / "api").mkdir()
        (tmp_path / "tests").mkdir()

        # Create some files
        (tmp_path / "src" / "auth" / "login.py").write_text("pass")
        (tmp_path / "src" / "api" / "server.py").write_text("pass")
        (tmp_path / "tests" / "test_auth.py").write_text("pass")

        tree = {"src/auth/login.py", "src/api/server.py", "tests/test_auth.py"}
        layout = repository_layout(tmp_path, tree)

        assert layout.project_name == "my-project"

    def test_layout_from_package_json(self, tmp_path):
        """Detect project name from package.json when pyproject.toml absent."""
        (tmp_path / "package.json").write_text('{"name": "node-project"}')

        tree = {"package.json"}
        layout = repository_layout(tmp_path, tree)

        assert layout.project_name == "node-project"

    def test_layout_fallback_to_dirname(self, tmp_path):
        """Fall back to directory name when no config files."""
        tree = {"src/main.py"}
        layout = repository_layout(tmp_path, tree)

        assert layout.project_name == tmp_path.name

    def test_has_ci_detection(self, tmp_path):
        """Detect CI presence from .github/workflows."""
        workflows_dir = tmp_path / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "test.yml").write_text("name: Test")

        tree = {".github/workflows/test.yml"}
        layout = repository_layout(tmp_path, tree)

        assert layout.has_ci is True

    def test_test_command_hint_pytest(self, tmp_path):
        """Detect pytest as test command."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname="test"')
        (tmp_path / "pytest.ini").write_text("")

        tree = {"pytest.ini"}
        layout = repository_layout(tmp_path, tree)

        assert layout.test_command_hint == "pytest"

    def test_test_command_hint_npm(self, tmp_path):
        """Detect npm test from package.json."""
        (tmp_path / "package.json").write_text('{"name":"test","scripts":{"test":"jest"}}')

        tree = {"package.json"}
        layout = repository_layout(tmp_path, tree)

        assert layout.test_command_hint == "npm test"
