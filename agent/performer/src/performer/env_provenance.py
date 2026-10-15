"""Spec 234 (performer side): materialise the daemon-frozen env-cache steps for one consumer job.

A consumer container mounts the env cache read-only. The bootstrap that wrote it is not trusted, so the job
never runs the cache's own scripts. Before the job starts, :func:`materialise` writes the daemon's frozen steps
(``activate.sh`` and the service scripts, from the payload) into a container-local directory, and copies each
deb the daemon listed. A deb is read from the cache once, hashed, and written only when the hash matches. A
file the daemon did not list is never opened.

The profile (``devenv-profile.sh``) and the performer's own reads take their scripts from
:func:`frozen_scripts_dir`, which points at this directory whenever ``_DEVENV_PROVENANCE_DIR`` is set.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from coordinare_service_inference.cache_provenance import WalkRefusedError, stat_digest

if TYPE_CHECKING:
    from performer.server.models import BootstrapScripts, EnvProvenance

log = structlog.get_logger(__name__)

# Container-local, root-owned, never mounted into anything. The profile reads it through the env var below.
PROVENANCE_ROOT = Path("/var/lib/devenv-provenance")
PROVENANCE_ROOT_ENV = "_DEVENV_PROVENANCE_DIR"
DEVENV_BOOTSTRAP_ENV = "_DEVENV_BOOTSTRAP"

# A deb larger than this is refused rather than copied (the daemon's freeze bound for one file is 2 GiB, but a
# single deb is far smaller; this caps what a consumer copies).
MAX_DEB_BYTES = 512 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_ROOT_MODE = 0o700
_DIR_MODE = 0o755
_SCRIPT_MODE = 0o755
_FILE_MODE = 0o644
# 265 (review B1): the create mode of a temp file before its real mode is set on the open descriptor, and the
# number of random bytes in its name.
_PRIVATE_CREATE_MODE = 0o600
_TEMP_TOKEN_BYTES = 8


class ProvenanceMaterialiseError(Exception):
    """A frozen step could not be materialised. The job is refused with this reason."""


PROVENANCE_SLUG_MAX = 120
PROVENANCE_SLUG_HASH_CHARS = 8
_SLUG_VALID = re.compile(r"[a-z0-9_][a-z0-9_-]*")
_SLUG_INVALID = re.compile(r"[^a-z0-9_-]")


def provenance_slug(cache_dir_name: str) -> str:
    """The directory name for a cache on the performer side (234). Identical to the daemon's ``provenance_slug``.

    A name that fits the pattern and the bound is kept. Any other name is cleaned, truncated, and suffixed with a
    short hash of the original, so every input yields a valid, bounded slug. The parity test pins both sides.
    """
    if len(cache_dir_name) <= PROVENANCE_SLUG_MAX and _SLUG_VALID.fullmatch(cache_dir_name):
        return cache_dir_name
    cleaned = _SLUG_INVALID.sub("-", cache_dir_name.lower()).lstrip("-") or "symphony"
    head = cleaned[: PROVENANCE_SLUG_MAX - PROVENANCE_SLUG_HASH_CHARS - 1]
    digest = hashlib.sha256(cache_dir_name.encode()).hexdigest()[:PROVENANCE_SLUG_HASH_CHARS]
    return f"{head}-{digest}"


def frozen_scripts_dir(env_cache_path: str) -> Path:
    """Where this job reads its activate.sh and service scripts from (234).

    With ``_DEVENV_PROVENANCE_DIR`` set (a consumer with a frozen payload) it is the materialised copy. Without it
    (the env_bootstrap job, which writes the cache) it is the cache itself.
    """
    root = os.environ.get(PROVENANCE_ROOT_ENV)
    if root:
        return Path(root) / provenance_slug(Path(env_cache_path).name)
    return Path(env_cache_path)


def _read_listed_deb(cache_path: str, rel: str, digest: str) -> bytes:
    """Read one listed deb from the cache with no link followed, and accept it only when it hashes to *digest*."""
    src = Path(cache_path) / rel
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        message = f"{rel} could not be opened as a plain file: {exc.strerror}"
        raise ProvenanceMaterialiseError(message) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            message = f"{rel} is not a regular file"
            raise ProvenanceMaterialiseError(message)
        if info.st_size > MAX_DEB_BYTES:
            message = f"{rel} is larger than {MAX_DEB_BYTES} bytes"
            raise ProvenanceMaterialiseError(message)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DEB_BYTES:
                message = f"{rel} grew past {MAX_DEB_BYTES} bytes while it was read"
                raise ProvenanceMaterialiseError(message)
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != digest:
        message = f"{rel} changed after the daemon froze the cache"
        raise ProvenanceMaterialiseError(message)
    return data


def materialise(provenance: EnvProvenance, *, root: Path) -> Path:
    """Write the frozen steps for *provenance* under ``root/<slug>`` and return that directory.

    Every listed deb is read and verified before anything is written, so a single changed deb refuses the whole
    job and leaves no partial copy. A previous copy for the same slug is removed first.
    """
    expected = provenance_slug(Path(provenance.cache_path).name)
    if provenance.slug != expected:
        message = f"slug {provenance.slug!r} does not match the cache path (expected {expected!r})"
        raise ProvenanceMaterialiseError(message)
    if provenance.version == 2 and provenance.stat_digest is None:
        message = "v2 provenance requires a stat digest"
        raise ProvenanceMaterialiseError(message)
    if provenance.stat_digest is not None:
        try:
            actual = stat_digest(Path(provenance.cache_path), max_entries=provenance.max_entries,
                                 max_bytes=provenance.max_bytes)
        except (OSError, WalkRefusedError) as exc:
            raise ProvenanceMaterialiseError(f"stat digest check refused: {exc}") from exc
        if actual != provenance.stat_digest:
            raise ProvenanceMaterialiseError("stat digest differs from the verifier baseline")
    verified = {rel: _read_listed_deb(provenance.cache_path, rel, digest) for rel, digest in provenance.debs.items()}
    dest = root / provenance.slug
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    root.mkdir(mode=_ROOT_MODE, parents=True, exist_ok=True)
    (dest / "services").mkdir(parents=True, mode=_DIR_MODE)
    (dest / "debs").mkdir(parents=True, mode=_DIR_MODE)
    _write(dest / "activate.sh", provenance.activate_sh, _FILE_MODE)
    if provenance.services_start_sh is not None:
        _write(dest / "services" / "services-start.sh", provenance.services_start_sh, _SCRIPT_MODE)
    if provenance.services_health_sh is not None:
        _write(dest / "services" / "services-health.sh", provenance.services_health_sh, _SCRIPT_MODE)
    for rel, data in verified.items():
        target = dest / rel
        target.write_bytes(data)
        target.chmod(_FILE_MODE)
    log.info(
        "env_cache.provenance_materialised",
        slug=provenance.slug,
        debs=len(verified),
        digest=provenance.digest,
    )
    return dest


def write_bootstrap_scripts(cache_path: str, scripts: BootstrapScripts) -> None:
    """265: write the daemon-rendered activate.sh and verify.sh into the bootstrap's cache at job start.

    The bootstrap agent may have left its own copies there. These overwrite them, so the agent sees the
    authoritative text and the activation this job sources is the daemon's. Each file is written to a temporary
    name in the same directory and renamed over the old one, so a reader never sees a partial script.
    """
    cache = Path(cache_path)
    if not cache.is_dir():
        message = "the env cache directory is not present, so the bootstrap scripts cannot be written"
        raise ProvenanceMaterialiseError(message)
    for name, text, mode in (
        ("activate.sh", scripts.activate_sh, _FILE_MODE),
        ("verify.sh", scripts.verify_sh, _SCRIPT_MODE),
    ):
        write_replacing(cache / name, text.encode("utf-8"), mode)
    log.info("env_cache.bootstrap_scripts_written", cache_path=cache_path)


def _temp_name(name: str) -> str:
    """A random temp name beside the target, so a link planted at a guessable name is never the one used."""
    return f".{name}.{secrets.token_hex(_TEMP_TOKEN_BYTES)}.tmp"


def write_replacing(path: Path, data: bytes, mode: int) -> None:
    """265 (review B1): write *data* to *path* with a safe create, then rename it over *path*.

    The temp file is created with O_EXCL and O_NOFOLLOW, so a link planted at its name makes the create fail and nothing
    is written through it. The mode is set on the open descriptor. A link at *path* is replaced by the rename, never
    written through. The file is fsynced before the rename.
    """
    temp = path.parent / _temp_name(path.name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(temp, flags, _PRIVATE_CREATE_MODE)
    except OSError as exc:
        message = f"could not create {path.name} safely: {exc.strerror or exc}"
        raise ProvenanceMaterialiseError(message) from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def _write(path: Path, text: str, mode: int) -> None:
    write_replacing(path, text.encode("utf-8"), mode)


__all__ = [
    "DEVENV_BOOTSTRAP_ENV",
    "MAX_DEB_BYTES",
    "PROVENANCE_ROOT",
    "PROVENANCE_ROOT_ENV",
    "ProvenanceMaterialiseError",
    "frozen_scripts_dir",
    "materialise",
    "provenance_slug",
    "write_bootstrap_scripts",
    "write_replacing",
]
