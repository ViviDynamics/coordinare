"""Spec 126 — durable feedback-disposition file contract (performer side).

The implementer writes ``.coordinare/feedback_dispositions.json``; the harness
reads it when building the ``pr_opened`` terminal response. Any failure or
malformed shape degrades to [] ("nothing disputed") — never an error.
"""
from __future__ import annotations

import json
from pathlib import Path

from performer.main import _read_feedback_dispositions


def _write(tmp_path: Path, payload: object) -> Path:
    coordinare_dir = tmp_path / ".coordinare"
    coordinare_dir.mkdir(parents=True, exist_ok=True)
    (coordinare_dir / "feedback_dispositions.json").write_text(
        json.dumps(payload) if not isinstance(payload, str) else payload,
        encoding="utf-8",
    )
    return tmp_path


def test_reads_well_formed_dispositions(tmp_path: Path) -> None:
    _write(tmp_path, [
        {"id": "fb-1", "disposition": "addressed", "reason": "fixed in c1"},
        {"id": "fb-2", "disposition": "disputed", "reason": "already correct"},
    ])

    out = _read_feedback_dispositions(tmp_path)

    assert out == [
        {"id": "fb-1", "disposition": "addressed", "reason": "fixed in c1"},
        {"id": "fb-2", "disposition": "disputed", "reason": "already correct"},
    ]


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _read_feedback_dispositions(tmp_path) == []


def test_malformed_json_returns_empty(tmp_path: Path) -> None:
    _write(tmp_path, "{not json")
    assert _read_feedback_dispositions(tmp_path) == []


def test_wrong_shapes_filtered(tmp_path: Path) -> None:
    _write(tmp_path, [
        {"id": "fb-1", "disposition": "maybe"},        # bad disposition
        {"disposition": "addressed"},                   # no id
        "not-a-dict",
        {"id": "fb-2", "disposition": "disputed"},      # ok, reason defaults ""
        {"id": "fb-3", "disposition": "addressed", "reason": "x" * 900},
    ])

    out = _read_feedback_dispositions(tmp_path)

    assert [d["id"] for d in out] == ["fb-2", "fb-3"]
    assert out[0]["reason"] == ""
    assert len(out[1]["reason"]) == 500  # reason capped


def test_non_list_payload_returns_empty(tmp_path: Path) -> None:
    _write(tmp_path, {"id": "fb-1"})
    assert _read_feedback_dispositions(tmp_path) == []
