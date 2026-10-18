"""Complete pinned repository bundles, independent of bounded model evidence."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import posixpath
import re
import tarfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_BLOB_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 100_000
CHUNK_BYTES = 64 * 1024


class ExecutionSourceError(ValueError):
    """Source admission failed; preserve a safe machine-readable reason."""
    def __init__(self, message: str, *, code: str = 'execution_source_invalid') -> None:
        self.code = code
        super().__init__(message)


def source_refusal(message: str, *, code: str = 'execution_source_invalid') -> NoReturn:
    raise ExecutionSourceError(message, code=code) from None


def checked_path(path: str) -> str:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(p in {"", ".", "..", ".git"} for p in path.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
    ):
        raise ExecutionSourceError("execution source path is invalid")
    return path


def immutable_object(value: str) -> str:
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise ExecutionSourceError("execution source requires immutable Git object identity")
    return value


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    mode: Literal["100644", "100755", "120000"]
    object_id: str
    size: int = Field(ge=0)
    _path = field_validator("path")(checked_path)
    _object = field_validator("object_id")(immutable_object)


class ExecutionSourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    source_commit: str
    tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=0)
    expanded_bytes: int = Field(ge=0)
    _commit = field_validator("source_commit")(immutable_object)

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.model_dump())).hexdigest()


@dataclass(frozen=True)
class ExecutionSourceBundle:
    identity: ExecutionSourceIdentity
    payload_path: Path
    payload_sha256: str
    payload_bytes: int


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def source_identity(commit: str, entries: list[SourceEntry]) -> ExecutionSourceIdentity:
    immutable_object(commit)
    ordered = sorted(entries, key=lambda e: e.path)
    if len(ordered) > MAX_ENTRIES or len({e.path for e in ordered}) != len(ordered):
        raise ExecutionSourceError("execution source entry count or uniqueness is invalid")
    paths = {e.path for e in ordered}
    for entry in ordered:
        if (
            len(entry.object_id) != len(commit)
            or entry.size > MAX_BLOB_BYTES
            or any(
                "/".join(entry.path.split("/")[:i]) in paths
                for i in range(1, len(entry.path.split("/")))
            )
        ):
            raise ExecutionSourceError("execution source tree identity or file layout is invalid")
    size = sum(e.size for e in ordered)
    if size > MAX_EXPANDED_BYTES:
        raise ExecutionSourceError("execution source exceeds expanded byte bound")
    return ExecutionSourceIdentity(
        source_commit=commit,
        tree_digest=hashlib.sha256(canonical([e.model_dump() for e in ordered])).hexdigest(),
        file_count=len(ordered),
        expanded_bytes=size,
    )


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionSourceError("execution source metadata contains duplicate keys")
        result[key] = value
    return result


class _BoundedReader(io.BufferedIOBase):
    def __init__(self, stream: gzip.GzipFile) -> None:
        self.stream = stream
        self.count = 0

    def read(self, size: int | None = -1) -> bytes:
        # Bound tar padding/extension records too, not only declared file bytes.
        ceiling = MAX_EXPANDED_BYTES + MAX_ENTRIES * 2048 + 10240
        data = self.stream.read(min(size if size is not None and size >= 0 else CHUNK_BYTES, ceiling - self.count + 1))
        self.count += len(data)
        if self.count > ceiling:
            raise ExecutionSourceError("execution source decompression exceeds byte bound")
        return data


def bundle_members(path: Path) -> Iterator[tuple[tarfile.TarFile, tarfile.TarInfo]]:
    if path.is_symlink() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ExecutionSourceError("execution source archive exceeds byte bound", code="execution_source_limit")
    try:
        with gzip.open(path, "rb") as compressed:
            reader = _BoundedReader(compressed)
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    yield archive, member
            while reader.read(CHUNK_BYTES):
                pass
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ExecutionSourceError("execution source archive is malformed") from exc


def resolved_source_path(
    path: str, entries: Mapping[str, SourceEntry], links: Mapping[str, str],
) -> str:
    pending = path.split("/")
    resolved: list[str] = []
    visited: set[str] = set()
    while pending:
        part = pending.pop(0)
        candidate = "/".join([*resolved, part])
        if candidate in links:
            if candidate in visited or len(visited) >= 8:
                raise ExecutionSourceError("execution source symlink cycle or depth exceeded")
            visited.add(candidate)
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(candidate), links[candidate]),
            )
            checked_path(target)
            pending = target.split("/") + pending
            resolved = []
        else:
            resolved.append(part)
    result = "/".join(resolved)
    if result not in entries and not any(p.startswith(result + "/") for p in entries):
        raise ExecutionSourceError("execution source symlink target missing")
    return result


def validate_execution_bundle(
    path: Path,
    *,
    expected: ExecutionSourceIdentity | None = None,
    review_inputs: Mapping[str, str] | None = None,
) -> ExecutionSourceIdentity:
    entries: dict[str, SourceEntry] = {}
    identity = None
    seen: set[str] = set()
    links: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for archive, member in bundle_members(path):
        if identity is None:
            if (
                member.name != "metadata.json"
                or not member.isfile()
                or not 0 < member.size <= MAX_METADATA_BYTES
            ):
                raise ExecutionSourceError("execution source metadata missing or oversized")
            stream = archive.extractfile(member)
            if stream is None:
                raise ExecutionSourceError("execution source metadata missing")
            data = json.loads(stream.read(MAX_METADATA_BYTES + 1), object_pairs_hook=_unique)
            if (
                not isinstance(data, dict)
                or set(data) != {"identity", "entries"}
                or not isinstance(data["entries"], list)
            ):
                raise ExecutionSourceError("execution source metadata invalid")
            if len(data["entries"]) > MAX_ENTRIES:
                raise ExecutionSourceError("execution source entry bound exceeded")
            declared = ExecutionSourceIdentity.model_validate(data["identity"])
            ordered = [SourceEntry.model_validate(item) for item in data["entries"]]
            identity = source_identity(declared.source_commit, ordered)
            if (
                identity != declared
                or (expected is not None and identity != expected)
                or member.size + identity.expanded_bytes > MAX_EXPANDED_BYTES
            ):
                raise ExecutionSourceError("execution source identity differs or exceeds bound")
            entries = {e.path: e for e in ordered}
            continue
        if not member.name.startswith("files/"):
            raise ExecutionSourceError("execution source contains unexpected member")
        name = checked_path(member.name[len("files/") :])
        entry = entries.get(name)
        if entry is None or name in seen:
            raise ExecutionSourceError("execution source contains extra or duplicate member")
        seen.add(name)
        digest = hashlib.new("sha1" if len(entry.object_id) == 40 else "sha256")
        digest.update(b"blob " + str(entry.size).encode() + b"\0")
        sha256 = hashlib.sha256()
        if entry.mode == "120000":
            if (
                not member.issym()
                or member.size != 0
                or member.linkname.startswith("/")
                or "\\" in member.linkname
            ):
                raise ExecutionSourceError("execution source link invalid")
            raw = member.linkname.encode("utf-8")
            if len(raw) != entry.size:
                raise ExecutionSourceError("execution source link size differs")
            links[name] = member.linkname
            digest.update(raw)
            sha256.update(raw)
        else:
            if (
                not member.isfile()
                or member.size != entry.size
                or bool(member.mode & 0o111) != (entry.mode == "100755")
            ):
                raise ExecutionSourceError("execution source file mode or size differs")
            stream = archive.extractfile(member)
            if stream is None:
                raise ExecutionSourceError("execution source file missing")
            consumed = 0
            while chunk := stream.read(CHUNK_BYTES):
                if consumed == 0 and chunk.startswith(
                    b"version https://git-lfs.github.com/spec/v1\n",
                ):
                    raise ExecutionSourceError("execution source LFS hydration unsupported", code="execution_source_unsupported")
                consumed += len(chunk)
                if consumed > entry.size:
                    raise ExecutionSourceError("execution source file exceeds byte bound")
                digest.update(chunk)
                sha256.update(chunk)
            if consumed != entry.size:
                raise ExecutionSourceError("execution source file truncated")
        if digest.hexdigest() != entry.object_id:
            raise ExecutionSourceError("execution source blob identity differs")
        hashes[name] = sha256.hexdigest()
    if identity is None or seen != set(entries):
        raise ExecutionSourceError("execution source file set is incomplete")
    for link in links:
        resolved_source_path(link, entries, links)
    for name, expected_digest in (review_inputs or {}).items():
        checked_path(name)
        target = resolved_source_path(name, entries, links)
        if hashes.get(target) != expected_digest:
            raise ExecutionSourceError("execution source differs from reviewed inputs")
    return identity
