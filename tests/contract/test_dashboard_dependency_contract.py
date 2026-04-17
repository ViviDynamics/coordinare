"""Contract test: dashboard snapshot extension for dependency state (046).

Validates that the ``blocked_by_dependencies`` field in the dashboard
snapshot matches the JSON-schema contract defined in
``specs/046-card-dependency-detection/contracts/dashboard-snapshot-extension.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest


def _load_schema() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "specs"
        / "046-card-dependency-detection"
        / "contracts"
        / "dashboard-snapshot-extension.json"
    )
    return json.loads(path.read_text())


def test_empty_blocked_by_dependencies_validates() -> None:
    """Empty array (no blocking deps) passes the schema."""
    schema = _load_schema()
    data = {"blocked_by_dependencies": []}
    jsonschema.validate(data, schema)


def test_full_blocked_by_dependencies_validates() -> None:
    """Fully-populated blocker entry passes the schema."""
    schema = _load_schema()
    data = {
        "blocked_by_dependencies": [
            {
                "issue_number": 42,
                "title": "Set up theming",
                "column": "IN_PROGRESS",
                "issue_url": "https://github.com/o/r/issues/42",
                "source": "explicit",
            },
        ],
    }
    jsonschema.validate(data, schema)


def test_null_title_and_url_validates() -> None:
    """Blocker with null title and URL (off-board issue) passes."""
    schema = _load_schema()
    data = {
        "blocked_by_dependencies": [
            {
                "issue_number": 999,
                "title": None,
                "column": None,
                "issue_url": None,
                "source": "explicit",
            },
        ],
    }
    jsonschema.validate(data, schema)


def test_invalid_source_fails() -> None:
    """Source value outside enum should fail."""
    schema = _load_schema()
    data = {
        "blocked_by_dependencies": [
            {
                "issue_number": 1,
                "column": "TODO",
                "source": "magic",
            },
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)


def test_missing_issue_number_fails() -> None:
    """issue_number is required per the schema."""
    schema = _load_schema()
    data = {
        "blocked_by_dependencies": [
            {"column": "TODO", "source": "explicit"},
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)
