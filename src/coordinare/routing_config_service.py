"""Locate, read, write, and validate the spec-078 performer routing-table YAML
from the coordinare dashboard (spec 081-config-ui).

The dashboard edits the **host-side** routing YAML that performer endpoints mount
into their containers. There is no in-flight injection: the next performer job
picks up the edited file at job start (research D2). This module resolves the
host path from a performer-endpoint config's ``volumes`` + the
``SELFHOSTED_ROUTING_CONFIG`` env var, reads/writes it via the shared atomic-write
primitive, and validates proposed edits against the spec-078 models reused from
``agent/performer/src/performer/proxy/routing.py`` for validation parity.

Design references (specs/081-config-ui/):
  * research D2 — write the host-side mounted YAML; next job reads it.
  * data-model E4 — RoutingTable / RoutingEntry / TargetDescriptor (reused).
  * contracts §Routing CRUD — all routing writes return ``applied: staged_next_job``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path  # noqa: TC003 — needed at runtime for pydantic type resolution
from typing import TYPE_CHECKING, Any, TypeGuard

import yaml
from pydantic import BaseModel, Field

from coordinare.lib.redaction import redact_secrets
from coordinare.services.config_write_service import FieldError, SaveResult
from coordinare.services.mounted_file import (
    MountedFileRefusedError,
    mounted_file_refusal,
    read_mounted_file,
    replace_mounted_file,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from coordinare.models.performer_endpoint import PerformerEndpointConfig

# Env var a performer reads at job start to locate its mounted routing table.
ROUTING_ENV_VAR = "SELFHOSTED_ROUTING_CONFIG"
# 208: the routing file is mounted from the host, possibly read-write into a performer.
# A table larger than this is refused rather than parsed.
ROUTING_MAX_BYTES = 1024 * 1024


class RoutingLocation(BaseModel):
    """Resolved host-side routing file + the endpoint that mounts it.

    ``mounts`` is every performer mount host (208). A routing file inside one of them is read
    and written with the link-refusing walk from the outermost mount root. A file outside
    all of them is an operator path.
    """

    endpoint_id: str
    host_path: Path
    container_path: str
    mounts: tuple[Path, ...] = ()

    model_config = {"arbitrary_types_allowed": True}


class RoutingView(BaseModel):
    """Read response for ``GET /api/config/routing`` (contracts §Routing CRUD)."""

    routing_available: bool = False
    entries: list[dict[str, Any]] = Field(default_factory=list)
    content_hash: str | None = None


def locate_routing_file(
    endpoints: list[PerformerEndpointConfig],
    mounts: Iterable[Path] = (),
) -> RoutingLocation | None:
    """Resolve the host-side routing YAML path from performer-endpoint configs.

    Scans each endpoint's ``env[SELFHOSTED_ROUTING_CONFIG]`` for the container
    path, then maps it back to a host path via the matching ``VolumeMount``.
    Returns ``None`` when no endpoint mounts a routing YAML (``routing_available``
    is false). (research D2)
    """
    for endpoint in endpoints:
        container_path = endpoint.env.get(ROUTING_ENV_VAR)
        if not container_path:
            continue
        for volume in endpoint.volumes:
            if str(volume.container_path) == container_path:
                return RoutingLocation(
                    endpoint_id=endpoint.id,
                    host_path=volume.host_path,
                    container_path=container_path,
                    mounts=tuple(mounts),
                )
    return None


def _is_writable_location(location: RoutingLocation | None) -> TypeGuard[RoutingLocation]:
    """A routing table the CRUD writers may replace: a regular file, not a link below a mount (208)."""
    return location is not None and mounted_file_refusal(
        location.host_path, mounts=location.mounts,
    ) is None


def routing_content_hash(location: RoutingLocation) -> str | None:
    """The ``sha256:<hex>`` hash of the routing table, or None when it is refused (208).

    The dashboard uses this in place of ``compute_content_hash``. That hash follows links
    and reads without a bound, so a performer could make it return the hash of a host
    file, or load a large one on every fetch. None means the file is missing, a link,
    not a regular file, or too large. The caller reports that as the read-only state.
    """
    return _readable_content_hash(location)


def _readable_content_hash(location: RoutingLocation) -> str | None:
    """Return ``host_path``'s content hash when it is a present, *readable* file;
    ``None`` when it is missing, a non-file, a link, or too large (208).

    The bytes come from :func:`read_mounted_file`, which refuses links and hard links and
    bounds the size, so a performer-planted file reads as absent. The hash is the same
    ``sha256:<hex>`` that ``compute_content_hash`` gives a regular file, so a read here
    and the write-side baseline check agree.

    Single source of truth shared by ``routing_available()`` and ``read_routing()``
    so the two cannot drift apart (Copilot round 27): if they disagreed,
    ``/api/config/all`` could present routing as editable while the routing endpoint
    reports it unavailable.
    """
    data = read_mounted_file(
        location.host_path, max_bytes=ROUTING_MAX_BYTES, mounts=location.mounts,
    )
    if data is None:
        return None
    return "sha256:" + hashlib.sha256(data).hexdigest()


def routing_available(
    endpoints: list[PerformerEndpointConfig], mounts: Iterable[Path] = (),
) -> bool:
    """True when a performer endpoint mounts a routing YAML that is present and
    readable on disk.

    Routing is only editable from the dashboard when the host-side file the
    performers mount is actually present *and readable* on this host (research
    D2). An endpoint that references a routing path which is unmounted / missing /
    unreadable on disk yields ``False`` — the UI then renders the read-only empty
    state (T019). Shares ``_readable_content_hash`` with ``read_routing()`` so the
    two always agree (Copilot round 27).
    """
    location = locate_routing_file(endpoints, mounts)
    return location is not None and _readable_content_hash(location) is not None


def _read_entries(location: RoutingLocation) -> list[dict[str, Any]]:
    """Return the raw entry dicts from the routing YAML (mapping- or list-rooted).

    Tolerant on read so a display fetch never 500s on a slightly malformed table;
    hard validation happens on write via the spec-078 models. A linked, oversize or
    unreadable file yields no entries (208).
    """
    data_bytes = read_mounted_file(
        location.host_path, max_bytes=ROUTING_MAX_BYTES, mounts=location.mounts,
    )
    if data_bytes is None:
        return []
    try:
        data = yaml.safe_load(data_bytes.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError):
        # Tolerant on read (see docstring): a malformed/invalid-UTF-8 table must not
        # 500 the display fetch, so it degrades to the empty state and the UI renders
        # its read-only banner. Hard validation happens on write.
        return []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("entries") or []
    else:
        entries = []
    return [e for e in entries if isinstance(e, dict)]


def _redact_for_display(value: Any) -> Any:
    """Redact every string in a routing entry tree (208). For display only: the write path
    reads the unredacted entries, so a saved table is never altered by a redaction."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_for_display(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_for_display(item) for key, item in value.items()}
    return value


