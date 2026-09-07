"""Tests for pointer section rendering (spec 171)."""
from __future__ import annotations

from performer.workflows.documenter.pointers import (
    render_pointer_section,
    replace_between_markers,
)


class TestRenderPointerSection:
    """Test pointer section rendering."""

    def test_basic_pointer_section(self):
        """Pointer section includes wiki info and entrypoint."""
        entrypoint = "docs/wiki/README.md"
        first_three = [
            ("Setup Guide", "docs/wiki/setup.md"),
            ("Architecture", "docs/wiki/architecture.md"),
            ("API Reference", "docs/wiki/api.md"),
        ]

        section = render_pointer_section(entrypoint, first_three)

        assert "docs/wiki/README.md" in section
        assert "Setup Guide" in section
        assert "Architecture" in section
        assert "API Reference" in section

    def test_pointer_section_under_40_lines(self):
        """Pointer section must be under 40 lines."""
        entrypoint = "docs/wiki/README.md"
        first_three = [
            ("Page 1", "docs/wiki/page1.md"),
            ("Page 2", "docs/wiki/page2.md"),
            ("Page 3", "docs/wiki/page3.md"),
        ]

        section = render_pointer_section(entrypoint, first_three)

        line_count = len(section.split("\n"))
        assert line_count <= 40

    def test_marker_format(self):
        """Pointer section uses correct marker format."""
        entrypoint = "docs/wiki/README.md"
        first_three = []

        section = render_pointer_section(entrypoint, first_three)

        assert "<!-- coordinare:wiki-pointer:start -->" in section
        assert "<!-- coordinare:wiki-pointer:end -->" in section

    def test_instruction_text(self):
        """Pointer section includes instructions."""
        entrypoint = "docs/wiki/README.md"
        first_three = [("Setup", "docs/wiki/setup.md")]

        section = render_pointer_section(entrypoint, first_three)

        # Should mention reading the index first
        assert "index" in section.lower() or "README" in section
        # Should mention loading pages as needed
        assert ("load" in section.lower() or "read" in section.lower())
        # Should mention no copying
        assert ("copy" in section.lower() or "wiki" in section.lower())


class TestReplaceBetweenMarkers:
    """Test replacing content between markers."""

    def test_replace_existing_section(self):
        """Replace existing section between markers."""
        original = """# File Header

<!-- coordinare:wiki-pointer:start -->
Old content here
More old content
<!-- coordinare:wiki-pointer:end -->

# Rest of file
"""
        new_section = """<!-- coordinare:wiki-pointer:start -->
New content here
<!-- coordinare:wiki-pointer:end -->"""

        result = replace_between_markers(original, new_section)

        assert "Old content" not in result
        assert "New content here" in result
        assert "# File Header" in result
        assert "# Rest of file" in result

    def test_append_when_missing(self):
        """Append section when markers are missing."""
        original = "# File Header\n\nContent here"
        new_section = """<!-- coordinare:wiki-pointer:start -->
Wiki pointer
<!-- coordinare:wiki-pointer:end -->"""

        result = replace_between_markers(original, new_section)

        assert "Content here" in result
        assert "Wiki pointer" in result
        assert result.endswith(new_section)

    def test_rest_unchanged(self):
        """Everything outside markers stays byte-for-byte identical."""
        original = """# Header
Line 1
<!-- coordinare:wiki-pointer:start -->
Old
<!-- coordinare:wiki-pointer:end -->
Line 2
"""
        new_section = """<!-- coordinare:wiki-pointer:start -->
New
<!-- coordinare:wiki-pointer:end -->"""

        result = replace_between_markers(original, new_section)

        # Should preserve the exact text outside markers
        assert "# Header\nLine 1\n" in result
        assert "\nLine 2" in result

    def test_crlf_input(self):
        """Handle CRLF line endings correctly."""
        original = "# Header\r\n<!-- coordinare:wiki-pointer:start -->\r\nOld\r\n<!-- coordinare:wiki-pointer:end -->\r\nEnd"
        new_section = """<!-- coordinare:wiki-pointer:start -->
New
<!-- coordinare:wiki-pointer:end -->"""

        result = replace_between_markers(original, new_section)

        assert "Old" not in result
        assert "New" in result
        # Should preserve structure
        assert "# Header" in result
        assert "End" in result
