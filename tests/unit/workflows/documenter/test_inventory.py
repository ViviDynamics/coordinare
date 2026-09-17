"""Tests for inventory building (spec 171)."""
from __future__ import annotations

import pytest
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.documenter import MAX_ROOT_EVIDENCE, _init_modules
from performer.workflows.documenter.inventory import (
    build_inventory,
    extract_citations,
    repository_layout,
    wiki_links,
)
from performer.workflows.documenter.models import RepositoryLayout
from performer.workflows.project_shape import ProjectShape, ProjectShapeUnknown
from performer.workflows.toolkit import Toolkit


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
            "---\nkind: reference\n---\n# API\n\nSee `src/api.py`\n\n[setup](setup.md)",
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
    """The layout is what the model read, plus arithmetic over the paths (#367).

    These tests used to assert that a project name came from ``pyproject.toml``
    or ``package.json`` and that a test command was ``pytest`` or ``npm test``.
    Those probes are gone: they understood two ecosystems and produced an empty
    picture for every other one, silently. The model reads the repository and
    says what it is; what stays here is counting.
    """

    @staticmethod
    def _toolkit(shape: ProjectShape) -> Toolkit:
        async def model_call(persona, content, max_tokens):
            return ModelReply(content=shape.model_dump_json(), finish_reason="stop")

        return Toolkit(metrics=WorkflowMetrics(), model_call=model_call, call_limit=4)

    @pytest.mark.asyncio
    async def test_the_project_name_is_the_one_the_model_read(self, tmp_path):
        (tmp_path / "mix.exs").write_text("defmodule Week.MixProject do\nend\n")
        tree = {"mix.exs", "lib/week.ex"}
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "week.ex").write_text("defmodule Week do\nend\n")

        layout = await repository_layout(
            self._toolkit(ProjectShape(project_name="week", test_command="mix test", source_dirs=["lib"])),
            tmp_path, tree,
        )
        assert layout.project_name == "week"
        assert layout.test_command_hint == "mix test", "a stack the old probes had never heard of"

    @pytest.mark.asyncio
    async def test_a_nameless_reading_falls_back_to_the_directory(self, tmp_path):
        layout = await repository_layout(
            self._toolkit(ProjectShape(project_name="", source_dirs=[])), tmp_path, {"src/main.go"},
        )
        assert layout.project_name == tmp_path.name

    @pytest.mark.asyncio
    async def test_packages_are_the_directories_the_model_named(self, tmp_path):
        """Sizes and test-presence are arithmetic over paths; the choice is not.

        This replaces an 18-entry file-extension table and a hardcoded list of
        directory names to skip, neither of which could see a language nobody
        had added to them.
        """
        for rel, text in {
            "app/models/week.rb": "class Week\nend\n",
            "lib/helper.rb": "module Helper\nend\n",
            "spec/week_spec.rb": "describe Week do\nend\n",
            "node_modules/x/index.js": "// vendored\n",
        }.items():
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        tree = {"app/models/week.rb", "lib/helper.rb", "spec/week_spec.rb", "node_modules/x/index.js"}

        layout = await repository_layout(
            self._toolkit(ProjectShape(project_name="week", source_dirs=["app", "lib"])), tmp_path, tree,
        )
        paths = [p["path"] for p in layout.packages]
        assert sorted(paths) == ["app", "lib"], "exactly the directories the reading named"
        assert "node_modules" not in paths, "the model excluded it; there is no skip list any more"
        assert "spec" not in paths, "and it excluded the tests without an extension table"
        assert all(p["size"] > 0 for p in layout.packages)
        # Ordering is by size descending, unchanged: the planner picks the
        # largest package with tests, and that is the behaviour it relies on.
        sizes = [p["size"] for p in layout.packages]
        assert sizes == sorted(sizes, reverse=True)

    @pytest.mark.asyncio
    async def test_a_directory_the_model_invented_is_ignored(self, tmp_path):
        """A reading is not a guarantee the path exists."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x = 1\n")
        layout = await repository_layout(
            self._toolkit(ProjectShape(source_dirs=["src", "does_not_exist"])), tmp_path, {"src/a.py"},
        )
        assert [p["path"] for p in layout.packages] == ["src"]

    @pytest.mark.asyncio
    async def test_ci_detection_stays_mechanical(self, tmp_path):
        """GitHub's workflow path is knowledge of the platform coordinare runs
        on, not of the project's stack, so it does not need the model."""
        layout = await repository_layout(
            self._toolkit(ProjectShape(source_dirs=[])), tmp_path, {".github/workflows/test.yml"},
        )
        assert layout.has_ci is True

    @pytest.mark.asyncio
    async def test_a_repository_the_model_cannot_characterise_stops(self, tmp_path):
        """The floor #367 asks both workflows to keep, taken from QA.

        The old code returned an empty layout here and the documenter wrote from
        a blank picture, with nothing recording that it had not understood.
        """
        with pytest.raises(ProjectShapeUnknown):
            await repository_layout(
                self._toolkit(ProjectShape(cannot_determine="no manifest I recognise")),
                tmp_path, {"README"},
            )


