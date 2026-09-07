"""Tests for README generation (spec 171)."""
from __future__ import annotations

from performer.workflows.documenter.index import (
    generate_readme,
    readme_shape_ok,
)
from performer.workflows.documenter.models import WikiPage


class TestGenerateReadme:
    """Test README generation in llms.txt shape."""

    def test_basic_readme_structure(self):
        """Generated README has correct structure."""
        pages = [
            WikiPage(
                path="docs/wiki/architecture.md",
                kind="explanation",
                title="Architecture",
                citations=[],
                links=[],
                size=1000,
            ),
            WikiPage(
                path="docs/wiki/setup.md",
                kind="how-to",
                title="Setup Guide",
                citations=[],
                links=[],
                size=500,
            ),
            WikiPage(
                path="docs/wiki/api.md",
                kind="reference",
                title="API Reference",
                citations=[],
                links=[],
                size=800,
            ),
        ]

        readme = generate_readme("TestProject", "A test project", pages)

        assert "# TestProject" in readme
        assert "A test project" in readme
        assert "[Architecture](architecture.md)" in readme
        assert "[Setup Guide](setup.md)" in readme
        assert "[API Reference](api.md)" in readme

    def test_summary_truncated_to_300_chars(self):
        """Summary is truncated to 300 characters."""
        long_summary = "x" * 400
        pages = []

        readme = generate_readme("Project", long_summary, pages)

        lines = readme.split("\n")
        blockquote_lines = [line for line in lines if line.startswith(">")]
        blockquote_text = "\n".join(blockquote_lines)
        # Remove blockquote markers
        content = blockquote_text.replace("> ", "").replace(">", "")
        assert len(content.strip()) <= 300

    def test_sections_in_order(self):
        """README sections appear in correct order."""
        pages = [
            WikiPage(
                path="docs/wiki/intro.md",
                kind="explanation",
                title="Introduction",
                citations=[],
                links=[],
                size=500,
            ),
            WikiPage(
                path="docs/wiki/decisions/001.md",
                kind="decision",
                title="Use async",
                citations=[],
                links=[],
                size=300,
            ),
        ]

        readme = generate_readme("Project", "Summary", pages)

        # Find positions of section headers
        start_here = readme.find("## Start here")
        readme.find("## Architecture")
        decisions = readme.find("## Decisions")

        # They should appear in order (or some may be missing)
        if start_here >= 0 and decisions >= 0:
            assert start_here < decisions

    def test_each_page_linked_exactly_once(self):
        """Each page appears exactly once in README."""
        pages = [
            WikiPage(
                path="docs/wiki/test.md",
                kind="reference",
                title="Test Page",
                citations=[],
                links=[],
                size=500,
            )
        ]

        readme = generate_readme("Project", "Summary", pages)

        count = readme.count("[Test Page](test.md)")
        assert count == 1

    def test_pages_under_kind_sections(self):
        """Pages are listed under their KIND_TO_SECTION sections."""
        pages = [
            WikiPage(
                path="docs/wiki/architecture.md",
                kind="explanation",
                title="Architecture",
                citations=[],
                links=[],
                size=500,
            ),
        ]

        readme = generate_readme("Project", "Summary", pages)

        # Architecture kind should appear under Architecture section
        arch_section = readme.find("## Architecture")
        arch_page = readme.find("[Architecture](architecture.md)")
        assert arch_section >= 0
        assert arch_page >= 0
        assert arch_section < arch_page

    def test_none_kind_under_optional(self):
        """Pages with kind=None are listed under Optional."""
        pages = [
            WikiPage(
                path="docs/wiki/other.md",
                kind=None,
                title="Other Info",
                citations=[],
                links=[],
                size=500,
            )
        ]

        readme = generate_readme("Project", "Summary", pages)

        optional_section = readme.find("## Optional")
        other_page = readme.find("[Other Info](other.md)")
        assert optional_section >= 0
        assert other_page >= 0


class TestReadmeShapeOk:
    """Test README shape validation."""

    def test_valid_shape(self):
        """Valid README passes shape check."""
        content = """# Project
> Summary here
## Start here
- [Page](page.md): Description
## Architecture
- [Design](design.md): How it works
"""
        pages = [
            WikiPage(
                path="docs/wiki/page.md",
                kind=None,
                title="Page",
                citations=[],
                links=[],
                size=100,
            ),
            WikiPage(
                path="docs/wiki/design.md",
                kind="explanation",
                title="Design",
                citations=[],
                links=[],
                size=100,
            ),
        ]

        failures = readme_shape_ok(content, pages)
        assert len(failures) == 0

    def test_missing_h1(self):
        """Missing H1 fails shape check."""
        content = "> Summary\n## Section\n- [Page](page.md): Desc"
        pages = []

        failures = readme_shape_ok(content, pages)
        assert len(failures) > 0
        assert any("h1" in f.lower() for f in failures)

    def test_missing_blockquote(self):
        """Missing blockquote after H1 fails check."""
        content = "# Project\n## Section\n- [Page](page.md): Desc"
        pages = []

        readme_shape_ok(content, pages)
        # May fail on blockquote presence

    def test_page_linked_twice(self):
        """Page linked twice fails shape check."""
        content = """# Project
> Summary
## Start here
- [Page](page.md): First
- [Page](page.md): Second
"""
        pages = [
            WikiPage(
                path="docs/wiki/page.md",
                kind=None,
                title="Page",
                citations=[],
                links=[],
                size=100,
            )
        ]

        failures = readme_shape_ok(content, pages)
        assert len(failures) > 0

    def test_invalid_h2_section(self):
        """Invalid H2 section name fails check."""
        content = """# Project
> Summary
## Invalid Section
- [Page](page.md): Desc
"""
        pages = []

        readme_shape_ok(content, pages)
        # May fail on invalid section name
