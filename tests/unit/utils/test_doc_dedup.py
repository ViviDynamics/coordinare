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
