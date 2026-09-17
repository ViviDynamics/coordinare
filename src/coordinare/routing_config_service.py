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
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

# Env var a performer reads at job start to locate its mounted routing table.
ROUTING_ENV_VAR = "SELFHOSTED_ROUTING_CONFIG"


class RoutingLocation(BaseModel):
    """Resolved host-side routing file + the endpoint that mounts it."""

    endpoint_id: str
    host_path: Path
    container_path: str

    model_config = {"arbitrary_types_allowed": True}


class RoutingView(BaseModel):
    """Read response for ``GET /api/config/routing`` (contracts §Routing CRUD)."""

    routing_available: bool = False
    entries: list[dict[str, Any]] = Field(default_factory=list)
    content_hash: str | None = None


def locate_routing_file(
    endpoints: list[PerformerEndpointConfig],
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
                )
    return None


def _readable_content_hash(host_path: Path) -> str | None:
    """Return ``host_path``'s content hash when it is a present, *readable* file;
    ``None`` when it is missing, a non-file, or present-but-unreadable.

    ``compute_content_hash`` is total — it swallows the read OSError and returns
    the empty-file sentinel — so the sentinel alone cannot distinguish a genuinely
    empty file from an unreadable one. When the hash equals the sentinel, re-probe
    readability so an unreadable mounted file degrades to ``None`` (read-only empty
    state) instead of being reported editable with a spurious sentinel hash that a
    later write could use to clobber data the UI never read.

    Single source of truth shared by ``routing_available()`` and ``read_routing()``
    so the two cannot drift apart (Copilot round 27): if they disagreed,
    ``/api/config/all`` could present routing as editable while the routing endpoint
    reports it unavailable.
    """
    if not host_path.is_file():
        return None
    from coordinare.services.config_write_service import compute_content_hash

    content_hash = compute_content_hash(host_path)
    empty_sentinel = "sha256:" + hashlib.sha256(b"").hexdigest()
    if content_hash == empty_sentinel:
        try:
            host_path.read_bytes()
        except OSError:
            return None
    return content_hash


def routing_available(endpoints: list[PerformerEndpointConfig]) -> bool:
    """True when a performer endpoint mounts a routing YAML that is present and
    readable on disk.

    Routing is only editable from the dashboard when the host-side file the
    performers mount is actually present *and readable* on this host (research
    D2). An endpoint that references a routing path which is unmounted / missing /
    unreadable on disk yields ``False`` — the UI then renders the read-only empty
    state (T019). Shares ``_readable_content_hash`` with ``read_routing()`` so the
    two always agree (Copilot round 27).
    """
    location = locate_routing_file(endpoints)
    return location is not None and _readable_content_hash(location.host_path) is not None


