"""Acquire complete pinned source without a daemon checkout or project execution."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import shutil
import tarfile
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from urllib.parse import quote

import httpx
import structlog
from coordinare_service_inference.environment.execution_source import (
    CHUNK_BYTES,
    MAX_ARCHIVE_BYTES,
    MAX_BLOB_BYTES,
    MAX_ENTRIES,
    MAX_EXPANDED_BYTES,
    MAX_METADATA_BYTES,
    ExecutionSourceBundle,
    ExecutionSourceError,
    ExecutionSourceIdentity,
    SourceEntry,
    bundle_members,
    canonical,
    checked_path,
    immutable_object,
    source_identity,
    validate_execution_bundle,
)

from coordinare.services.environment_acceptance import accepted_environment

if TYPE_CHECKING:
    from collections.abc import Buffer, Mapping
    from pathlib import Path

    from coordinare.models.env_cache import EnvCacheState

logger = structlog.get_logger(__name__)
MAX_FALLBACK_BLOBS = 256
MAX_REDIRECTS = 3
MAX_LINK_BYTES = 4096
GIT_SHA1_HEX_LENGTH = 40


_SOURCE_HTTP_ACTIVE: ContextVar[bool] = ContextVar("execution_source_http_active", default=False)


class _SourceRequestRedaction(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _SOURCE_HTTP_ACTIVE.get() and isinstance(record.args, tuple):
            record.args = tuple(
                str(value.copy_with(query=None, fragment=None))
                if isinstance(value, httpx.URL)
                else value
                for value in record.args
            )
        return True


_SOURCE_REQUEST_REDACTION = _SourceRequestRedaction()


@dataclass(frozen=True)
class GitHubSourceRequest:
    api_base: str
    token: str = field(repr=False)
    owner: str
    repo: str
    commit: str


@dataclass(frozen=True)
class RepositorySourceRequest:
    name: str
    commit: str
    review_inputs: Mapping[str, str]


@dataclass(frozen=True)
class _DownloadContext:
    client: httpx.AsyncClient
    token: str = field(repr=False)
    api_origin: str
    deadline: float


class SourceProvider(Protocol):
    async def prepare_execution_source(
        self,
        owner: str,
        repo: str,
        *,
        commit: str,
        destination: Path,
        deadline: float,
    ) -> ExecutionSourceBundle: ...


def source_refusal(message: str, *, code: str = 'execution_source_invalid') -> NoReturn:
    """Typed daemon failure boundary; shared package annotations may be external."""
    raise ExecutionSourceError(message, code=code) from None


def check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        message = "execution source acquisition deadline exceeded"
        raise TimeoutError(message)


async def _download(
    context: _DownloadContext, url: str, destination: Path, *, limit: int, accept: str,
) -> None:
    client, token, api_origin, deadline = (
        context.client,
        context.token,
        context.api_origin,
        context.deadline,
    )
    trusted = httpx.URL(api_origin)
    for redirect in range(MAX_REDIRECTS + 1):
        check_deadline(deadline)
        target = httpx.URL(url)
        if target.scheme != "https" or target.username or target.password:
            source_refusal("execution source download URL refused")
        same_origin = (target.scheme, target.host, target.port) == (
            trusted.scheme,
            trusted.host,
            trusted.port,
        )
        headers = {"Accept": accept}
        if same_origin:
            headers["Authorization"] = "Bearer " + token
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect == MAX_REDIRECTS or "location" not in response.headers:
                    source_refusal("execution source redirect bound exceeded")
                url = str(response.url.join(response.headers["location"]))
                continue
            response.raise_for_status()
            total = 0
            with destination.open("xb") as output:
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    check_deadline(deadline)
                    total += len(chunk)
                    if total > limit:
                        source_refusal("execution source download byte bound exceeded")
                    output.write(chunk)
            return
    source_refusal("execution source redirect invalid")


class _BoundedOutput(io.BufferedIOBase):
    def __init__(self, output: Any) -> None:
        self.output = output
        self.count = 0

    def write(self, data: Buffer) -> int:
        self.count += memoryview(data).nbytes
        if self.count > MAX_ARCHIVE_BYTES:
            source_refusal("execution source bundle exceeds compressed bound")
        return int(self.output.write(data))


def write_execution_bundle(
    commit: str,
    entries: list[SourceEntry],
    objects: Path,
    destination: Path,
    *,
    deadline: float,
) -> ExecutionSourceBundle:
    """Normalize checked blob objects into a provider-independent binary bundle."""
    identity = source_identity(commit, entries)
    metadata = canonical(
        {
            "identity": identity.model_dump(),
            "entries": [e.model_dump() for e in sorted(entries, key=lambda e: e.path)],
        },
    )
    if (
        len(metadata) > MAX_METADATA_BYTES
        or len(metadata) + identity.expanded_bytes > MAX_EXPANDED_BYTES
    ):
        source_refusal("execution source metadata exceeds byte bound")
    with (
        destination.open("xb") as output,
        tarfile.open(
            fileobj=_BoundedOutput(output),
            mode="w|gz",
            format=tarfile.PAX_FORMAT,
        ) as archive,
    ):
        info = tarfile.TarInfo("metadata.json")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
        for entry in sorted(entries, key=lambda e: e.path):
            check_deadline(deadline)
            info = tarfile.TarInfo("files/" + entry.path)
            info.mode = 0o755 if entry.mode == "100755" else 0o644
            if entry.mode == "120000":
                if entry.size > MAX_LINK_BYTES:
                    source_refusal("execution source symlink exceeds path bound")
                info.type = tarfile.SYMTYPE
                info.linkname = (objects / entry.object_id).read_bytes().decode("utf-8")
                archive.addfile(info)
            else:
                info.size = entry.size
                with (objects / entry.object_id).open("rb") as stream:
                    archive.addfile(info, stream)
    validate_execution_bundle(destination, expected=identity)
    digest = hashlib.sha256()
    with destination.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            check_deadline(deadline)
            digest.update(chunk)
    return ExecutionSourceBundle(
        identity,
        destination,
        digest.hexdigest(),
        destination.stat().st_size,
    )


def _write_archive_blob(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    entry: SourceEntry,
    objects: Path,
    deadline: float,
) -> None:
    digest = hashlib.new("sha1" if len(entry.object_id) == GIT_SHA1_HEX_LENGTH else "sha256")
    digest.update(b"blob " + str(entry.size).encode() + b"\0")
    target = objects / entry.object_id
    # Duplicate blobs may occur at multiple paths; verify each archive entry.
    with target.open("wb") as output:
        if entry.mode == "120000":
            if (
                not member.issym()
                or member.size != 0
                or len(member.linkname.encode()) != entry.size
            ):
                source_refusal("execution source symlink identity differs")
            raw = member.linkname.encode()
            digest.update(raw)
            output.write(raw)
        else:
            if (
                not member.isfile()
                or member.size != entry.size
                or bool(member.mode & 0o111) != (entry.mode == "100755")
            ):
                source_refusal("execution source archive file mode/size differs")
            stream = archive.extractfile(member)
            if stream is None:
                source_refusal("execution source archive file missing")
            while chunk := stream.read(CHUNK_BYTES):
                check_deadline(deadline)
                digest.update(chunk)
                output.write(chunk)
    if digest.hexdigest() != entry.object_id:
        source_refusal("execution source archive content differs from pinned tree")


def _normalize_archive(
    archive_path: Path,
    entries: list[SourceEntry],
    objects: Path,
    deadline: float,
) -> set[str]:
    declared = {e.path: e for e in entries}
    seen: set[str] = set()
    root = None
    count = 0
    for archive, member in bundle_members(archive_path):
        check_deadline(deadline)
        count += 1
        if count > MAX_ENTRIES * 2 + 1:
            source_refusal("execution source archive entry bound exceeded")
        components = member.name.rstrip("/").split("/", 1)
        checked_path(components[0])
        if root is None:
            root = components[0]
        if root != components[0]:
            source_refusal("execution source archive has multiple roots")
        if len(components) == 1:
            if not member.isdir():
                source_refusal("execution source archive root is not directory")
            continue
        name = checked_path(components[1])
        if member.isdir():
            if not any(p.startswith(name + "/") for p in declared):
                source_refusal("execution source archive has unexpected directory")
            continue
        entry = declared.get(name)
        if entry is None or name in seen:
            source_refusal("execution source archive has unexpected or duplicate file")
        seen.add(name)
        _write_archive_blob(archive, member, entry, objects, deadline)
    return seen


def _create_destination(destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)


def _tree_entries(tree_path: Path, commit: str) -> list[SourceEntry]:
    with tree_path.open("rb") as stream:
        tree = json.load(stream)
    if (
        not isinstance(tree, dict)
        or tree.get("truncated") is not False
        or not isinstance(tree.get("tree"), list)
        or len(tree["tree"]) > MAX_ENTRIES * 2
    ):
        source_refusal("execution source tree is incomplete or oversized")
    entries = []
    all_paths: set[str] = set()
    for item in tree["tree"]:
        if not isinstance(item, dict):
            source_refusal("execution source tree entry invalid")
        path = checked_path(item.get("path", ""))
        if path in all_paths:
            source_refusal("execution source tree has duplicate entries")
        all_paths.add(path)
        if item.get("type") == "tree" and item.get("mode") == "040000":
            continue
        if item.get("type") != "blob":
            source_refusal(
                "execution source submodule or tree entry unsupported",
                code="execution_source_unsupported",
            )
        entries.append(
            SourceEntry(
                path=path,
                mode=item.get("mode"),
                object_id=item.get("sha"),
                size=item.get("size"),
            ),
        )
    source_identity(commit, entries)
    return entries


async def _acquire_github_source(
    request: GitHubSourceRequest, *, destination: Path, deadline: float,
) -> ExecutionSourceBundle:
    api_base, token, owner, repo, commit = (
        request.api_base,
        request.token,
        request.owner,
        request.repo,
        request.commit,
    )
    immutable_object(commit)
    check_deadline(deadline)
    if not token.strip() or not owner or not repo:
        source_refusal("execution source requires repository authentication and identity")
    _create_destination(destination)
    started = time.monotonic()
    try:
        async with asyncio.timeout_at(deadline):
            base = f"{api_base.rstrip('/')}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                context = _DownloadContext(client, token, api_base, deadline)
                tree_path = destination / "tree.json"
                await _download(
                    context,
                    f"{base}/git/trees/{commit}?recursive=1",
                    tree_path,
                    limit=MAX_METADATA_BYTES,
                    accept="application/vnd.github+json",
                )
                entries = _tree_entries(tree_path, commit)
                archive_path = destination / "provider.tgz"
                await _download(
                    context,
                    f"{base}/tarball/{commit}",
                    archive_path,
                    limit=MAX_ARCHIVE_BYTES,
                    accept="application/vnd.github+json",
                )
                objects = destination / "objects"
                objects.mkdir()
                seen = _normalize_archive(archive_path, entries, objects, deadline)
                missing = [e for e in entries if e.path not in seen]
                if len(missing) > MAX_FALLBACK_BLOBS:
                    source_refusal("execution source missing blob completion bound exceeded")
                semaphore = asyncio.Semaphore(8)
                # Git tree paths can share an object. Download missing objects once.
                unique = {e.object_id: e for e in missing if not (objects / e.object_id).exists()}

                async def complete(entry: SourceEntry) -> None:
                    async with semaphore:
                        await _download(
                            context,
                            f"{base}/git/blobs/{entry.object_id}",
                            objects / entry.object_id,
                            limit=min(entry.size, MAX_BLOB_BYTES),
                            accept="application/vnd.github.raw+json",
                        )

                async with asyncio.TaskGroup() as group:
                    for entry in unique.values():
                        group.create_task(complete(entry))
            bundle = write_execution_bundle(
                commit,
                entries,
                objects,
                destination / "source.tgz",
                deadline=deadline,
            )
            archive_path.unlink()
            tree_path.unlink()
            shutil.rmtree(objects)
            logger.info(
                "environment_source.acquired",
                source_commit=commit,
                source_digest=bundle.identity.digest(),
                file_count=bundle.identity.file_count,
                expanded_bytes=bundle.identity.expanded_bytes,
                transport_bytes=bundle.payload_bytes,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            return bundle
    except BaseException:
        shutil.rmtree(destination)
        raise


async def prepare_repository_source(
    state: Mapping[str, Any],
    request: RepositorySourceRequest,
    *,
    destination: Path,
    deadline: float,
) -> ExecutionSourceBundle:
    """Resolve an actual provider for a parent-owned commit and review hash set."""
    name, commit, review_inputs = request.name, request.commit, request.review_inputs
    config = state.get("coordinare_config")
    global_config = getattr(config, "global_config", None) or state.get("config")
    symphony = (state.get("symphony_configs") or {}).get(name)
    if symphony is None:
        symphony = next((s for s in getattr(config, "symphonies", []) if s.name == name), None)
    effective = symphony.effective_config(global_config) if symphony is not None else global_config
    github = (state.get("symphony_github_services") or {}).get(name) or state.get("github_service")
    provider = getattr(github, "prepare_execution_source", None)
    if effective is None or provider is None:
        source_refusal(
            "execution source provider unavailable; review files cannot substitute for a checkout",
        )
    bundle = await provider(
        effective.github_org,
        effective.project_name or name,
        commit=commit,
        destination=destination,
        deadline=deadline,
    )
    validate_execution_bundle(
        bundle.payload_path,
        expected=bundle.identity,
        review_inputs=review_inputs,
    )
    if bundle.identity.source_commit != commit:
        source_refusal("execution source commit differs from accepted manifest")
    return bundle


async def ensure_execution_source(
    state: Mapping[str, Any],
    name: str,
    cache: EnvCacheState,
    *,
    destination: Path,
    deadline: float,
) -> ExecutionSourceBundle:

    manifest = accepted_environment(cache)
    if manifest is None:
        source_refusal("execution source requires independently accepted manifest")
    bundle = await prepare_repository_source(
        state,
        RepositorySourceRequest(name, manifest.source_commit, manifest.inputs),
        destination=destination,
        deadline=deadline,
    )
    previous = cache.environment_execution_source
    if previous is not None and ExecutionSourceIdentity.model_validate(previous) != bundle.identity:
        source_refusal("execution source differs from accepted bootstrap source")
    cache.environment_execution_source = bundle.identity.model_dump(mode="json")
    return bundle


async def checkpoint_source_ownership(state: Mapping[str, Any]) -> None:
    """A failed persistence callback prevents source acquisition/execution."""
    service = state.get("env_cache_service")
    backing = getattr(service, "_kubernetes_state", {})
    save = (
        state.get("snapshot_checkpoint_fn")
        or state.get("snapshot_save_fn")
        or backing.get("snapshot_checkpoint_fn")
        or backing.get("snapshot_save_fn")
    )
    if callable(save):
        await save()


async def acquire_github_source(
    request: GitHubSourceRequest, *, destination: Path, deadline: float,
) -> ExecutionSourceBundle:
    logger_http = logging.getLogger("httpx")
    if _SOURCE_REQUEST_REDACTION not in logger_http.filters:
        logger_http.addFilter(_SOURCE_REQUEST_REDACTION)
    context_token = _SOURCE_HTTP_ACTIVE.set(True)
    try:
        return await _acquire_github_source(
            request,
            destination=destination,
            deadline=deadline,
        )
    except httpx.HTTPError:
        source_refusal("execution source provider request unavailable", code="execution_source_unavailable")
    finally:
        _SOURCE_HTTP_ACTIVE.reset(context_token)
