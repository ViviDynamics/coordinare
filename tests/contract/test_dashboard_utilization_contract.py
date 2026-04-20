"""Contract test: dashboard utilization schema for spec 048."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest


def _load_schema() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "specs"
        / "048-horizontal-performer-scaling"
        / "contracts"
        / "dashboard-utilization.json"
    )
    return json.loads(path.read_text())


def test_empty_utilization_validates() -> None:
    schema = _load_schema()
    jsonschema.validate({"role_utilization": []}, schema)


def test_full_utilization_validates() -> None:
    schema = _load_schema()
    data = {
        "role_utilization": [
            {"role": "implementing", "active": 2, "max": 3, "queued": 1},
            {"role": "reviewing", "active": 1, "max": 1, "queued": 0},
        ]
    }
    jsonschema.validate(data, schema)


def test_missing_required_field_fails() -> None:
    schema = _load_schema()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"role_utilization": [{"role": "implementing", "active": 1}]},
            schema,
        )


def test_negative_active_fails() -> None:
    schema = _load_schema()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"role_utilization": [{"role": "implementing", "active": -1, "max": 1}]},
            schema,
        )