def _read_entries(host_path: Path) -> list[dict[str, Any]]:
    """Return the raw entry dicts from the routing YAML (mapping- or list-rooted).

    Tolerant on read so a display fetch never 500s on a slightly malformed table;
    hard validation happens on write via the spec-078 models.
    """
    import yaml

    if not host_path.is_file():
        return []
    try:
        data = yaml.safe_load(host_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        # Tolerant on read (see docstring): a malformed/unreadable/invalid-UTF-8
        # table must not 500 the display fetch — degrade to the empty state so the
        # UI can render its read-only banner. Hard validation happens on write.
        return []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("entries") or []
    else:
        entries = []
    return [e for e in entries if isinstance(e, dict)]


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
    content_hash = _readable_content_hash(location.host_path)
    if content_hash is None:
        return RoutingView(routing_available=False)
    return RoutingView(
        routing_available=True,
        entries=_read_entries(location.host_path),
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


def _routing_conflict(host_path: Path, base_hash: str) -> Any:
    """Guard the optimistic-concurrency baseline before a routing write.

    Returns a ``validation`` ``SaveResult`` if ``base_hash`` is missing/empty (a
    client error — without this an empty hash never matches the real content hash,
    surfacing a misleading "config changed on disk" conflict), a ``conflict``
    ``SaveResult`` if it is present but stale, else ``None``. Mirrors
    ``config_write_service._conflict_result`` for cross-store parity.
    """
    from coordinare.services.config_write_service import (
        ConcurrencyConflictError,
        FieldError,
        SaveResult,
        guard_concurrency,
    )

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
    try:
        guard_concurrency(host_path, base_hash)
    except ConcurrencyConflictError as err:
        return SaveResult(
            ok=False,
            errors=[FieldError(key=None, code="conflict", message=str(err))],
        )
    except OSError:
        # host_path exists but is unreadable (permissions / mount glitch):
        # compute_content_hash inside guard_concurrency raised OSError. Mirror the
        # tolerant-read contract — degrade to read-only rather than 500 the write
        # endpoint — and surface it as ``forbidden`` (the routing store is not
        # currently writable), not a misleading ``conflict``.
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
    return None


def _readonly_guard(location: RoutingLocation | None) -> Any:
    """Return a ``forbidden`` :class:`SaveResult` when routing is not writable.

    Routing is writable only when a performer endpoint mounts a routing YAML
    *and the host-side path is an actual file* — the same ``is_file()`` semantics
    ``routing_available()`` / ``read_routing()`` use. A ``None`` location (no
    mount) or a non-file path (missing / directory / mount glitch) is read-only,
    so the CRUD writers must never create or overwrite a file there. Returns
    ``None`` when the location is writable.
    """
    from coordinare.services.config_write_service import FieldError, SaveResult

    if location is not None and location.host_path.is_file():
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
    host_path: Path, entries: list[dict[str, Any]], base_hash: str,
) -> Any:
    """Atomically write the routing table and return a ``staged_next_job`` result.

    Re-checks optimistic concurrency against ``base_hash`` immediately before the
    atomic swap (FR-016, "reject immediately before swap") so an out-of-band edit
    landing between the caller's initial baseline check and this write is caught
    instead of clobbered (TOCTOU).
    """
    from coordinare.services.config_write_service import (
        FieldError,
        SaveResult,
        atomic_write_yaml,
    )

    try:
        _validate_table(entries)
    except ValueError as exc:
        return SaveResult(
            ok=False,
            errors=[FieldError(key=None, code="validation", message=str(exc))],
        )

    # FR-016: re-check the on-disk hash here, right before the swap, to close the
    # window where the routing file changed since the caller's baseline check.
    conflict = _routing_conflict(host_path, base_hash)
    if conflict is not None:
        return conflict

    try:
        new_hash = atomic_write_yaml(host_path, {"entries": entries})
    except OSError as exc:
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
    return SaveResult(ok=True, applied="staged_next_job", new_hash=new_hash)


def create_routing_entry(
    location: RoutingLocation | None, entry: dict[str, Any], base_hash: str,
) -> Any:
    """Append a new routing entry and stage it for the next performer job (T036)."""
    readonly = _readonly_guard(location)
    if readonly is not None:
        return readonly

    # Reject a missing/empty base_hash up front (a client error, not a race). The
    # *stale-hash* guard is deferred to just before the swap inside _write_entries.
    if not base_hash:
        return _routing_conflict(location.host_path, base_hash)

    entries = _read_entries(location.host_path)
    entries.append(entry)
    return _write_entries(location.host_path, entries, base_hash)


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
    from coordinare.services.config_write_service import FieldError, SaveResult

    readonly = _readonly_guard(location)
    if readonly is not None:
        return readonly

    # Reject a missing/empty base_hash up front (a client error, not a race). The
    # *stale-hash* guard is deferred to just before the swap inside _write_entries.
    if not base_hash:
        return _routing_conflict(location.host_path, base_hash)

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

    entries = _read_entries(location.host_path)
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
    entries[index] = merged
    return _write_entries(location.host_path, entries, base_hash)


def delete_routing_entry(
    location: RoutingLocation | None, index: int, base_hash: str,
) -> Any:
    """Delete the routing entry at ``index`` and re-stage the table (T036)."""
    from coordinare.services.config_write_service import FieldError, SaveResult

    readonly = _readonly_guard(location)
    if readonly is not None:
        return readonly

    # Reject a missing/empty base_hash up front (a client error, not a race). The
    # *stale-hash* guard is deferred to just before the swap inside _write_entries.
    if not base_hash:
        return _routing_conflict(location.host_path, base_hash)

    entries = _read_entries(location.host_path)
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
    return _write_entries(location.host_path, entries, base_hash)
