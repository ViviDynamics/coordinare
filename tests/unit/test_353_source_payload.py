"""Catch truncated/corrupted evidence and unsafe clean-source materialization."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from coordinare_service_inference.environment.source_payload import (
    SourcePayload,
    decode_sources,
    encode_sources,
    materialize_sources,
)


def hashes(files):
    return {path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()}


def test_large_unicode_bundle_roundtrip():
    files = {"nested/a": "é" * 100_000, "other/b": "z" * 200_000}
    payload = encode_sources(files)
    expected = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert payload.size_bytes > 315_664
    assert b"".join(payload.chunks) == expected
    assert payload.sha256 == hashlib.sha256(expected).hexdigest()
    assert decode_sources(payload, expected_inputs=hashes(files)) == files


def test_document_bounds():
    files = {f"part-{i:02}": "x" * (256 * 1024) for i in range(64)}
    excess = len(json.dumps(files, separators=(",", ":")).encode()) - 16 * 1024 * 1024
    files["part-63"] = files["part-63"][:-excess]
    payload = encode_sources(files)
    assert payload.size_bytes == 16 * 1024 * 1024
    assert len(payload.chunks) == 256
    assert decode_sources(payload, expected_inputs=hashes(files)) == files
    files["part-63"] += "x"
    with pytest.raises(ValueError, match="source document"):
        encode_sources(files)


def test_escaped_text_bound():
    files = {"evidence": "\0" * (256 * 1024)}
    payload = encode_sources(files)
    assert payload.size_bytes > 256 * 1024
    assert decode_sources(payload, expected_inputs=hashes(files)) == files
    files = {f"file-{i}": "\0" * (256 * 1024) for i in range(11)}
    with pytest.raises(ValueError, match="source document"):
        encode_sources(files)


@pytest.mark.parametrize("mutation", ["corrupt", "missing", "duplicate", "extra", "reorder", "oversize", "length", "digest"])
def test_corrupt_missing_duplicate_extra_chunk(mutation):
    files = {"a": "a" * 100_000, "b": "b" * 100_000}
    payload = encode_sources(files)
    chunks = list(payload.chunks)
    if mutation == "corrupt":
        chunks[0] = b"!" + chunks[0][1:]
    elif mutation == "missing":
        chunks.pop()
    elif mutation == "duplicate":
        chunks[1] = chunks[0]
    elif mutation == "extra":
        chunks.append(b"x")
    elif mutation == "reorder":
        chunks.reverse()
    elif mutation == "oversize":
        chunks = [b"".join(chunks)]
    payload = replace(payload, chunks=tuple(chunks))
    if mutation == "length":
        payload = replace(payload, size_bytes=1)
    if mutation == "digest":
        payload = replace(payload, sha256="0" * 64)
    with pytest.raises(ValueError):
        decode_sources(payload, expected_inputs=hashes(files))


@pytest.mark.parametrize("inputs", [{}, {"wrong": "0" * 64}, {"evidence": "0" * 64}])
def test_exact_input_set_required(inputs):
    with pytest.raises(ValueError, match="source input"):
        decode_sources(encode_sources({"evidence": "abc"}), expected_inputs=inputs)


@pytest.mark.parametrize("path", ["../outside", "/outside", "x/../outside", "x//y", "a\\b", "."])
def test_source_paths_and_existing_symlink_refused(tmp_path, path):
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    with pytest.raises(ValueError):
        materialize_sources({path: "overwrite"}, tmp_path / "repo")
    assert outside.read_text() == "untouched"


def test_existing_symlink_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "repo"
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, FileExistsError)):
        materialize_sources({"file": "overwrite"}, destination)
    assert list(outside.iterdir()) == []


def test_materialization_creates_private_nested_sources(tmp_path):
    destination = tmp_path / "repo"
    materialize_sources({"deep/path/file": "exact\r\nbytes é", "root": "ok"}, destination)
    assert (destination / "deep/path/file").read_bytes() == "exact\r\nbytes é".encode()
    assert destination.stat().st_mode & 0o777 == 0o700
    with pytest.raises((ValueError, FileExistsError)):
        materialize_sources({"root": "changed"}, destination)
    assert (destination / "root").read_text() == "ok"


@pytest.mark.parametrize("raw", [b'{"a":"1","a":"2"}', b'{"a":1}', b'[]', b'{"a":"x","a/b":"y"}'])
def test_untrusted_document_shape_refused(raw):
    payload = SourcePayload(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), chunks=(raw,))
    with pytest.raises(ValueError):
        decode_sources(payload, expected_inputs={"a": hashlib.sha256(b"2").hexdigest()})


def test_individual_file_bound():
    with pytest.raises(ValueError, match="source file"):
        encode_sources({"huge": "x" * (256 * 1024 + 1)})


def test_verifier_admission_accepts_large_admitted_source():
    from coordinare.services.kubernetes_verifier import check_project_files

    check_project_files({"first": "x" * 150_000, "second": "y" * 200_000})