def read_routing(location: RoutingLocation | None) -> RoutingView:
    """Read the routing table at ``location`` (Phase 5, T036).

    Returns the read-only empty state when no endpoint mounts a routing YAML or
    the host-side file is missing (contracts §Routing CRUD); otherwise the raw
    entries plus the optimistic-concurrency ``content_hash``.
    """
    if location is None:
        return RoutingView(routing_available=False)
    # Shared readability probe with ``routing_available()`` (Copilot round 27):
    # a missing / non-file / present-but-unreadable mounted file yields ``None`` and
    # degrades to the read-only empty state rather than being reported editable with
    # a spurious empty-file sentinel hash.
    content_hash = _readable_content_hash(location)
    if content_hash is None:
        return RoutingView(routing_available=False)
    return RoutingView(
        routing_available=True,
        entries=_redact_for_display(_read_entries(location)),
        content_hash=content_hash,
    )


def validate_routing_entry(entry: dict[str, Any]) -> None:
    """Validate one proposed routing entry against the spec-078 models.

    Constructs ``RoutingEntry``/``TargetDescriptor`` so the coordinare enforces the
    same rules the performer does (reroute ⇒ empty normalizers; normalize ⇒
    non-empty, all in ``NORMALIZER_REGISTRY``; ``wire_format`` ∈ {openai,
    anthropic}; non-empty base_url). Raises ``ValueError`` on failure. (Phase 5,
    T036)
    """
    try:
        from performer.proxy.routing import RoutingEntry
    except ModuleNotFoundError as exc:
        # The performer package is not a coordinare dependency and is absent from the
        # daemon image (Dockerfile.daemon copies only ``src`` + ``uv sync``). Convert
        # to ValueError so callers return a structured ``validation`` result instead
        # of 500ing the routing endpoint (Copilot round 28).
        raise ValueError(
            "Routing validation is unavailable: the performer routing models "
            "(performer.proxy.routing) are not installed in this environment.",
        ) from exc
    from pydantic import ValidationError

    try:
        RoutingEntry.model_validate(entry)
    except ValidationError as exc:
        # Surface the first secret-free pydantic message as a bare ValueError.
        errs = exc.errors()
        msg = errs[0]["msg"] if errs else "invalid routing entry"
        raise ValueError(msg) from exc


