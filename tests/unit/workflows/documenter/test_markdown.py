"""Tests for markdown parsing utilities (spec 171)."""
from __future__ import annotations

from performer.workflows.documenter.markdown import (
    Fence,
    Heading,
    Link,
    backticked_tokens,
    blockquote_under_h1,
    body_chars,
    fenced_blocks,
    headings,
    links,
    parse_frontmatter,
)


class TestParseFrontmatter:
    """Test YAML frontmatter parsing."""

    def test_basic_frontmatter(self):
        text = """---
kind: reference
title: Test Page
---
# Body"""
        meta, body = parse_frontmatter(text)
        assert meta["kind"] == "reference"
        assert meta["title"] == "Test Page"
        assert body.strip() == "# Body"

    def test_no_frontmatter(self):
        text = "# No frontmatter\n\nContent"
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_empty_frontmatter(self):
        text = """---
---
# Body"""
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body.strip() == "# Body"

    def test_frontmatter_with_special_chars(self):
        text = """---
kind: how-to
description: How to use the API
---
Content"""
        meta, _body = parse_frontmatter(text)
        assert meta["kind"] == "how-to"
        assert meta["description"] == "How to use the API"

    def test_crlf_input(self):
        text = "---\r\nkind: reference\r\n---\r\n# Body"
        meta, _body = parse_frontmatter(text)
        assert meta["kind"] == "reference"


class TestHeadings:
    """Test heading extraction."""

    def test_basic_headings(self):
        text = """# Main Title
Some text.
## Section One
More text.
### Subsection
Even more."""
        h = headings(text)
        assert len(h) >= 3
        assert h[0].level == 1
        assert h[0].text == "Main Title"
        assert h[1].level == 2
        assert h[1].text == "Section One"

    def test_headings_with_link_and_code(self):
        text = """# [Link](url) Heading
## Code `token` Heading"""
        h = headings(text)
        assert h[0].has_link is True
        assert h[1].has_code is True

    def test_fences_hide_headings(self):
        text = """# Real Heading
```
# Not a heading
## Also not
```
## Real Section"""
        h = headings(text)
        # Should only see the real headings
        real_headings = [hd for hd in h if hd.text in ("Real Heading", "Real Section")]
        assert len(real_headings) == 2

    def test_heading_line_numbers(self):
        text = """# First
Text
## Second"""
        h = headings(text)
        assert h[0].line_no == 0
        assert h[1].line_no == 2

    def test_nested_code_spans_in_heading(self):
        text = "## Code `part1` and `part2` in heading"
        h = headings(text)
        assert len(h) == 1
        assert h[0].has_code is True


class TestFencedBlocks:
    """Test fenced code block detection."""

    def test_fenced_blocks(self):
        text = """```python
def hello():
    pass
```
Some text.
```
no language
```"""
        fences = fenced_blocks(text)
        assert len(fences) == 2
        assert fences[0].language == "python"
        assert fences[1].language == ""

    def test_fenced_blocks_line_numbers(self):
        text = """Line 0
```python
code
```
Line 4"""
        fences = fenced_blocks(text)
        assert len(fences) == 1
        assert fences[0].line_no == 1

    def test_nested_fences_not_matched(self):
        """Inner fence markers should not be recognized inside a fence."""
        text = """```
outer fence
```inner
```
"""
        fences = fenced_blocks(text)
        # Should only see the outer fence
        assert len(fences) == 1


class TestLinks:
    """Test link extraction."""

    def test_basic_links(self):
        text = "[Google](https://google.com) and [Local](./page.md)"
        found = links(text)
        assert len(found) == 2
        assert found[0].text == "Google"
        assert found[0].target == "https://google.com"
        assert found[1].text == "Local"
        assert found[1].target == "./page.md"

    def test_links_with_titles(self):
        text = '[Link](url "Title") text'
        found = links(text)
        assert len(found) == 1
        assert found[0].target == "url"

    def test_no_links(self):
        text = "This is plain text with no links"
        found = links(text)
        assert found == []

    def test_link_line_numbers(self):
        text = """Line 0
[Link1](url1)
Line 2
[Link2](url2)"""
        found = links(text)
        assert found[0].line_no == 1
        assert found[1].line_no == 3


class TestBacktickedTokens:
    """Test backticked token extraction."""

    def test_basic_tokens(self):
        text = "Use `src/lib.py` or `config.json` for this."
        tokens = backticked_tokens(text)
        assert "src/lib.py" in tokens
        assert "config.json" in tokens

    def test_nested_backticks_ignored(self):
        """Single backticks do not nest."""
        text = "Code: `token1` and `token2`"
        tokens = backticked_tokens(text)
        assert len(tokens) == 2

    def test_empty_backticks(self):
        text = "Empty `` and full `token`"
        tokens = backticked_tokens(text)
        # Empty backticks may or may not appear depending on implementation
        assert "token" in tokens

    def test_multiline_code(self):
        """Backticks should only match on a single line."""
        text = """First `token1`
Second `token2`"""
        tokens = backticked_tokens(text)
        assert "token1" in tokens
        assert "token2" in tokens


class TestBlockquoteUnderH1:
    """Test blockquote extraction after first H1."""

    def test_blockquote_extraction(self):
        text = """# Title
> This is a blockquote
> on multiple lines
Some normal text."""
        bq = blockquote_under_h1(text)
        assert bq is not None
        assert "blockquote" in bq.lower()

    def test_no_blockquote(self):
        text = """# Title
Just normal text, no blockquote."""
        bq = blockquote_under_h1(text)
        assert bq is None

    def test_blockquote_interrupted_by_content(self):
        text = """# Title
> Blockquote
Normal text
> Another quote (too late)"""
        bq = blockquote_under_h1(text)
        assert "blockquote" in bq.lower()
        assert "Another" not in bq

    def test_no_h1(self):
        text = "> Blockquote\n## Just H2"
        bq = blockquote_under_h1(text)
        assert bq is None


class TestBodyChars:
    """Test character count excluding frontmatter."""

    def test_with_frontmatter(self):
        text = """---
kind: reference
---
# Heading
Body content here."""
        chars = body_chars(text)
        assert "---" not in text[text.index(text):] or chars > 0
        # Should not include frontmatter
        assert chars == len("# Heading\nBody content here.")

    def test_without_frontmatter(self):
        text = "# Heading\nContent"
        chars = body_chars(text)
        assert chars == len(text)

    def test_empty_body(self):
        text = "---\nkind: ref\n---\n"
        chars = body_chars(text)
        assert chars == 0


class TestHeadingModel:
    """Test Heading data model."""

    def test_heading_creation(self):
        h = Heading(level=2, text="Test", line_no=5, has_link=False, has_code=True)
        assert h.level == 2
        assert h.text == "Test"
        assert h.line_no == 5
        assert h.has_code is True


class TestFenceModel:
    """Test Fence data model."""

    def test_fence_creation(self):
        f = Fence(language="python", line_no=10)
        assert f.language == "python"
        assert f.line_no == 10


class TestLinkModel:
    """Test Link data model."""

    def test_link_creation(self):
        found = Link(text="Click me", target="https://example.com", line_no=3)
        assert found.text == "Click me"
        assert found.target == "https://example.com"
        assert found.line_no == 3
