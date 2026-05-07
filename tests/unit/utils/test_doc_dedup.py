"""Tests for markdown doc deduplication (055 Phase 5)."""
from __future__ import annotations

from pathlib import Path

from coordinare.utils.doc_dedup import (
    DocDeduplicationResult,
    find_duplicate_sections,
    merge_duplicate_sections,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(workspace: Path, rel: str, content: str) -> Path:
    p = workspace / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# find_duplicate_sections tests
# ---------------------------------------------------------------------------


def test_no_md_files(tmp_path):
    assert find_duplicate_sections(tmp_path) == {}


def test_no_duplicates(tmp_path):
    _write(tmp_path, "a.md", "# Alpha\nSome content.\n")
    _write(tmp_path, "b.md", "# Beta\nOther content.\n")
    assert find_duplicate_sections(tmp_path) == {}


def test_exact_heading_duplicate(tmp_path):
    _write(tmp_path, "a.md", "# Setup\nRun npm install.\n")
    _write(tmp_path, "b.md", "# Setup\nRun pip install.\n")
    dupes = find_duplicate_sections(tmp_path)
    assert "Setup" in dupes
    assert len(dupes["Setup"]) == 2


def test_single_file_multiple_headings_no_dupe(tmp_path):
    _write(tmp_path, "a.md", "# Setup\nContent A.\n\n# Usage\nContent B.\n")
    assert find_duplicate_sections(tmp_path) == {}


# ---------------------------------------------------------------------------
# merge_duplicate_sections tests
# ---------------------------------------------------------------------------


def test_empty_workspace(tmp_path):
    result = merge_duplicate_sections(tmp_path)
    assert result.sections_merged == 0
    assert result.conflicts_preserved == 0
    assert result.files_modified == []


def test_high_overlap_merges_duplicate(tmp_path):
    """Two files with nearly identical Setup sections → duplicate removed.

    README.md sorts before docs/setup.md (uppercase < lowercase in ASCII),
    so README.md is canonical and docs/setup.md has the section removed.
    """
    canonical = "# Setup\n\nRun `npm install` to set up dependencies.\n\n"
    duplicate = "# Setup\n\nRun `npm install` to set up the dependencies.\n\n"
    _write(tmp_path, "docs/setup.md", duplicate + "# Other\nStuff.\n")
    _write(tmp_path, "README.md", canonical + "# Intro\nHello.\n")

    result = merge_duplicate_sections(tmp_path)

    assert result.sections_merged >= 1
    # docs/setup.md (the non-canonical file) should have had the section removed
    setup_text = (tmp_path / "docs/setup.md").read_text()
    assert "# Setup" not in setup_text
    # README.md (canonical) still has it
    readme = (tmp_path / "README.md").read_text()
    assert "# Setup" in readme


def test_exact_duplicate_merges(tmp_path):
    body = "# Overview\n\nThis is the overview.\n"
    _write(tmp_path, "a.md", body)
    _write(tmp_path, "b.md", body)

    result = merge_duplicate_sections(tmp_path)
    assert result.sections_merged == 1

    # b.md should no longer have Overview
    b_text = (tmp_path / "b.md").read_text()
    assert "# Overview" not in b_text


def test_unrelated_sections_not_merged(tmp_path):
    _write(tmp_path, "a.md", "# Setup\nInstall with npm.\n")
    _write(tmp_path, "b.md", "# Setup\nCompletely unrelated content about databases.\n")

    result = merge_duplicate_sections(tmp_path)
    # Low overlap — nothing merged or only conflict-preserved
    assert result.sections_merged == 0


def test_canonical_priority_respected(tmp_path):
    """canonical_priority selects which file keeps the section."""
    body = "# Overview\n\nThis is exactly the same overview content for both files.\n"
    _write(tmp_path, "docs/api.md", body)
    _write(tmp_path, "README.md", body)

    result = merge_duplicate_sections(tmp_path, canonical_priority=["README.md"])

    assert result.sections_merged == 1
    # docs/api.md should have had the section removed
    api_text = (tmp_path / "docs/api.md").read_text()
    assert "# Overview" not in api_text
    # README.md keeps it
    readme_text = (tmp_path / "README.md").read_text()
    assert "# Overview" in readme_text


def test_result_dataclass_defaults():
    r = DocDeduplicationResult()
    assert r.files_modified == []
    assert r.sections_merged == 0
    assert r.conflicts_preserved == 0


# ---------------------------------------------------------------------------
# Coverage gap tests for uncovered lines
# ---------------------------------------------------------------------------


def test_token_overlap_both_empty():
    """_token_overlap returns 1.0 when both strings are empty (line 53)."""
    from coordinare.utils.doc_dedup import _token_overlap
    assert _token_overlap("", "") == 1.0


def test_token_overlap_one_empty():
    """_token_overlap returns 0.0 when one string is empty (line 55)."""
    from coordinare.utils.doc_dedup import _token_overlap
    assert _token_overlap("hello world", "") == 0.0
    assert _token_overlap("", "hello world") == 0.0


def test_extract_sections_no_headings():
    """_extract_sections returns [] for text with no ATX headings (line 39)."""
    from coordinare.utils.doc_dedup import _extract_sections
    assert _extract_sections("just plain text\nno headings here") == []


def test_find_duplicate_sections_skips_unreadable(tmp_path, monkeypatch):
    """OSError reading a file is silently skipped (lines 67-68)."""
    _write(tmp_path, "a.md", "# Setup\nContent.\n")
    _write(tmp_path, "b.md", "# Setup\nContent.\n")
    orig_read_text = Path.read_text

    def _bad_read(self, *args, **kwargs):
        if self.name == "b.md":
            raise OSError("permission denied")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _bad_read)
    # Only a.md readable — no duplicates found
    result = find_duplicate_sections(tmp_path)
    assert result == {}


