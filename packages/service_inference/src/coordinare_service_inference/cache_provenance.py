"""Bounded provenance walks shared by the verifier and consumer images.

Records follow spec 234's sorted descriptor walk and SHA256 encoding. Content
records omit inode and ctime so a full pass can distinguish drift from stat churn.
No manifest leaves the verifier, and no code from the cache is imported.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_SCRIPT_BYTES = 256 * 1024
MAX_PATH_BYTES = 512
MAX_RESULT_BYTES = 1024 * 1024
MAX_LINK_HOPS = 40
READ_CHUNK_BYTES = 64 * 1024
RESULT_SENTINEL = "COORDINARE_PROVENANCE="
DEFAULT_MAX_ENTRIES = 1_000_000
DEFAULT_MAX_BYTES = 16 * 1024**3
SCRIPT_PATHS = ("activate.sh", "services/services-start.sh", "services/services-health.sh")


class WalkRefusedError(Exception):
    """A missing cache, unsafe entry or bound refuses the entire result."""


@dataclass(frozen=True)
class Entry:
    rel: str
    kind: str
    mode: int
    size: int
    ino: int
    ctime_ns: int
    sha256: str | None = None
    target: str | None = None


def _directory(name: str | Path, *, parent: int | None = None) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)


def _file_hash(parent: int, name: str, info: os.stat_result, max_bytes: int) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _identity(opened) != _identity(info):
            message = f"{name} changed while being opened"
            raise WalkRefusedError(message)
        digest = hashlib.sha256()
        remaining = max_bytes
        while chunk := os.read(fd, READ_CHUNK_BYTES):
            remaining -= len(chunk)
            if remaining < 0:
                message = "cache exceeds env_cache.provenance_max_bytes"
                raise WalkRefusedError(message)
            digest.update(chunk)
        if _identity(os.fstat(fd)) != _identity(opened):
            message = f"{name} changed while being read"
            raise WalkRefusedError(message)
        return digest.hexdigest()
    finally:
        os.close(fd)


@dataclass
class _Walk:
    max_entries: int
    max_bytes: int
    hash_files: bool
    count: int = 0
    total: int = 0

    def entries(self, fd: int, prefix: str = "") -> Iterator[Entry]:
        with os.scandir(fd) as scan:
            names = sorted(entry.name for entry in scan)
        for name in names:
            self.count += 1
            if self.count > self.max_entries:
                message = "cache exceeds env_cache.provenance_max_entries"
                raise WalkRefusedError(message)
            rel = prefix + name
            if len(rel.encode("utf-8", "surrogateescape")) > MAX_PATH_BYTES:
                message = "cache path exceeds provenance path bound"
                raise WalkRefusedError(message)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                yield Entry(rel, "dir", mode, 0, info.st_ino, info.st_ctime_ns)
                child = _directory(name, parent=fd)
                try:
                    yield from self.entries(child, rel + "/")
                finally:
                    os.close(child)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=fd)
                yield Entry(rel, "link", mode, len(target), info.st_ino, info.st_ctime_ns, target=target)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    message = f"{rel} is hard linked"
                    raise WalkRefusedError(message)
                self.total += info.st_size
                if self.total > self.max_bytes:
                    message = "cache exceeds env_cache.provenance_max_bytes"
                    raise WalkRefusedError(message)
                digest = _file_hash(fd, name, info, self.max_bytes) if self.hash_files else None
                yield Entry(rel, "file", mode, info.st_size, info.st_ino, info.st_ctime_ns, digest)
            else:
                message = f"{rel} is not a regular file, directory or link"
                raise WalkRefusedError(message)


def _entries(cache: Path, walk: _Walk) -> Iterator[Entry]:
    parent = _directory(cache.parent.resolve())
    try:
        fd = _directory(cache.name, parent=parent)
        try:
            yield from walk.entries(fd)
        finally:
            os.close(fd)
    except FileNotFoundError as exc:
        message = "cache directory or entry is missing"
        raise WalkRefusedError(message) from exc
    finally:
        os.close(parent)


def _stat_record(entry: Entry) -> bytes:
    fields = (entry.rel, entry.kind, str(entry.mode), str(entry.size), str(entry.ino), str(entry.ctime_ns), entry.target or "")
    return "".join(f"{field}\0" for field in fields).encode("utf-8", "surrogateescape")


def stat_digest(cache: Path, *, max_entries: int, max_bytes: int) -> str:
    """Spec 234's exact stat digest, before any consumer activation."""
    digest = hashlib.sha256()
    for entry in _entries(cache, _Walk(max_entries, max_bytes, hash_files=False)):
        digest.update(_stat_record(entry))
    return digest.hexdigest()


