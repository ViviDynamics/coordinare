"""Bounded, content-addressed source evidence shared by clean verifier transports."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from coordinare_service_inference.environment.models import source_path

SOURCE_DOCUMENT_MAX_BYTES = 16 * 1024 * 1024
SOURCE_FILE_MAX_BYTES = 256 * 1024
SOURCE_CHUNK_MAX_BYTES = 64 * 1024
SOURCE_MAX_CHUNKS = 256


@dataclass(frozen=True)
class SourcePayload:
    """Ordered chunks and trusted metadata, checked again before materialization."""

    sha256: str
    size_bytes: int
    chunks: tuple[bytes, ...]


def _checked_files(files: Mapping[str, str]) -> None:
    if not isinstance(files, Mapping):
        raise ValueError("source document must be a path-to-text mapping")
    for path, text in files.items():
        if not isinstance(path, str) or not isinstance(text, str):
            raise ValueError("source document must contain text paths and contents")
        source_path(path)
        if len(text.encode("utf-8")) > SOURCE_FILE_MAX_BYTES:
            raise ValueError("source file exceeds byte bound")
        parts = path.split("/")
        if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
            raise ValueError("source input paths overlap a file and directory")


def encode_sources(files: Mapping[str, str]) -> SourcePayload:
    """Canonicalize admitted source, bounding JSON escaping as well as raw text."""
    _checked_files(files)
    data = json.dumps(dict(files), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > SOURCE_DOCUMENT_MAX_BYTES:
        raise ValueError("source document exceeds byte bound")
    return SourcePayload(
        sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
        chunks=tuple(data[start:start + SOURCE_CHUNK_MAX_BYTES] for start in range(0, len(data), SOURCE_CHUNK_MAX_BYTES)),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("source document contains duplicate paths")
        result[key] = value
    return result


def decode_sources(payload: SourcePayload, *, expected_inputs: Mapping[str, str]) -> dict[str, str]:
    """Reject missing, corrupt, reordered, extra or unbound evidence before use."""
    if (type(payload.size_bytes) is not int or not 0 < payload.size_bytes <= SOURCE_DOCUMENT_MAX_BYTES
            or len(payload.chunks) != (payload.size_bytes + SOURCE_CHUNK_MAX_BYTES - 1) // SOURCE_CHUNK_MAX_BYTES
            or not 0 < len(payload.chunks) <= SOURCE_MAX_CHUNKS):
        raise ValueError("source document size or chunk count is invalid")
    if any(not isinstance(chunk, bytes) or not 0 < len(chunk) <= SOURCE_CHUNK_MAX_BYTES for chunk in payload.chunks):
        raise ValueError("source document chunk is invalid")
    data = b"".join(payload.chunks)
    if len(data) != payload.size_bytes or hashlib.sha256(data).hexdigest() != payload.sha256:
        raise ValueError("source document content identity differs")
    try:
        files = json.loads(data, object_pairs_hook=_unique_object)
        _checked_files(files)
    except (TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("source document is malformed") from exc
    canonical = encode_sources(files)
    if b"".join(canonical.chunks) != data:
        raise ValueError("source document is not canonical")
    if (set(files) != set(expected_inputs)
            or any(hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_inputs[path]
                   for path, text in files.items())):
        raise ValueError("source inputs differ from accepted manifest")
    return dict(files)


def materialize_sources(files: Mapping[str, str], destination: Path) -> None:
    """Write admitted source into a fresh directory without following symlinks.

    The caller owns the destination's parent. An existing destination is refused,
    even if empty. Directory descriptors keep nested writes anchored to that root.
    """
    encode_sources(files)
    destination.mkdir(mode=0o700)
    root_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for path, text in files.items():
            directory_fd = os.dup(root_fd)
            try:
                parts = path.split("/")
                for part in parts[:-1]:
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    os.close(directory_fd)
                    directory_fd = next_fd
                file_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o600, dir_fd=directory_fd)
                with os.fdopen(file_fd, "wb") as output:
                    output.write(text.encode("utf-8"))
            finally:
                os.close(directory_fd)
    finally:
        os.close(root_fd)


def restore_source_directory(payload_directory: Path, destination: Path) -> None:
    """Read an immutable projected payload and validate it before writing source."""
    with (payload_directory / "metadata.json").open("rb") as metadata_stream:
        metadata_bytes = metadata_stream.read(256 * 1024 + 1)
    if len(metadata_bytes) > 256 * 1024:
        raise ValueError("source metadata exceeds byte bound")
    metadata = json.loads(metadata_bytes, object_pairs_hook=_unique_object)
    if not isinstance(metadata, dict) or set(metadata) != {"sha256", "size_bytes", "count", "inputs"}:
        raise ValueError("source metadata fields are invalid")
    count = metadata["count"]
    if type(count) is not int or not 0 < count <= SOURCE_MAX_CHUNKS:
        raise ValueError("source metadata chunk count is invalid")
    expected_names = {"metadata.json", *(f"chunk-{index:04}" for index in range(count))}
    # Kubernetes' atomic projected-volume internals start with '..'. Payload keys
    # are generated by trusted code and never use that prefix.
    if {entry.name for entry in payload_directory.iterdir() if not entry.name.startswith("..")} != expected_names:
        raise ValueError("source metadata file set differs")
    chunks = []
    for index in range(count):
        with (payload_directory / f"chunk-{index:04}").open("rb") as stream:
            chunks.append(stream.read(SOURCE_CHUNK_MAX_BYTES + 1))
    payload = SourcePayload(metadata["sha256"], metadata["size_bytes"], tuple(chunks))
    inputs = metadata["inputs"]
    if not isinstance(inputs, dict):
        raise ValueError("source input identities are invalid")
    files = decode_sources(payload, expected_inputs=inputs)
    materialize_sources(files, destination)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    restore_source_directory(Path(sys.argv[1]), Path(sys.argv[2]))