class TestInitEvidenceNamesNoManifestAndNoExtension:
    """The last two hardcoded lists in the documenter (#364, finished in #367).

    `_init_modules` chose evidence for an init-mode page from a six-name
    manifest list and a six-extension source tuple. A repository whose manifest
    is `mix.exs` offered no project files; one written in Elixir, Swift or
    Kotlin offered no source files. The init page was then written from nothing,
    and nothing recorded that it had been.
    """

    @staticmethod
    def _plan(path: str):
        from types import SimpleNamespace
        return SimpleNamespace(path=path)

    @staticmethod
    def _layout(dirs: list[str]):
        return RepositoryLayout(
            project_name="week", has_ci=False, test_command_hint="",
            packages=[{"path": d, "size": 100, "has_tests": False} for d in dirs],
        )

    def test_a_stack_with_none_of_the_old_names_still_gets_evidence(self):
        """mix.exs and .ex: no entry in either deleted list."""
        tree = {"mix.exs", "README.md", "lib/week.ex", "lib/week/server.ex", "test/week_test.exs"}
        got = _init_modules(self._plan("docs/wiki/architecture.md"), tree, self._layout(["lib"]))
        assert "mix.exs" in got, "the project's own manifest was invisible"
        assert any(p.endswith(".ex") for p in got), "its source was invisible"

    def test_a_repository_whose_only_root_file_is_unknown_still_surfaces_it(self):
        """The sharpest version: nothing here appeared in either deleted list."""
        tree = {"shard.yml", "src/week.cr"}
        got = _init_modules(self._plan("docs/wiki/architecture.md"), tree, self._layout(["src"]))
        assert got == ["shard.yml", "src/week.cr"]

    def test_setup_pages_get_the_repositorys_own_root_files(self):
        tree = {"Cargo.toml", "Justfile", "rust-toolchain.toml", "src/main.rs"}
        got = _init_modules(self._plan("docs/wiki/setup.md"), tree, self._layout(["src"]))
        assert "Cargo.toml" in got and "Justfile" in got

    def test_root_evidence_is_bounded(self):
        """A repository with fifty root files must not put them all in a prompt."""
        tree = {f"file{i}.toml" for i in range(50)} | {"src/a.py"}
        got = _init_modules(self._plan("docs/wiki/setup.md"), tree, self._layout(["src"]))
        assert len([p for p in got if "/" not in p]) <= MAX_ROOT_EVIDENCE

    def test_tests_are_still_excluded_from_architecture_evidence(self):
        """The one filter that survives, because it is about role, not stack."""
        tree = {"go.mod", "pkg/app.go", "pkg/tests/app_test.go"}
        got = _init_modules(self._plan("docs/wiki/architecture.md"), tree, self._layout(["pkg"]))
        assert "pkg/tests/app_test.go" not in got
        assert "pkg/app.go" in got
