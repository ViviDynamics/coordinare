"""Restore a validated complete source bundle without writing through links."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
        os.fchmod(root_fd, 0o755)
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
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd,
                    )
                    os.fchmod(next_fd, 0o755)
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
                    os.fchmod(file_fd, 0o755 if member.mode & 0o111 else 0o644)
                    with os.fdopen(file_fd, "wb") as output:
                        shutil.copyfileobj(stream, output, length=64 * 1024)
            finally:
                os.close(directory_fd)
        # Revalidate after writing as well: no consumer commands run on a changed payload.
        validate_execution_bundle(path, expected=expected, review_inputs=review_inputs)
    finally:
        os.close(root_fd)
    initialize_source_repository(destination, _bundle_entries(path), expected.source_commit)


# The restored tree as git records it: path to (mode, object id).
def _bundle_entries(path: Path) -> dict[str, tuple[str, str]]:
    for archive, member in bundle_members(path):
        if member.name == "metadata.json":
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("execution source metadata missing")
            data = json.loads(stream.read())
            return {entry["path"]: (entry["mode"], entry["object_id"]) for entry in data["entries"]}
    raise ValueError("execution source metadata missing")


SOURCE_BRANCH = "coordinare-source"
GIT_SECONDS = 300


def initialize_source_repository(destination: Path, entries: Mapping[str, tuple[str, str]], commit: str) -> bool:
    """Make the restored tree a git checkout: one commit holding exactly the pinned tree, detached, no remote.

    Dettmore e2e-83: the installer ran on a git clone, but the clean consumer got this plain tree, so every check that
    reads git state failed with "not a git repository". Implementer jobs run in git checkouts, so such checks are
    legitimate. The commit's id differs from the pinned commit (its history and metadata are not in the bundle), but
    its tree is the pinned tree: every staged entry is compared with the bundle's verified git object ids and modes.
    Returns False, leaving the plain tree, only where the image has no git.
    """
    git = shutil.which("git")
    if git is None:
        return False
    # No inherited GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE may point these commands elsewhere.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="coordinare",
                       GIT_AUTHOR_EMAIL="coordinare@localhost", GIT_COMMITTER_NAME="coordinare",
                       GIT_COMMITTER_EMAIL="coordinare@localhost", GIT_AUTHOR_DATE="2000-01-01T00:00:00Z",
                       GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")

    def run(*arguments: str) -> bytes:
        return subprocess.run(
            [git, "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgSign=false",
             "-c", "init.defaultBranch=" + SOURCE_BRANCH, "-C", str(destination), *arguments],
            env=environment, check=True, capture_output=True, timeout=GIT_SECONDS,
        ).stdout

    sha256 = any(len(object_id) == 64 for _, object_id in entries.values())
    run("init", "--quiet", *(["--object-format=sha256"] if sha256 else []))
    run("add", "--all", "--force")
    staged: dict[str, tuple[str, str]] = {}
    for record in run("ls-files", "--stage", "-z").split(b"\0"):
        if record:
            fields, name = record.decode().split("\t", 1)
            mode, object_id, _ = fields.split(" ")
            staged[name] = (mode, object_id)
    if staged != dict(entries):
        raise ValueError("execution source repository differs from the pinned tree")
    run("commit", "--quiet", "--no-verify", "--allow-empty", "-m", f"coordinare execution source {commit}")
    run("checkout", "--quiet", "--detach")
    run("branch", "--quiet", "-D", SOURCE_BRANCH)
    return True


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
            bundle, destination, expected=identity, review_inputs=data["review_inputs"],
        )


if __name__ == "__main__":
    import sys
    from pathlib import Path

    restore_projected_source(Path(sys.argv[1]), Path(sys.argv[2]))
