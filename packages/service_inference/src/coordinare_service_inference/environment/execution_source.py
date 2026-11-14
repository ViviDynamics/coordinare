"""Complete pinned repository bundles, independent of bounded model evidence."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .inventory import InventoryEntry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Mapping
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
    components = path.split("/")
    resolved: list[str] = []
    visited: set[str] = set()
    while components:
        part = components.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                source_refusal("execution source symlink escapes owned root")
            resolved.pop()
            continue
        candidate = "/".join([*resolved, part])
        if candidate in links:
            if candidate in visited or len(visited) >= 8:
                source_refusal("execution source symlink cycle or depth exceeded")
            visited.add(candidate)
            # Follow intermediate links before processing subsequent parent steps,
            # just as the filesystem does. Lexical normalization is unsafe here.
            target = links[candidate]
            if target.startswith("/"):
                source_refusal("execution source symlink escapes owned root")
            components = target.split("/") + components
        else:
            if components and (candidate in entries or not any(
                name.startswith(candidate + "/") for name in entries
            )):
                source_refusal("execution source symlink traverses a missing or non-directory")
            resolved.append(part)
    result = "/".join(resolved)
    if result and result not in entries and not any(p.startswith(result + "/") for p in entries):
        raise ExecutionSourceError("execution source symlink target missing")
    return result


async def read_source_reference(
    path: str, links: Collection[str], fetch: Callable[[str], Awaitable[str | None]],
) -> str | None:
    """Read pinned text evidence with the same component ordering as source access."""
    checked_path(path)
    components = path.split("/")
    resolved: list[str] = []
    seen: set[str] = set()
    while components:
        part = components.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                source_refusal("execution source symlink escapes owned root")
            resolved.pop()
            continue
        current = "/".join([*resolved, part])
        if current in links:
            if current in seen or len(seen) >= 8:
                return None
            seen.add(current)
            target = await fetch(current)
            if target is None:
                return None
            if target.startswith("/") or "\\" in target:
                source_refusal("execution source link invalid")
            components = target.split("/") + components
        else:
            resolved.append(part)
    return await fetch(checked_path("/".join(resolved)))


def bundle_inventory(
    path: Path, *, expected: ExecutionSourceIdentity | Mapping[str, Any],
) -> dict[str, InventoryEntry]:
    """Pinned sizes, modes and recorded link targets from a verified bundle, without reading file contents."""
    identity = expected if isinstance(expected, ExecutionSourceIdentity) else ExecutionSourceIdentity.model_validate(expected)
    validate_execution_bundle(path, expected=identity)
    entries: dict[str, InventoryEntry] = {}
    targets: dict[str, str] = {}
    for archive, member in bundle_members(path):
        if member.name == "metadata.json" and not entries:
            stream = archive.extractfile(member)
            if stream is None:
                raise ExecutionSourceError("execution source metadata missing")
            data = json.loads(stream.read(MAX_METADATA_BYTES + 1), object_pairs_hook=_unique)
            for item in data["entries"]:
                entry = SourceEntry.model_validate(item)
                entries[entry.path] = InventoryEntry(entry.path, entry.size, entry.mode)
        elif member.issym():
            targets[checked_path(member.name[len("files/"):])] = member.linkname
    return {path: replace(entry, target=targets.get(path)) if entry.mode == "120000" else entry
            for path, entry in entries.items()}


def _blob_matches(entry: SourceEntry, data: bytes) -> bool:
    digest = hashlib.new("sha1" if len(entry.object_id) == 40 else "sha256")
    digest.update(b"blob " + str(entry.size).encode() + b"\0" + data)
    return digest.hexdigest() == entry.object_id


def bundle_texts(
    path: Path, names: Iterable[str], *, expected: ExecutionSourceIdentity, limit: int,
) -> tuple[dict[str, str], list[str]]:
    """Pinned text of the named regular files, read from the packed archive: no network and no working tree.

    Each file's bytes are checked against its pinned blob identity, so the text is exactly the pinned content. Symlinks
    and other non-files are never read. Files over ``limit`` are returned by name rather than read.
    """
    wanted = {checked_path(name) for name in names}
    entries: dict[str, SourceEntry] = {}
    texts: dict[str, str] = {}
    oversize: list[str] = []
    for archive, member in bundle_members(path):
        if member.name == "metadata.json" and not entries:
            stream = archive.extractfile(member)
            if stream is None:
                raise ExecutionSourceError("execution source metadata missing")
            data = json.loads(stream.read(MAX_METADATA_BYTES + 1), object_pairs_hook=_unique)
            declared = ExecutionSourceIdentity.model_validate(data["identity"])
            ordered = [SourceEntry.model_validate(item) for item in data["entries"]]
            if source_identity(declared.source_commit, ordered) != declared or declared != expected:
                raise ExecutionSourceError("execution source identity differs")
            entries = {entry.path: entry for entry in ordered}
            continue
        if not member.name.startswith("files/"):
            continue
        name = checked_path(member.name[len("files/"):])
        entry = entries.get(name)
        if name not in wanted or entry is None or entry.mode == "120000" or not member.isfile():
            continue
        if entry.size > limit:
            oversize.append(name)
            continue
        stream = archive.extractfile(member)
        if stream is None or member.size != entry.size:
            raise ExecutionSourceError("execution source file differs from pinned size")
        data = stream.read(entry.size + 1)
        if len(data) != entry.size or not _blob_matches(entry, data):
            raise ExecutionSourceError("execution source blob identity differs")
        texts[name] = data.decode("utf-8", "replace")
    return texts, sorted(oversize)


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
