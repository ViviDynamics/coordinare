"""Pinned Git-object source adapter for private validation snapshots only."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from typing import TYPE_CHECKING

from coordinare_service_inference.environment.execution_source import (
    MAX_METADATA_BYTES,
    SourceEntry,
    immutable_object,
    source_identity,
)

from coordinare.services.environment_source_provider import write_execution_bundle

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare_service_inference.environment.execution_source import ExecutionSourceBundle
    from scripts.validate_environment_repositories import Snapshot


async def prepare_snapshot_source(
    snapshot: Snapshot, *, commit: str, destination: Path, deadline: float,
) -> ExecutionSourceBundle:
    immutable_object(commit)
    if commit != snapshot.commit:
        raise ValueError("snapshot source identity differs")
    if asyncio.get_running_loop().time() >= deadline:
        raise TimeoutError("snapshot source deadline exceeded")
    raw = await snapshot.git("ls-tree", "-rzl", "--full-tree", commit, max_bytes=MAX_METADATA_BYTES)
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, kind, oid, size = metadata.decode("ascii").split()
        if kind != "blob":
            raise ValueError("snapshot source submodules unsupported")
        entries.append(
            SourceEntry(path=name.decode("utf-8"), mode=mode, object_id=oid, size=int(size)),
        )
    source_identity(commit, entries)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    objects = destination / "objects"
    objects.mkdir()
    unique = {entry.object_id: entry for entry in entries}
    env = dict(
        os.environ,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OPTIONAL_LOCKS="0",
    )
    process = None
    writer = None
    try:
        async with asyncio.timeout_at(deadline):
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(snapshot.source),
                "cat-file",
                "--batch",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            if process.stdin is None or process.stdout is None:
                raise ValueError("snapshot object transport unavailable")

            async def feed() -> None:
                if process is None or process.stdin is None:
                    raise ValueError("snapshot object input unavailable")
                for oid in unique:
                    process.stdin.write((oid + "\n").encode())
                    await process.stdin.drain()
                process.stdin.close()

            writer = asyncio.create_task(feed())
            for oid, entry in unique.items():
                header = await process.stdout.readline()
                if header.decode("ascii").strip().split() != [oid, "blob", str(entry.size)]:
                    raise ValueError("snapshot Git object header differs")
                remaining = entry.size
                with (objects / oid).open("xb") as output:
                    while remaining:
                        chunk = await process.stdout.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise ValueError("snapshot Git object truncated")
                        remaining -= len(chunk)
                        output.write(chunk)
                if await process.stdout.readexactly(1) != b"\n":
                    raise ValueError("snapshot Git object terminator differs")
            await writer
            if await process.stdout.read(1):
                raise ValueError("snapshot Git object output contains extra data")
            await process.wait()
            if process.returncode:
                raise ValueError("snapshot Git object transport failed")
            bundle = write_execution_bundle(
                commit, entries, objects, destination / "source.tgz", deadline=deadline,
            )
            shutil.rmtree(objects)
            return bundle
    except BaseException:
        shutil.rmtree(destination)
        raise
    finally:
        if writer is not None:
            if not writer.done():
                writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
                await writer
        if process is not None:
            if process.returncode is None:
                process.kill()
            async with asyncio.timeout(2):
                if process.stdout is not None:
                    while await process.stdout.read(64 * 1024):
                        pass
                await process.wait()