def _validate_table(entries: list[dict[str, Any]]) -> None:
    """Validate the whole proposed routing table via the spec-078 ``RoutingTable``."""
    try:
        from performer.proxy.routing import RoutingTable
    except ModuleNotFoundError as exc:
        # See ``validate_routing_entry``: a missing performer package becomes a
        # structured ``validation`` error rather than a 500 (Copilot round 28).
        raise ValueError(
            "Routing validation is unavailable: the performer routing models "
            "(performer.proxy.routing) are not installed in this environment.",
        ) from exc
    from pydantic import ValidationError

    try:
        RoutingTable.model_validate({"entries": entries})
    except ValidationError as exc:
        errs = exc.errors()
        msg = errs[0]["msg"] if errs else "invalid routing table"
        raise ValueError(msg) from exc


def _routing_conflict(location: RoutingLocation, base_hash: str) -> Any:
    """Guard the optimistic-concurrency baseline before a routing write.

    Returns a ``validation`` ``SaveResult`` if ``base_hash`` is missing/empty (a
    client error — without this an empty hash never matches the real content hash,
    surfacing a misleading "config changed on disk" conflict), a ``conflict``
    ``SaveResult`` if it is present but stale, else ``None``. The current hash comes
    from the same bounded, link-refusing reader as the reads (208), so the baseline a
    write compares against is the content the daemon would have read.
    """

    if not base_hash:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=None,
                    code="validation",
                    message="base_hash is required; reload the config and retry",
                ),
            ],
        )
    current = _readable_content_hash(location)
    if current is None:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=None,
                    code="forbidden",
                    message=(
                        "Routing table is currently unreadable on this host; "
                        "routing is read-only."
                    ),
                ),
            ],
        )
    if current != base_hash:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=None,
                    code="conflict",
                    message="config changed on disk since it was read; reload and retry",
                ),
            ],
        )
    return None


