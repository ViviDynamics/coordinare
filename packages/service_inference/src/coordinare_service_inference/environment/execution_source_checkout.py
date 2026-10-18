"""Prove a performer's initial checkout matches the pinned execution-source tree."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from typing import TYPE_CHECKING

from coordinare_service_inference.environment.execution_source import (
    CHUNK_BYTES,
    MAX_METADATA_BYTES,
    SourceEntry,
    immutable_object,
    resolved_source_path,
    source_identity,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity


async def _git(repo: Path, *args: str) -> bytes:
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/git",
        "-C",
        str(repo),
        *args,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output = bytearray()
    try:
        async with asyncio.timeout(30):
            if process.stdout is None:
                raise ValueError("execution source Git output missing")
            while chunk := await process.stdout.read(CHUNK_BYTES):
                output.extend(chunk)
                if len(output) > MAX_METADATA_BYTES:
                    raise ValueError("execution source Git output exceeds bound")
            await process.wait()
            if process.returncode:
                raise ValueError("execution source pinned Git object unavailable")
            return bytes(output)
    finally:
        if process.returncode is None:
            process.kill()
        async with asyncio.timeout(2):
            if process.stdout is not None:
                while await process.stdout.read(CHUNK_BYTES):
                    pass
            await process.wait()


async def checkout_identity(
    repo: Path, *, commit: str, review_inputs: Mapping[str, str],
) -> ExecutionSourceIdentity:
    immutable_object(commit)
    if (await _git(repo, "rev-parse", "HEAD")).decode().strip() != commit:
        raise ValueError("execution source checkout commit differs")
    raw = await _git(repo, "ls-tree", "-rzl", "--full-tree", commit)
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, kind, oid, size = metadata.decode("ascii").split()
        if kind != "blob":
            raise ValueError("execution source checkout entry unsupported")
        entries.append(
            SourceEntry(path=name.decode("utf-8"), mode=mode, object_id=oid, size=int(size)),
        )
    identity = source_identity(commit, entries)
    by_path = {e.path: e for e in entries}
    links = {}
    hashes = {}
    root_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for entry in entries:
            parts = entry.path.split("/")
            parent_fd = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    child_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd,
                    )
                    os.close(parent_fd)
                    parent_fd = child_fd
                digest = hashlib.new("sha1" if len(entry.object_id) == 40 else "sha256")
                digest.update(b"blob " + str(entry.size).encode() + b"\0")
                review_hash = hashlib.sha256()
                if entry.mode == "120000":
                    target = os.readlink(parts[-1], dir_fd=parent_fd)
                    data = target.encode()
                    links[entry.path] = target
                    digest.update(data)
                    review_hash.update(data)
                else:
                    file_fd = os.open(
                        parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd,
                    )
                    with os.fdopen(file_fd, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_size != entry.size
                            or bool(info.st_mode & 0o111) != (entry.mode == "100755")
                        ):
                            raise ValueError("execution source checkout file identity differs")
                        while chunk := stream.read(CHUNK_BYTES):
                            if chunk.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                                raise ValueError("execution source LFS content unsupported")
                            digest.update(chunk)
                            review_hash.update(chunk)
                if digest.hexdigest() != entry.object_id:
                    raise ValueError("execution source checkout content differs")
                hashes[entry.path] = review_hash.hexdigest()
            finally:
                os.close(parent_fd)
    except OSError as exc:
        raise ValueError("execution source checkout file unavailable or unsafe") from exc
    finally:
        os.close(root_fd)
    for link_name in links:
        resolved_source_path(link_name, by_path, links)
    for review_path, digest_value in review_inputs.items():
        if hashes.get(resolved_source_path(review_path, by_path, links)) != digest_value:
            raise ValueError("execution source checkout review input differs")
    return identity
