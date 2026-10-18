"""Restore a validated complete source bundle without writing through links."""

from __future__ import annotations

import os
import shutil
from contextlib import suppress
from typing import TYPE_CHECKING

from coordinare_service_inference.environment.execution_source import (
    bundle_members,
    validate_execution_bundle,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity


def restore_execution_bundle(
    path: Path,
    destination: Path,
    *,
    expected: ExecutionSourceIdentity,
    review_inputs: Mapping[str, str],
) -> None:
    validate_execution_bundle(path, expected=expected, review_inputs=review_inputs)
    if destination.exists() or destination.is_symlink():
        raise ValueError("execution source destination already exists")
    destination.mkdir(mode=0o700)
    root_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for archive, member in bundle_members(path):
            if member.name == "metadata.json":
                continue
            parts = member.name[len("files/") :].split("/")
            directory_fd = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                    next_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                    )
                    os.close(directory_fd)
                    directory_fd = next_fd
                if member.issym():
                    os.symlink(member.linkname, parts[-1], dir_fd=directory_fd)
                else:
                    file_fd = os.open(
                        parts[-1],
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o700 if member.mode & 0o111 else 0o600,
                        dir_fd=directory_fd,
                    )
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("execution source file missing")
                    with os.fdopen(file_fd, "wb") as output:
                        shutil.copyfileobj(stream, output, length=64 * 1024)
            finally:
                os.close(directory_fd)
        # Revalidate after writing as well: no consumer commands run on a changed payload.
        validate_execution_bundle(path, expected=expected, review_inputs=review_inputs)
    finally:
        os.close(root_fd)


def restore_projected_source(payload_directory: Path, destination: Path) -> None:
    """Admit bounded projected chunks before restoring a full source tree."""
    import hashlib
    import json
    import tempfile
    from pathlib import Path

    from coordinare_service_inference.environment.execution_source import (
        CHUNK_BYTES,
        MAX_ARCHIVE_BYTES,
        ExecutionSourceIdentity,
    )

    with (payload_directory / "transport.json").open("rb") as stream:
        metadata = stream.read(1024 * 1024 + 1)
    if len(metadata) > 1024 * 1024:
        raise ValueError("execution source transport metadata exceeds bound")
    data = json.loads(metadata)
    if not isinstance(data, dict) or set(data) != {
        "sha256",
        "size_bytes",
        "count",
        "identity",
        "review_inputs",
    }:
        raise ValueError("execution source transport metadata invalid")
    size, count = data["size_bytes"], data["count"]
    if (
        type(size) is not int
        or not 0 < size <= MAX_ARCHIVE_BYTES
        or type(count) is not int
        or count != (size + CHUNK_BYTES - 1) // CHUNK_BYTES
    ):
        raise ValueError("execution source transport lengths invalid")
    expected_names = {"transport.json", *(f"chunk-{i:04}" for i in range(count))}
    if {
        p.name for p in payload_directory.iterdir() if not p.name.startswith("..")
    } != expected_names:
        raise ValueError("execution source transport file set differs")
    identity = ExecutionSourceIdentity.model_validate(data["identity"])
    if not isinstance(data["review_inputs"], dict):
        raise ValueError("execution source review inputs invalid")
    with tempfile.TemporaryDirectory(prefix="coordinare-source-payload-") as temporary:
        bundle = Path(temporary) / "source.tgz"
        digest = hashlib.sha256()
        total = 0
        with bundle.open("xb") as output:
            for index in range(count):
                with (payload_directory / f"chunk-{index:04}").open("rb") as stream:
                    chunk = stream.read(CHUNK_BYTES + 1)
                expected_size = min(CHUNK_BYTES, size - total)
                if len(chunk) != expected_size:
                    raise ValueError("execution source chunk size differs")
                total += len(chunk)
                digest.update(chunk)
                output.write(chunk)
        if total != size or digest.hexdigest() != data["sha256"]:
            raise ValueError("execution source transport identity differs")
        restore_execution_bundle(
            bundle, destination, expected=identity, review_inputs=data["review_inputs"]
        )


if __name__ == "__main__":
    import sys
    from pathlib import Path

    restore_projected_source(Path(sys.argv[1]), Path(sys.argv[2]))