def _readonly_guard(location: RoutingLocation | None) -> Any:
    """Return a ``forbidden`` :class:`SaveResult` when routing is not writable.

    Routing is writable only when a performer endpoint mounts a routing YAML
    *and the host-side path is an actual file*: a plain, single-name regular file, the
    same check ``routing_available()`` / ``read_routing()`` apply (208). A ``None``
    location (no mount) or a non-file path (missing / directory / link / mount glitch)
    is read-only,
    so the CRUD writers must never create or overwrite a file there. Returns
    ``None`` when the location is writable.
    """

    if _is_writable_location(location):
        return None
    if location is None:
        message = "No performer endpoint mounts a routing table; routing is read-only."
    else:
        # An endpoint *does* mount a routing path, but the host-side file is missing
        # or not a regular file (unmounted / directory / mount glitch). Say so —
        # don't claim "no endpoint mounts" — so the operator can fix the mount.
        message = (
            f"Routing table for endpoint '{location.endpoint_id}' is not a file on "
            f"this host ({location.host_path}); routing is read-only."
        )
    return SaveResult(
        ok=False,
        errors=[FieldError(key=None, code="forbidden", message=message)],
    )


def _write_entries(
    location: RoutingLocation, entries: list[dict[str, Any]], base_hash: str,
) -> Any:
    """Atomically write the routing table and return a ``staged_next_job`` result.

    Re-checks optimistic concurrency against ``base_hash`` immediately before the
    atomic swap (FR-016, "reject immediately before swap") so an out-of-band edit
    landing between the caller's initial baseline check and this write is caught
    instead of clobbered (TOCTOU).
    """

    try:
        _validate_table(entries)
    except ValueError as exc:
        return SaveResult(
            ok=False,
            errors=[FieldError(key=None, code="validation", message=str(exc))],
        )

    # FR-016: re-check the on-disk hash here, right before the swap, to close the
    # window where the routing file changed since the caller's baseline check.
    conflict = _routing_conflict(location, base_hash)
    if conflict is not None:
        return conflict

    payload = yaml.safe_dump(
        {"entries": entries}, sort_keys=False, default_flow_style=False, allow_unicode=True,
    ).encode("utf-8")
    try:
        replace_mounted_file(location.host_path, payload, mounts=location.mounts)
    except (OSError, MountedFileRefusedError) as exc:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=None,
                    code="forbidden",
                    message=f"Could not write routing table: {exc}",
                ),
            ],
        )
    new_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
    return SaveResult(ok=True, applied="staged_next_job", new_hash=new_hash)


def _pair(entry: dict[str, Any]) -> tuple[Any, Any]:
    return (entry.get("backend"), entry.get("model"))


def _duplicate_refusal(others: list[dict[str, Any]], backend: Any, model: Any) -> Any:
    """143 third check: a (backend, model) pair appears once. Refuse a write that makes a second one."""
    if any(_pair(e) == (backend, model) for e in others):
        return SaveResult(ok=False, errors=[FieldError(
            key="model", code="validation",
            message=f"A routing entry for backend {backend!r} and model {model!r} already exists; "
                    "each backend and model pair can appear once.",
        )])
    return None


def create_routing_entry(
    location: RoutingLocation | None, entry: dict[str, Any], base_hash: str,
) -> Any:
    """Append a new routing entry and stage it for the next performer job (T036)."""
    if not _is_writable_location(location):
        return _readonly_guard(location)

    # Reject a missing/empty base_hash up front (a client error, not a race). The
    # *stale-hash* guard is deferred to just before the swap inside _write_entries.
    if not base_hash:
        return _routing_conflict(location, base_hash)

    entries = _read_entries(location)
    # A stale version is a conflict before anything about the content is judged.
    stale = _routing_conflict(location, base_hash)
    if stale is not None:
        return stale
    refusal = _duplicate_refusal(entries, entry.get("backend"), entry.get("model"))
    if refusal is not None:
        return refusal
    entries.append(entry)
    return _write_entries(location, entries, base_hash)


def _update_refusal(
    location: RoutingLocation, base_hash: str, entries: list[dict[str, Any]], index: int, merged: dict[str, Any],
) -> Any:
    """A stale version, or a change that would make a second (backend, model) pair; None otherwise."""
    stale = _routing_conflict(location, base_hash)
    if stale is not None:
        return stale
    if _pair(merged) == _pair(entries[index]):
        return None
    return _duplicate_refusal(
        [e for i, e in enumerate(entries) if i != index], merged.get("backend"), merged.get("model"),
    )