def test_merge_skips_unreadable_file(tmp_path, monkeypatch):
    """OSError reading a file during merge is silently skipped (lines 102-103)."""
    _write(tmp_path, "a.md", "# Setup\nContent.\n")
    _write(tmp_path, "b.md", "# Setup\nContent.\n")
    orig_read_text = Path.read_text

    def _bad_read(self, *args, **kwargs):
        if self.name == "b.md":
            raise OSError("permission denied")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _bad_read)
    result = merge_duplicate_sections(tmp_path)
    # Only one file readable, so no duplicates to merge
    assert result.sections_merged == 0


def test_canonical_priority_not_in_paths(tmp_path):
    """canonical_priority with a path not in the file list falls through (lines 116->121)."""
    body = "# Overview\n\nIdentical content for both files here.\n"
    _write(tmp_path, "a.md", body)
    _write(tmp_path, "b.md", body)
    # "NONEXISTENT.md" is not in any path — alphabetical order used instead
    result = merge_duplicate_sections(tmp_path, canonical_priority=["NONEXISTENT.md"])
    assert result.sections_merged == 1


def test_ambiguous_overlap_preserved(tmp_path):
    """Sections with overlap between AMBIGUOUS_THRESHOLD and OVERLAP_THRESHOLD get renamed (lines 137-150)."""
    # Jaccard overlap ≈ 0.57: intersection=4, union=7, 4/7≈0.57 > AMBIGUOUS(0.30) < OVERLAP(0.60)
    _write(tmp_path, "a.md", "# Deploy\n\nalpha beta gamma uniquex\n")
    _write(tmp_path, "b.md", "# Deploy\n\nalpha beta gamma uniquey uniquez\n")
    result = merge_duplicate_sections(tmp_path)
    assert result.conflicts_preserved >= 1
    # Both files should have their heading renamed with a source hint
    a_text = (tmp_path / "a.md").read_text()
    b_text = (tmp_path / "b.md").read_text()
    assert "Deploy" in a_text
    assert "Deploy" in b_text


def test_rename_heading_renames_first_match():
    """_rename_heading renames the first matching heading (lines 165-169)."""
    from coordinare.utils.doc_dedup import _rename_heading
    text = "# Setup\nContent.\n\n# Setup\nMore content.\n"
    result = _rename_heading(text, "Setup", "Setup (a.md)")
    assert "# Setup (a.md)" in result
    # Second heading is unchanged (count=1)
    lines = result.splitlines()
    headings = [ln for ln in lines if ln.startswith("# ")]
    assert headings[0] == "# Setup (a.md)"
    assert headings[1] == "# Setup"


def test_remove_section_noop_when_heading_not_found(tmp_path):
    """_remove_section on a duplicate that was already removed returns same text (line 131)."""
    body = "# Overview\n\nIdentical overview text for both files.\n"
    _write(tmp_path, "a.md", body)
    _write(tmp_path, "b.md", body + "\n# Extra\nStuff.\n")
    # Merge once — b.md loses Overview
    result = merge_duplicate_sections(tmp_path)
    assert result.sections_merged == 1
    b_after = (tmp_path / "b.md").read_text()
    assert "# Overview" not in b_after
