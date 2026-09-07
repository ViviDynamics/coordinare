"""Tests for documenter workflow models (spec 171)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from performer.workflows.documenter.models import (
    BAD_LINK_TEXT,
    DOC_PATH_PREFIXES,
    DOC_ROOT_FILES,
    KIND_TO_SECTION,
    KINDS,
    PAGE_MAX_CHARS,
    PAGE_MIN_CHARS,
    PLAN_CAP,
    POINTER_FILES,
    POINTER_MARKERS,
    POINTER_MAX_LINES,
    README_SECTIONS,
    REQUIRED_HEADINGS,
    SUMMARY_MAX_CHARS,
    DocsRecord,
    PageEvidence,
    PagePlan,
    PageResult,
    RepositoryLayout,
    WikiPage,
    model_page_write_schema,
)
from pydantic import ValidationError


class TestConstants:
    """Test that constants match the spec."""

    def test_kinds(self):
        assert KINDS == ("explanation", "how-to", "reference", "decision")

    def test_required_headings_structure(self):
        assert set(REQUIRED_HEADINGS.keys()) == set(KINDS)
        assert REQUIRED_HEADINGS["explanation"] == (
            "What it is",
            "How it fits",
            "Why it is this way",
            "Where to change it",
        )
        assert REQUIRED_HEADINGS["how-to"] == ("Goal", "Prerequisites", "Steps", "Verify")
        assert REQUIRED_HEADINGS["reference"] == ()
        assert REQUIRED_HEADINGS["decision"] == (
            "Context",
            "Decision",
            "Consequences",
            "Status",
        )

    def test_readme_sections(self):
        assert README_SECTIONS == (
            "Start here",
            "Architecture",
            "How to",
            "Reference",
            "Decisions",
            "Optional",
        )

    def test_kind_to_section_mapping(self):
        assert KIND_TO_SECTION == {
            "explanation": "Architecture",
            "how-to": "How to",
            "reference": "Reference",
            "decision": "Decisions",
        }

    def test_doc_path_prefixes(self):
        assert DOC_PATH_PREFIXES == ("docs/", "doc/")

    def test_doc_root_files(self):
        assert DOC_ROOT_FILES == ("README", "CONTRIBUTING", "CHANGELOG")

    def test_pointer_files(self):
        assert POINTER_FILES == ("AGENTS.md", "CLAUDE.md")

    def test_pointer_markers(self):
        assert POINTER_MARKERS == (
            "<!-- coordinare:wiki-pointer:start -->",
            "<!-- coordinare:wiki-pointer:end -->",
        )

    def test_plan_cap(self):
        assert PLAN_CAP == 8

    def test_page_size_bounds(self):
        assert PAGE_MIN_CHARS == 400
        assert PAGE_MAX_CHARS == 12000

    def test_summary_max_chars(self):
        assert SUMMARY_MAX_CHARS == 300

    def test_pointer_max_lines(self):
        assert POINTER_MAX_LINES == 40

    def test_bad_link_text(self):
        assert BAD_LINK_TEXT == ("here", "link", "this")


class TestWikiPage:
    """Test WikiPage model."""

    def test_basic_creation(self):
        page = WikiPage(
            path="docs/wiki/test.md",
            kind="reference",
            title="Test Page",
            citations=["src/lib.py", "src/utils/"],
            links=["architecture.md", "setup.md"],
            size=1500,
        )
        assert page.path == "docs/wiki/test.md"
        assert page.kind == "reference"
        assert page.title == "Test Page"

    def test_kind_can_be_none(self):
        page = WikiPage(
            path="docs/wiki/test.md",
            kind=None,
            title="Test",
            citations=[],
            links=[],
            size=100,
        )
        assert page.kind is None

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            WikiPage(
                path="docs/wiki/test.md",
                kind="reference",
                title="Test",
                citations=[],
                links=[],
                size=100,
                unknown_field="bad",
            )


class TestRepositoryLayout:
    """Test RepositoryLayout model."""

    def test_basic_creation(self):
        layout = RepositoryLayout(
            project_name="MyProject",
            packages=[
                {"path": "src/auth", "size": 5000, "has_tests": True},
                {"path": "src/api", "size": 3000, "has_tests": False},
            ],
            has_ci=True,
            test_command_hint="pytest",
        )
        assert layout.project_name == "MyProject"
        assert len(layout.packages) == 2

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            RepositoryLayout(
                project_name="MyProject",
                packages=[],
                has_ci=True,
                test_command_hint="pytest",
                unexpected_field="bad",
            )


class TestPagePlan:
    """Test PagePlan model."""

    def test_basic_creation(self):
        plan = PagePlan(
            path="docs/wiki/payments.md",
            kind="reference",
            source="brief",
            justification="Requested in documentation brief",
            exists=False,
            say=["How payments are processed", "API structure"],
            modules=["src/payments/api.py"],
        )
        assert plan.path == "docs/wiki/payments.md"
        assert plan.source == "brief"

    def test_say_and_modules_default_to_empty(self):
        plan = PagePlan(
            path="docs/wiki/test.md",
            kind="how-to",
            source="inventory",
            justification="Cites changed file",
            exists=True,
        )
        assert plan.say == []
        assert plan.modules == []


class TestPageEvidence:
    """Test PageEvidence model."""

    def test_basic_creation(self):
        evidence = PageEvidence(
            commands=[
                {
                    "command": "head -20 src/lib.py",
                    "exit_code": 0,
                    "chars": 500,
                    "refused": False,
                    "reason": "",
                }
            ],
            chars=500,
        )
        assert len(evidence.commands) == 1
        assert evidence.chars == 500

    def test_empty_commands(self):
        evidence = PageEvidence(commands=[], chars=0)
        assert evidence.commands == []


class TestPageResult:
    """Test PageResult model."""

    def test_basic_creation(self):
        result = PageResult(
            path="docs/wiki/test.md",
            kind="reference",
            action="write",
            reason="",
            citations_checked=3,
            citations_missing=[],
            links_missing=[],
            contract_failures=[],
            size=1200,
            dropped=False,
            drop_reason=None,
        )
        assert result.path == "docs/wiki/test.md"
        assert result.action == "write"
        assert not result.dropped

    def test_dropped_with_reason(self):
        result = PageResult(
            path="docs/wiki/bad.md",
            kind=None,
            action="write",
            reason="",
            citations_checked=1,
            citations_missing=["src/nonexistent.py"],
            links_missing=[],
            contract_failures=[],
            size=800,
            dropped=True,
            drop_reason="Missing citation: src/nonexistent.py",
        )
        assert result.dropped
        assert result.drop_reason is not None

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            PageResult(
                path="docs/wiki/test.md",
                kind="reference",
                action="write",
                dropped=False,
                bad_field="not allowed",
            )


class TestDocsRecord:
    """Test DocsRecord model."""

    def test_basic_creation(self):
        record = DocsRecord(
            mode="update",
            brief_present=True,
            changed_files=["src/lib.py", "tests/test_lib.py"],
            plan=[
                PagePlan(
                    path="docs/wiki/lib.md",
                    kind="reference",
                    source="brief",
                    justification="Named in brief",
                    exists=False,
                )
            ],
            results=[
                PageResult(
                    path="docs/wiki/lib.md",
                    kind="reference",
                    action="write",
                    reason="",
                    citations_checked=1,
                    citations_missing=[],
                    links_missing=[],
                    contract_failures=[],
                    size=1000,
                    dropped=False,
                    drop_reason=None,
                )
            ],
            files_written=["docs/wiki/lib.md"],
            verdict="docs_committed",
        )
        assert record.mode == "update"
        assert len(record.plan) == 1

    def test_plan_max_items_8(self):
        """Plan must not exceed 8 items per schema."""
        plans = [
            PagePlan(
                path=f"docs/wiki/page{i}.md",
                kind="reference",
                source="brief",
                justification=f"Page {i}",
                exists=False,
            )
            for i in range(9)
        ]
        with pytest.raises(ValidationError) as exc_info:
            DocsRecord(
                mode="update",
                brief_present=True,
                changed_files=[],
                plan=plans,
                results=[],
                files_written=[],
                verdict="docs_committed",
            )
        error_text = str(exc_info.value).lower()
        assert "maxItems" in error_text or "too_long" in error_text or "at most 8" in error_text

    def test_results_max_items_8(self):
        """Results must not exceed 8 items per schema."""
        results = [
            PageResult(
                path=f"docs/wiki/page{i}.md",
                kind="reference",
                action="write",
                reason="",
                citations_checked=0,
                dropped=False,
            )
            for i in range(9)
        ]
        with pytest.raises(ValidationError) as exc_info:
            DocsRecord(
                mode="update",
                brief_present=True,
                changed_files=[],
                plan=[],
                results=results,
                files_written=[],
                verdict="docs_committed",
            )
        error_text = str(exc_info.value).lower()
        assert "maxItems" in error_text or "too_long" in error_text or "at most 8" in error_text

    def test_jsonschema_validation(self):
        """DocsRecord must validate against the JSON schema."""
        record = DocsRecord(
            mode="update",
            brief_present=False,
            changed_files=["src/main.py"],
            plan=[],
            results=[],
            files_written=[],
            verdict="docs_committed",
        )
        # Convert to dict and validate against schema
        record_dict = record.model_dump()
        schema_path = (
            Path(__file__).resolve().parents[4]
            / "specs/171-documenter-workflow/contracts/docs-record.schema.json"
        )
        assert schema_path.exists(), f"Schema not found at {schema_path}"

        import jsonschema

        with open(schema_path) as f:
            schema = json.load(f)
        # Should not raise
        jsonschema.validate(record_dict, schema)

    def test_env_blocked_with_reason(self):
        record = DocsRecord(
            mode="update",
            brief_present=False,
            changed_files=[],
            plan=[],
            results=[],
            files_written=[],
            verdict="env_blocked",
            hold_reason="Uncommitted changes in tree",
        )
        assert record.verdict == "env_blocked"
        assert record.hold_reason == "Uncommitted changes in tree"


class TestModelPageWriteSchema:
    """Test the dynamically generated ModelPageWrite schema."""

    def test_basic_validation(self):
        """Test that the schema rejects bad actions and missing fields."""
        schema_cls = model_page_write_schema(12000)
        valid_data = {"action": "write", "content": "# Test\n\nContent here", "reason": ""}
        instance = schema_cls(**valid_data)
        assert instance.action == "write"

    def test_content_size_bound(self):
        """Test that content respects the max_chars bound."""
        schema_cls = model_page_write_schema(100)
        with pytest.raises(ValidationError):
            schema_cls(action="write", content="x" * 101, reason="")

    def test_reason_size_bound(self):
        """Test that reason respects the 300 char bound."""
        schema_cls = model_page_write_schema(12000)
        with pytest.raises(ValidationError):
            schema_cls(action="write", content="", reason="x" * 301)

    def test_rejects_path_field(self):
        """Test that the schema rejects a path field (FR-006)."""
        schema_cls = model_page_write_schema(12000)
        with pytest.raises(ValidationError):
            schema_cls(
                action="write",
                content="Test",
                reason="",
                path="docs/wiki/test.md",
            )

    def test_rejects_unknown_action(self):
        """Test that invalid action values are rejected."""
        schema_cls = model_page_write_schema(12000)
        with pytest.raises(ValidationError):
            schema_cls(action="invalid", content="", reason="")

    def test_extra_forbid_rejects_unknown_fields(self):
        """Test that extra='forbid' rejects unknown fields."""
        schema_cls = model_page_write_schema(12000)
        with pytest.raises(ValidationError):
            schema_cls(
                action="write",
                content="Test",
                reason="",
                unknown_field="not allowed",
            )

    def test_retired_action(self):
        """Test that 'retire' action is accepted."""
        schema_cls = model_page_write_schema(12000)
        instance = schema_cls(action="retire", content="", reason="No longer used")
        assert instance.action == "retire"

    def test_unchanged_action(self):
        """Test that 'unchanged' action is accepted."""
        schema_cls = model_page_write_schema(12000)
        instance = schema_cls(action="unchanged", content="", reason="No changes needed")
        assert instance.action == "unchanged"

    def test_content_defaults_to_empty(self):
        """Test that content defaults to empty string."""
        schema_cls = model_page_write_schema(12000)
        instance = schema_cls(action="unchanged", reason="No changes")
        assert instance.content == ""

    def test_reason_defaults_to_empty(self):
        """Test that reason defaults to empty string."""
        schema_cls = model_page_write_schema(12000)
        instance = schema_cls(action="write", content="Test content")
        assert instance.reason == ""