def update_routing_entry(
    location: RoutingLocation | None,
    index: int,
    changes: dict[str, Any],
    base_hash: str,
) -> Any:
    """Merge ``changes`` into the entry at ``index`` and re-stage the table (T036).

    ``changes`` may carry a nested ``target`` mapping; it is shallow-merged into
    the existing target so a partial target edit keeps untouched fields.
    """

    if not _is_writable_location(location):
        return _readonly_guard(location)

    # Reject a missing/empty base_hash up front (a client error, not a race). The
    # *stale-hash* guard is deferred to just before the swap inside _write_entries.
    if not base_hash:
        return _routing_conflict(location, base_hash)

    # Reject a non-mapping ``changes`` up front. A client sending a list/string
    # would otherwise hit ``dict(changes)`` below and raise TypeError/ValueError
    # (a 500) instead of the structured 422-style result the API layer provides.
    if not isinstance(changes, dict):
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=str(index),
                    code="validation",
                    message="Routing entry changes must be a mapping of fields.",
                ),
            ],
        )

    entries = _read_entries(location)
    if index < 0 or index >= len(entries):
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=str(index),
                    code="validation",
                    message=f"No routing entry at index {index}.",
                ),
            ],
        )

    merged = {**entries[index]}
    incoming = dict(changes)
    target_changes = incoming.pop("target", None)
    merged.update(incoming)
    if isinstance(target_changes, dict):
        # A corrupted on-disk entry may carry a non-mapping ``target`` (string/list).
        # ``merged.get("target") or {}`` would return that truthy non-dict and the
        # ``**`` unpack would raise TypeError (a 500) before _validate_table could
        # report a structured error. Coerce any non-mapping existing target to {}.
        existing_target = merged.get("target")
        if not isinstance(existing_target, dict):
            existing_target = {}
        merged["target"] = {**existing_target, **target_changes}
    refusal = _update_refusal(location, base_hash, entries, index, merged)
    if refusal is not None:
        return refusal
    entries[index] = merged
    return _write_entries(location, entries, base_hash)


def delete_routing_entry(
    location: RoutingLocation | None, index: int, base_hash: str,
) -> Any:
    """Delete the routing entry at ``index`` and re-stage the table (T036)."""

    if not _is_writable_location(location):
        return _readonly_guard(location)

    # Reject a missing/empty base_hash up front (a client error, not a race). The
    # *stale-hash* guard is deferred to just before the swap inside _write_entries.
    if not base_hash:
        return _routing_conflict(location, base_hash)

    entries = _read_entries(location)
    if index < 0 or index >= len(entries):
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=str(index),
                    code="validation",
                    message=f"No routing entry at index {index}.",
                ),
            ],
        )

    del entries[index]
    return _write_entries(location, entries, base_hash)


def routing_expected_problem(location: RoutingLocation | None, index: int, expected: Any) -> str | None:
    """What is wrong with the entry a write names, or None.

    143 third check: an absent ``expected`` is not checked; a present one that is not an object is
    ``malformed``; an object naming another entry than the one at ``index`` is ``moved``.
    """
    if expected is None:
        return None
    if not isinstance(expected, dict):
        return "malformed"
    return "moved" if routing_entry_moved(location, index, expected) else None


def routing_entry_moved(location: RoutingLocation | None, index: int, expected: Any) -> bool:
    """True when a write names an entry (its backend and model) that is not at ``index`` now.

    143 second review: the table can change between a page's load and its save, so an index
    may point at a different entry. A write that names no entry is not refused here.
    """
    if not isinstance(expected, dict):
        return False
    entries = read_routing(location).entries
    current = entries[index] if 0 <= index < len(entries) else None
    return not (
        current is not None
        and current.get("backend") == expected.get("backend")
        and current.get("model") == expected.get("model")
    )
