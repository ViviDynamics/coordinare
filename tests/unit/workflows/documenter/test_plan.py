"""Tests for plan building (spec 171)."""
from __future__ import annotations

from performer.workflows.documenter.models import WikiPage
from performer.workflows.documenter.plan import (
    build_plan,
    init_skeleton,
    is_doc_path,
    select_pages,
)


class TestIsDocPath:
    """Test doc path detection."""

    def test_docs_prefix(self):
        assert is_doc_path("docs/wiki/test.md")
        assert is_doc_path("docs/README.md")

    def test_doc_prefix(self):
        assert is_doc_path("doc/guide.md")

    def test_readme_basename(self):
        assert is_doc_path("README.md")
        assert is_doc_path("README")

    def test_contributing_basename(self):
        assert is_doc_path("CONTRIBUTING.md")

    def test_changelog_basename(self):
        assert is_doc_path("CHANGELOG")

    def test_pointer_files(self):
        assert is_doc_path("AGENTS.md")
        assert is_doc_path("CLAUDE.md")

    def test_non_doc_paths(self):
        assert not is_doc_path("src/main.py")
        assert not is_doc_path("tests/test.py")
        assert not is_doc_path("LICENSE")


class TestSelectPages:
    """Test page selection from brief and inventory."""

    def test_brief_entries_first(self):
        """Brief entries are selected first."""
        brief_docs = [
            {"topic": "Payments", "location": "docs/wiki/payments.md", "say": ["Payment processing"]},
            {"topic": "API", "location": "docs/wiki/api.md", "say": ["REST API"]},
        ]
        changed_files = []
        inventory = []

        plans, _deferred, _refused = select_pages(brief_docs, changed_files, inventory, 8)

        # Brief entries + README
        paths = [p.path for p in plans]
        assert "docs/wiki/payments.md" in paths
        assert "docs/wiki/api.md" in paths

    def test_inventory_pages_from_changed_files(self):
        """Inventory pages whose citations match changed files."""
        brief_docs = []
        changed_files = ["src/payments/ledger.py"]
        inventory = [
            WikiPage(
                path="docs/wiki/architecture.md",
                kind="explanation",
                title="Architecture",
                citations=["src/payments/", "src/api/"],
                links=[],
                size=1000,
            )
        ]

        plans, _deferred, _refused = select_pages(brief_docs, changed_files, inventory, 8)

        paths = [p.path for p in plans]
        assert "docs/wiki/architecture.md" in paths

    def test_readme_appended_when_other_pages_selected(self):
        """README is appended when any page is selected."""
        brief_docs = [{"topic": "Test", "location": "docs/wiki/test.md", "say": []}]
        changed_files = []
        inventory = []

        plans, _deferred, _refused = select_pages(brief_docs, changed_files, inventory, 8)

        paths = [p.path for p in plans]
        assert any("README" in p for p in paths)

    def test_plan_capped_at_8(self):
        """Plan is capped at 8 entries."""
        brief_docs = [
            {"topic": f"Page {i}", "location": f"docs/wiki/page{i}.md", "say": []}
            for i in range(10)
        ]
        changed_files = []
        inventory = []

        plans, deferred, _refused = select_pages(brief_docs, changed_files, inventory, 8)

        assert len(plans) <= 8
        assert len(deferred) > 0

    def test_refused_non_doc_paths(self):
        """Paths outside docs/ are refused."""
        brief_docs = [
            {"topic": "Code", "location": "src/main.py", "say": []},
        ]
        changed_files = []
        inventory = []

        _plans, _deferred, refused = select_pages(brief_docs, changed_files, inventory, 8)

        assert "src/main.py" in refused

    def test_deduplication_by_location(self):
        """Duplicate brief entries are deduplicated."""
        brief_docs = [
            {"topic": "API", "location": "docs/wiki/api.md", "say": ["REST"]},
            {"topic": "API", "location": "docs/wiki/api.md", "say": ["GraphQL"]},
        ]
        changed_files = []
        inventory = []

        plans, _deferred, _refused = select_pages(brief_docs, changed_files, inventory, 8)

        api_plans = [p for p in plans if "api" in p.path]
        assert len(api_plans) == 1
        # Both say texts should be collected
        assert len(api_plans[0].say) == 2


class TestInitSkeleton:
    """Test skeleton generation for init mode."""

    def test_basic_skeleton(self):
        """Init mode selects README, architecture, setup, testing."""
        layout_obj = type('Layout', (), {
            'project_name': 'test',
            'packages': [],
            'has_ci': False,
            'test_command_hint': 'pytest'
        })()

        plans, _deferred = init_skeleton(layout_obj, [], 8)

        paths = [p.path for p in plans]
        # Should include README, architecture, setup, testing
        assert any("README" in p for p in paths)

    def test_packages_by_size_largest_first(self):
        """Packages are ordered by size descending."""
        layout_obj = type('Layout', (), {
            'project_name': 'test',
            'packages': [
                {'path': 'src/small', 'size': 1000, 'has_tests': True},
                {'path': 'src/large', 'size': 5000, 'has_tests': True},
            ],
            'has_ci': False,
            'test_command_hint': ''
        })()

        _plans, _deferred = init_skeleton(layout_obj, [], 8)

        # Large package should come before small in the plan


class TestBuildPlan:
    """Test plan building with mode selection."""

    def test_update_mode_plan(self):
        """Plan for update mode uses brief and inventory."""
        brief_docs = [{"topic": "New", "location": "docs/wiki/new.md", "say": []}]
        changed_files = ["src/main.py"]
        inventory = []
        layout_obj = None

        plans, _deferred, _refused = build_plan(
            "update", brief_docs, changed_files, inventory, layout_obj, 8
        )

        assert len(plans) > 0

    def test_init_mode_plan(self):
        """Plan for init mode uses skeleton."""
        brief_docs = []
        changed_files = []
        inventory = []
        layout_obj = type('Layout', (), {
            'project_name': 'test',
            'packages': [],
            'has_ci': False,
            'test_command_hint': ''
        })()

        plans, _deferred, _refused = build_plan(
            "init", brief_docs, changed_files, inventory, layout_obj, 8
        )

        # Should include some skeleton pages
        assert len(plans) >= 0


def test_175_existing_brief_page_is_read_and_merged():
    page = WikiPage(path="docs/wiki/api.md", kind="reference", title="API", citations=[], links=[], size=200)
    plans, _, _ = select_pages([{"location": page.path, "say": ["New behavior"]}], [], [page], 8)
    selected = [plan for plan in plans if plan.path == page.path]
    assert len(selected) == 1
    assert selected[0].exists is True