def _check_links(links: dict[str, str]) -> None:
    for rel, target in links.items():
        current = list(PurePosixPath(rel).parent.parts)
        stack = list(reversed(target.split("/")))
        hops = 0
        if not target or target.startswith("/"):
            message = f"link {rel} leaves the cache"
            raise WalkRefusedError(message)
        while stack:
            part = stack.pop()
            if part in ("", "."):
                continue
            if part == "..":
                if not current:
                    message = f"link {rel} leaves the cache"
                    raise WalkRefusedError(message)
                current.pop()
                continue
            key = "/".join([*current, part])
            if key in links:
                hops += 1
                through = links[key]
                if hops > MAX_LINK_HOPS or not through or through.startswith("/"):
                    message = f"link {rel} leaves the cache or exceeds the link bound"
                    raise WalkRefusedError(message)
                stack.extend(reversed(through.split("/")))
            else:
                current.append(part)


def _script(cache: Path, rel: str, digest: str) -> str:
    fd = _directory(cache.parent.resolve())
    try:
        for part in (cache.name, *PurePosixPath(rel).parts[:-1]):
            child = _directory(part, parent=fd)
            os.close(fd)
            fd = child
        leaf = os.open(PurePosixPath(rel).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(leaf)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_SCRIPT_BYTES:
                message = f"{rel} is not a bounded plain script"
                raise WalkRefusedError(message)
            with os.fdopen(leaf, "rb", closefd=False) as stream:
                data = stream.read(MAX_SCRIPT_BYTES + 1)
        finally:
            os.close(leaf)
    finally:
        os.close(fd)
    if len(data) > MAX_SCRIPT_BYTES or hashlib.sha256(data).hexdigest() != digest:
        message = f"{rel} changed during the walk or exceeds the script bound"
        raise WalkRefusedError(message)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        message = f"{rel} is not UTF-8"
        raise WalkRefusedError(message) from exc


def walk_summary(cache: Path, activate_sh: str, *, max_entries: int, max_bytes: int) -> dict[str, Any]:
    """Walk content once and return only digests, counters and frozen consumer inputs."""
    walk = _Walk(max_entries, max_bytes, hash_files=True)
    content = hashlib.sha256()
    stats = hashlib.sha256()
    links: dict[str, str] = {}
    debs: dict[str, str] = {}
    scripts: dict[str, str] = {}
    inputs: dict[str, Any] = {"activate_sh": activate_sh, "services_start_sh": None, "services_health_sh": None, "debs": debs}
    small_bytes = len(activate_sh.encode("utf-8"))
    for entry in _entries(cache, walk):
        stats.update(_stat_record(entry))
        row = [entry.rel, entry.kind, entry.mode, entry.size, entry.sha256, entry.target]
        content.update((json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8", "surrogateescape"))
        if entry.kind == "link":
            links[entry.rel] = entry.target or ""
        if entry.kind == "file" and entry.rel.startswith("debs/") and entry.rel.endswith(".deb"):
            debs[entry.rel] = entry.sha256 or ""
            small_bytes += len(entry.rel.encode("utf-8")) + len(debs[entry.rel])
            if small_bytes > MAX_RESULT_BYTES:
                message = "consumer inputs exceed the verifier result bound"
                raise WalkRefusedError(message)
        if entry.rel in SCRIPT_PATHS and entry.sha256 is not None:
            scripts[entry.rel] = entry.sha256
    _check_links(links)
    if scripts.get("activate.sh") != hashlib.sha256(activate_sh.encode("utf-8")).hexdigest():
        message = "activate.sh does not match the daemon-rendered text"
        raise WalkRefusedError(message)
    for path, key in zip(SCRIPT_PATHS[1:], ("services_start_sh", "services_health_sh"), strict=True):
        if path in scripts:
            inputs[key] = _script(cache, path, scripts[path])
    result = {"content_digest": content.hexdigest(), "stat_digest": stats.hexdigest(), "entry_count": walk.count,
              "total_bytes": walk.total, "inputs": inputs}
    if len(json.dumps(result).encode("utf-8")) > MAX_RESULT_BYTES:
        message = "consumer inputs exceed the verifier result bound"
        raise WalkRefusedError(message)
    return result
