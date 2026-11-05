"""Spike #376: the evidence cross-check between nare read records and coordinare digests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "spikes" / "nare_discovery.py"
_spec = importlib.util.spec_from_file_location("nare_discovery_spike", SCRIPT)
assert _spec is not None and _spec.loader is not None
spike = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spike)

A, B = "a" * 64, "b" * 64


def _read(path: str, sha: str, size: int, start: int, end: int, **extra: object) -> dict[str, object]:
    return {"tool": "read", "path": path, "sha256": sha, "size": size,
            "bytes": {"start": start, "end": end}, **extra}


def test_whole_file_is_the_union_of_contiguous_ranges() -> None:
    reads = spike.read_records([
        _read("x", A, 100, 0, 60), _read("x", A, 100, 50, 100),
        _read("y", B, 100, 0, 40), _read("y", B, 100, 60, 100),
        {"tool": "search", "path": "", "files": [{"path": "z", "sha256": A}]},
    ])
    assert reads["x"]["whole_file"] is True
    assert reads["y"]["whole_file"] is False
    assert "z" not in reads


def test_cross_check_flags_mismatch_unread_and_partial_citations() -> None:
    reads = spike.read_records([_read("ok", A, 10, 0, 10), _read("bad", A, 10, 0, 10),
                                _read("part", A, 10, 0, 4)])
    check = spike.cross_check(reads, {"ok": A, "bad": B, "part": A}, {"ok", "part", "never"})
    assert check["digests_matched"] == 2
    assert check["digest_mismatches"] == ["bad"]
    assert check["cited_unread"] == ["never"]
    assert check["cited_partial"] == ["part"]
    assert check["complete"] is False


def test_cross_check_complete_when_every_citation_matches() -> None:
    reads = spike.read_records([_read("ok", A, 10, 0, 10)])
    assert spike.cross_check(reads, {"ok": A}, {"ok"})["complete"] is True


def test_a_file_read_at_two_versions_never_matches() -> None:
    reads = spike.read_records([_read("x", A, 10, 0, 10), _read("x", B, 10, 0, 10)])
    assert spike.cross_check(reads, {"x": A}, {"x"})["digest_mismatches"] == ["x"]


def test_schema_transform_opens_only_schema_valued_maps() -> None:
    schema = {"properties": {"env": {"type": "object", "additionalProperties": {"type": "string"}},
                             "strict": {"type": "object", "additionalProperties": False}}}
    loosened, paths = spike.nare_schema(schema)
    assert loosened["properties"]["env"]["additionalProperties"] is True
    assert loosened["properties"]["strict"]["additionalProperties"] is False
    assert paths == ["#/properties/env"]
    assert schema["properties"]["env"]["additionalProperties"] == {"type": "string"}
