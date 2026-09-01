"""Atomic, optimistic-concurrency-guarded config write-back (spec 081-config-ui).

Shared write primitive for every dashboard save endpoint — both ``config.yaml``
(global tuning + personas + symphonies + spec-080 catalogs) and the spec-078
performer routing-table YAML.

Design references (specs/081-config-ui/):
  * research D4 — SHA-256 content-hash optimistic concurrency: every read returns
    a baseline hash, every save submits it; the write re-reads + recomputes +
    rejects (409 ``conflict``) on mismatch immediately before the swap.
  * research D5 — atomic write-back: ``tempfile.mkstemp(dir=parent)`` →
    ``yaml.safe_dump`` → ``os.chmod`` (preserve mode) → ``os.replace``. Mirrors
    the proven pattern in :mod:`coordinare.services.persona_service`. ``safe_dump``
    strips file-level comments (documented trade-off).
  * research D6 — referential-integrity + delete-protection for spec-080 catalogs,
    reusing ``ProjectConfiguration._validate_orchestration_catalogs()`` invariants.
  * research D7 — hot-reload vs restart classification from the annotation table.
  * data-model E6 — SaveRequest / SaveResult / FieldError shapes.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from coordinare.config import CoordinareConfiguration

Store = Literal["config_yaml", "routing_yaml"]
Applied = Literal["hot_reloaded", "staged_restart", "staged_next_job"]
ErrorCode = Literal["validation", "conflict", "referenced", "forbidden"]

# The spec-080 catalogs editable via the catalog CRUD endpoints (research D6).
CATALOG_KEYS: tuple[str, ...] = ("endpoints", "model_endpoints", "modes")

# The scalar-group sections ``save_section`` may persist. Only ``global`` is a
# scalar group in config.yaml; catalogs/personas/symphonies have their own
# endpoints. Guarded defensively so a malformed request can never write an
# arbitrary top-level key into config.yaml.
SCALAR_SECTIONS: tuple[str, ...] = ("global",)


class FieldError(BaseModel):
    """A single actionable, secret-free, stack-trace-free error (data-model E6)."""

    key: str | None = None
    code: ErrorCode
    message: str


class SaveRequest(BaseModel):
    """An edit submitted by the dashboard (data-model E6)."""

    store: Store = "config_yaml"
    section: str
    changes: dict[str, Any] = Field(default_factory=dict)
    base_hash: str


class SaveResult(BaseModel):
    """The outcome of a save (data-model E6).

    ``message`` carries an operator-readable advisory that is not itself an
    error — e.g. the FR-015 "saved to disk but live reload failed; restart to
    apply" note when a hot-reload is downgraded to ``staged_restart``.
    """

    ok: bool
    applied: Applied | None = None
    new_hash: str | None = None
    errors: list[FieldError] = Field(default_factory=list)
    message: str | None = None


class ConcurrencyConflictError(Exception):
    """Raised when the on-disk file changed since the submitted ``base_hash``."""


def compute_content_hash(path: Path) -> str:
    """Return ``sha256:<hex>`` of the on-disk file bytes (research D4).

    Used as the optimistic-concurrency baseline. Returns the empty/missing
    sentinel (``sha256`` of ``b""``) for any path that is not a readable regular
    file: absent, a non-file (directory from a mount glitch), or a regular file
    whose bytes cannot be read (:class:`OSError` from permission/mount issues).
    Hashing is therefore total and never raises — callers treat the sentinel as
    "no readable baseline".
    """
    if not path.is_file():
        return "sha256:" + hashlib.sha256(b"").hexdigest()
    try:
        raw = path.read_bytes()
    except OSError:
        return "sha256:" + hashlib.sha256(b"").hexdigest()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def safe_failure_reason(exc: BaseException) -> str:
    """A reason for a config read/write failure with no filesystem path in it.

    158 (#241): these messages reach the browser, and the obvious
    ``f"...: {exc}"`` puts the absolute path on the wire --
    ``[Errno 13] Permission denied: '/etc/coordinare/config.yaml'`` for an
    :class:`OSError`, and ``in "/etc/coordinare/config.yaml", line 3, column 1`` for a
    :class:`yaml.YAMLError`. Both name where the deployment keeps its config, to
    anyone who can reach the endpoint.

    ``strerror`` is the useful half of an OSError ("Permission denied") and carries no
    path. Nothing else here has a comparably safe field, so those get a description of
    the failure rather than the exception's own words. The full exception still goes to
    the log, which is where an operator debugging this should be looking.
    """
    strerror = getattr(exc, "strerror", None)
    if isinstance(strerror, str) and strerror:
        return strerror
    if isinstance(exc, UnicodeDecodeError):
        return "the file is not valid UTF-8"
    if isinstance(exc, yaml.YAMLError):
        return "the file is not valid YAML"
    return "the file could not be read"


def guard_concurrency(path: Path, base_hash: str) -> None:
    """Re-read ``path``, recompute its hash, raise :class:`ConcurrencyConflictError`
    if it differs from ``base_hash`` (research D4). Call immediately before swap.

    ``compute_content_hash()`` is total and returns the empty-file sentinel for an
    unreadable-but-present regular file (it swallows :class:`OSError`). A bare hash
    comparison would then let a sentinel ``base_hash`` spuriously match such a file,
    permitting a write to clobber data the UI never read. Guard against that: when
    the computed hash equals the empty sentinel but the path is a present regular
    file, re-probe its readability and let any :class:`OSError` propagate so callers
    degrade to a structured ``forbidden`` result rather than proceeding.
    """
    current = compute_content_hash(path)
    empty_sentinel = "sha256:" + hashlib.sha256(b"").hexdigest()
    if current == empty_sentinel and path.is_file():
        # Re-raises OSError for an unreadable regular file; a genuinely empty file
        # reads as b"" and passes through to the normal hash comparison below.
        path.read_bytes()
    if current != base_hash:
        raise ConcurrencyConflictError(
            "config changed on disk since it was read; reload and retry"
        )


def atomic_write_yaml(path: Path, data: dict[str, Any]) -> str:
    """Atomically write ``data`` to ``path`` as YAML, returning the new hash.

    ``tempfile.mkstemp(dir=path.parent)`` → ``yaml.safe_dump`` → ``flush`` +
    ``fsync`` → ``os.chmod`` (preserve original mode) → ``os.replace`` (research
    D5). ``safe_dump`` strips file-level comments (documented trade-off).
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Preserve the original file's permission bits when overwriting. Mask with
    # S_IMODE so only the rwx/setuid/sticky bits are carried over — st_mode also
    # carries file-type bits (S_IFREG, …) that are non-portable to pass to chmod.
    original_mode: int | None = None
    if path.exists():
        original_mode = stat.S_IMODE(os.stat(path).st_mode)

    fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.", suffix=".tmp")
    # Track whether the raw fd is still ours to close. os.fdopen() takes ownership
    # of fd on success (closing the wrapper closes fd), but if fdopen() itself
    # raises (e.g. resource exhaustion) ownership never transfers and the raw fd
    # would leak. Keep fd_open True until fdopen has demonstrably succeeded.
    fd_open = True
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd_open = False
            yaml.safe_dump(
                data,
                fh,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
            )
            fh.flush()
            os.fsync(fh.fileno())
        if original_mode is not None:
            os.chmod(tmp_name, original_mode)
        os.replace(tmp_name, path)
    except BaseException:
        if fd_open:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

    return compute_content_hash(path)


def _safe_atomic_write(
    path: Path, data: dict[str, Any], *, key: str
) -> tuple[str | None, SaveResult | None]:
    """Atomically write ``data``, converting :class:`OSError` to a ``forbidden`` result.

    Returns ``(new_hash, None)`` on success or ``(None, SaveResult)`` when the
    write fails for an operational reason (read-only filesystem, permission
    change, disk-full). An unstructured 500 would otherwise bubble to FastAPI;
    instead callers surface an operator-actionable ``forbidden`` SaveResult,
    matching the read-only semantics used elsewhere in the write paths.
    """
    try:
        return atomic_write_yaml(path, data), None
    except OSError as exc:
        return None, SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=key,
                    code="forbidden",
                    message=(
                        f"Could not write the configuration file: "
                        f"{safe_failure_reason(exc)}"
                    ),
                )
            ],
        )


# --- T026: hot-reload vs restart classification (research D7) ------------------


def classify_section_applied(section: str, changed_keys: Iterable[str]) -> Applied:
    """Classify a section save as ``hot_reloaded`` or ``staged_restart`` (D7).

    A save is ``staged_restart`` if *any* changed field is annotated
    ``restart_required`` (process bindings like ports/hosts read once at startup);
    otherwise it is ``hot_reloaded``. Sourced from the annotation table so the
    classification stays in lockstep with the descriptor layer.
    """
    from coordinare.config_descriptors import annotation_for

    for key in changed_keys:
        if annotation_for(f"{section}.{key}").restart_required:
            return "staged_restart"
    return "hot_reloaded"


# --- validation helpers (shared by section + catalog saves) --------------------


def _field_error_from_pydantic(err: dict[str, Any]) -> FieldError:
    """Map one pydantic error dict to a secret-free :class:`FieldError`.

    pydantic v2 ``msg`` strings never echo the input value, so they are safe to
    surface to the operator. The dotted field name is taken from the last string
    element of ``loc`` (e.g. ``('global_config', 'poll_interval_seconds')`` →
    ``poll_interval_seconds``).
    """
    loc = err.get("loc", ())
    key = loc[-1] if loc and isinstance(loc[-1], str) else None
    return FieldError(key=key, code="validation", message=err.get("msg", "invalid value"))


def _validate_candidate(candidate: dict[str, Any]) -> list[FieldError]:
    """Validate a full candidate config dict, returning ``[]`` on success.

    Builds a throwaway :class:`CoordinareConfiguration` — this enforces both
    field-level constraints AND cross-catalog referential integrity (the
    ``_validate_orchestration_catalogs`` validator) in a single pass. A ``${VAR}``
    / unset ``github_token`` is substituted with a placeholder for the validation
    pass only (never written back); the substitute is never surfaced.
    """
    from pydantic import ValidationError

    from coordinare.config import CoordinareConfiguration
    from coordinare.config_descriptors import is_env_placeholder
    from coordinare.config_validation import coerce_multi_symphony_raw

    attempt = dict(candidate)
    token = attempt.get("github_token")
    if is_env_placeholder(token) or token is None:
        attempt["github_token"] = "ghp_placeholder_for_validation"

    try:
        CoordinareConfiguration(**coerce_multi_symphony_raw(attempt))
    except ValidationError as exc:
        return [_field_error_from_pydantic(e) for e in exc.errors()]
    except ValueError as exc:  # cross-field validators may raise bare ValueError
        return [FieldError(key=None, code="validation", message=str(exc))]
    return []


def _load_config_for_refs(candidate: dict[str, Any]) -> CoordinareConfiguration:
    """Build a :class:`CoordinareConfiguration` from a (valid) on-disk config dict.

    Used to compute referential-integrity referrers for delete-protection. Mirrors
    the placeholder-token substitution of :func:`_validate_candidate`.
    """
    from coordinare.config import CoordinareConfiguration
    from coordinare.config_descriptors import is_env_placeholder
    from coordinare.config_validation import coerce_multi_symphony_raw

    attempt = dict(candidate)
    token = attempt.get("github_token")
    if is_env_placeholder(token) or token is None:
        attempt["github_token"] = "ghp_placeholder_for_validation"
    return CoordinareConfiguration(**coerce_multi_symphony_raw(attempt))


def _referrers(config: CoordinareConfiguration, catalog: str, item_id: str) -> list[str]:
    """Return the referrers of ``item_id`` in ``catalog`` (research D6).

    Reuses the descriptor layer's reference DAG so delete-protection here stays
    consistent with the ``referenced_by`` / ``deletable`` flags the UI renders.
    """
    from coordinare import config_descriptors as cd

    for section in cd._build_catalog_sections(config):
        if section.id == catalog:
            for item in section.items:
                if item.id == item_id:
                    return list(item.referenced_by)
    return []


def _conflict_result(path: Path, base_hash: str) -> SaveResult | None:
    """Guard the optimistic-concurrency baseline before a write.

    Returns a ``validation`` :class:`SaveResult` if ``base_hash`` is missing/empty
    (a client error — without this an empty hash never matches the real content
    hash, surfacing a misleading "config changed on disk" conflict), a ``conflict``
    :class:`SaveResult` if it is present but stale, else None.
    """
    if not base_hash:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=None,
                    code="validation",
                    message="base_hash is required; reload the config and retry",
                )
            ],
        )
    try:
        guard_concurrency(path, base_hash)
    except ConcurrencyConflictError as err:
        return SaveResult(
            ok=False,
            errors=[FieldError(key=None, code="conflict", message=str(err))],
        )
    except OSError as exc:
        # The baseline re-read can fail operationally (unreadable path, a directory
        # where a file is expected due to a mount glitch). Surface a structured
        # forbidden result, consistent with the other tolerant read/write paths.
        #
        # 158 (#241): `str(exc)` on an OSError includes the absolute path, and this
        # message reaches the browser. Found because 158 copied this line for
        # consistency and a review caught the copy; the original had it too.
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=None,
                    code="forbidden",
                    message=(
                        f"Could not read the configuration file: "
                        f"{safe_failure_reason(exc)}"
                    ),
                )
            ],
        )
    return None


# --- T024: scalar-group section save ------------------------------------------


def save_section(request: SaveRequest, config_path: Path) -> SaveResult:
    """Validate + persist a ``scalar_group`` section edit (Phase 4, T024).

    Optimistic-concurrency guarded (409 ``conflict`` on a stale ``base_hash``),
    full-config validated (422 ``validation`` on failure, secret-free), atomically
    written, then classified hot-reload vs restart (T026). A secret field whose
    submitted value is still the mask is a no-op — the mask is never written back,
    preserving the on-disk ``${VAR}`` / real secret (research D3).
    """
    from coordinare.config_descriptors import (
        SECRET_MASK,
        annotation_for,
        scalar_field_names,
    )

    if request.store != "config_yaml":
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=None,
                    code="forbidden",
                    message=(
                        f"save_section only writes config.yaml, not store "
                        f"'{request.store}'"
                    ),
                )
            ],
        )
    if request.section not in SCALAR_SECTIONS:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=None,
                    code="forbidden",
                    message=(
                        f"section '{request.section}' is not an editable scalar "
                        f"group; supported: {', '.join(SCALAR_SECTIONS)}"
                    ),
                )
            ],
        )

    # Per-field allow-list: only real *scalar* fields of the section may be written.
    # ProjectConfiguration is extra="ignore", so without this an unknown key (typo)
    # or a non-scalar catalog/collection field (endpoints, personas, performers — each
    # with its own CRUD endpoint) would be persisted into config.yaml unchecked,
    # bypassing the per-section/per-catalog API boundaries.
    allowed_fields = scalar_field_names(request.section)
    unknown_fields = [f for f in request.changes if f not in allowed_fields]
    if unknown_fields:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=field,
                    code="validation",
                    message=(
                        f"'{field}' is not an editable scalar field of section "
                        f"'{request.section}'"
                    ),
                )
                for field in unknown_fields
            ],
        )

    # Reject a missing/empty base_hash up front (a client error, not a race) so we
    # do not spend a read/validate on a malformed request. The *stale-hash* guard,
    # though, is deferred to just before the write below (FR-016).
    if not request.base_hash:
        return _conflict_result(config_path, request.base_hash)

    loaded, read_error = _safe_read_config_dict(config_path, key=request.section)
    if read_error is not None:
        return read_error
    candidate = dict(loaded)

    for field, value in request.changes.items():
        ann = annotation_for(f"{request.section}.{field}")
        if ann.secret and value == SECRET_MASK:
            continue  # masked-unchanged secret → never overwrite with the mask
        candidate[field] = value

    # No-op short-circuit: an empty changes payload, or one that reduces to no
    # change after the masked-secret skips, leaves ``candidate`` identical to what
    # was read. Re-dumping would needlessly strip file-level comments (``safe_dump``)
    # and may trigger a hot reload for a write that changes nothing, so return a
    # success no-op carrying the unchanged on-disk hash (Copilot round 28).
    if candidate == loaded:
        return SaveResult(
            ok=True,
            applied=None,
            new_hash=compute_content_hash(config_path),
            message="No changes to apply.",
        )

    errors = _validate_candidate(candidate)
    if errors:
        return SaveResult(ok=False, errors=errors)

    # FR-016 ("reject immediately before swap"): re-check optimistic concurrency
    # here, after read/validate and immediately before the atomic write, to close
    # the TOCTOU window where config.yaml could change on disk between the initial
    # load and the swap (last-writer-wins clobber).
    conflict = _conflict_result(config_path, request.base_hash)
    if conflict is not None:
        return conflict

    new_hash, write_error = _safe_atomic_write(
        config_path, candidate, key=request.section
    )
    if write_error is not None:
        return write_error
    applied = classify_section_applied(request.section, request.changes.keys())
    return SaveResult(ok=True, applied=applied, new_hash=new_hash)


# --- T025: spec-080 catalog create / update / delete --------------------------


def _safe_read_config_dict(
    config_path: Path, *, key: str | None = None
) -> tuple[dict[str, Any], SaveResult | None]:
    """Parse ``config.yaml`` for a write path; tolerate absence, surface corruption.

    Returns ``(loaded_dict, None)`` for an absent file (``{}``), an empty file
    (``None`` document → ``{}``, treated as absent), or a valid mapping. Malformed
    or structurally corrupt content is NOT silently tolerated: returns
    ``({}, SaveResult)`` with a ``forbidden`` error when the path exists but is not
    a regular file (a directory / symlink-to-dir from a mount glitch), is
    unreadable (:class:`OSError`), contains invalid UTF-8
    (:class:`UnicodeDecodeError`), is malformed (:class:`yaml.YAMLError`), or
    parses to a valid-but-non-mapping document (a list/scalar at the root — the
    on-disk config is structurally corrupted). An out-of-band edit/mount glitch
    must surface a structured result, never an unstructured 500 (or a confusing
    pydantic "field required" error from falling through with ``{}``). Bailing
    (rather than silently treating corrupt content as ``{}``) also avoids
    clobbering the rest of the config on the subsequent write.
    """
    if not config_path.is_file():
        if config_path.exists():
            # Exists but is not a regular file (directory / symlink-to-dir / mount
            # glitch). Treating this as "absent" would fall through to the write
            # and surface confusing pydantic "field required" errors; surface the
            # read-only reality as a structured ``forbidden`` result instead.
            return {}, SaveResult(
                ok=False,
                errors=[
                    FieldError(
                        key=key,
                        code="forbidden",
                        message=(
                            "Configuration path exists but is not a regular file; "
                            "it is read-only."
                        ),
                    )
                ],
            )
        return {}, None
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return {}, SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=key,
                    code="forbidden",
                    message=(
                        f"Could not read the configuration file: "
                        f"{safe_failure_reason(exc)}"
                    ),
                )
            ],
        )
    if loaded is None:
        # Empty (or whitespace/comment-only) file — YAML parses to ``None``. Treat
        # it as absent: no content to corrupt, the write fills in the section.
        return {}, None
    if not isinstance(loaded, dict):
        # Parses fine but the root is a list/scalar — the on-disk config is
        # structurally corrupted. Surface a structured ``forbidden`` rather than
        # coercing to ``{}`` (which masks the real problem behind confusing pydantic
        # "field required" errors and would clobber the corrupt file on write).
        return {}, SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=key,
                    code="forbidden",
                    message=(
                        "Configuration file root is not a mapping; the file is "
                        "structurally corrupted and is read-only."
                    ),
                )
            ],
        )
    return loaded, None


def _reject_unknown_catalog(catalog: str) -> SaveResult | None:
    """Return a ``validation`` :class:`SaveResult` for an unsupported catalog key.

    Defensive backstop mirroring the dashboard endpoint guard. ``ProjectConfiguration``
    is ``extra="ignore"``, so a mis-routed internal call with a typo'd / unsupported
    ``catalog`` would otherwise persist a junk top-level key into ``config.yaml``
    without a validation error. Returns ``None`` for a known catalog.
    """
    if catalog in CATALOG_KEYS:
        return None
    return SaveResult(
        ok=False,
        errors=[
            FieldError(
                key=catalog,
                code="validation",
                message=f"Unknown catalog: {catalog}",
            )
        ],
    )


def _read_catalog_items(
    loaded: dict[str, Any], catalog: str
) -> tuple[list[Any], SaveResult | None]:
    """Return the on-disk catalog section as a list, or a ``forbidden`` result.

    A catalog section is a list of item mappings. An absent / ``None`` section is
    treated as the empty list. But a *present, non-list* section (e.g. a corrupted
    ``endpoints: {…}`` dict from a hand-edit/merge accident) must NOT be coerced via
    ``list(...)`` — that would iterate dict keys and yield misleading "No X item
    named …" / follow-on validation errors. Surface it as structurally
    corrupt/read-only instead (``forbidden``), mirroring ``_safe_read_config_dict``
    (Copilot round 29).
    """
    section = loaded.get(catalog)
    if section is None:
        return [], None
    if not isinstance(section, list):
        return [], SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=catalog,
                    code="forbidden",
                    message=(
                        f"The '{catalog}' section of config.yaml is "
                        f"{type(section).__name__}, not a list; the file is "
                        "structurally corrupted and is read-only."
                    ),
                )
            ],
        )
    return list(section), None


def create_catalog_item(
    catalog: str, item: dict[str, Any], base_hash: str, config_path: Path
) -> SaveResult:
    """Append a new item to a spec-080 catalog (Phase 4, T025).

    Concurrency-guarded, then validated against the full config so an unknown
    cross-catalog reference (e.g. a model_endpoint pointing at a missing endpoint)
    is rejected 422 ``validation`` naming the dangling reference.
    """
    unknown = _reject_unknown_catalog(catalog)
    if unknown is not None:
        return unknown

    if not base_hash:
        return _conflict_result(config_path, base_hash)

    loaded, read_error = _safe_read_config_dict(config_path, key=catalog)
    if read_error is not None:
        return read_error
    items, shape_error = _read_catalog_items(loaded, catalog)
    if shape_error is not None:
        return shape_error
    items.append(item)
    candidate = {**loaded, catalog: items}

    errors = _validate_candidate(candidate)
    if errors:
        return SaveResult(ok=False, errors=errors)

    # FR-016: re-check optimistic concurrency immediately before the swap (see
    # save_section) to close the read→write clobber window.
    conflict = _conflict_result(config_path, base_hash)
    if conflict is not None:
        return conflict

    new_hash, write_error = _safe_atomic_write(config_path, candidate, key=catalog)
    if write_error is not None:
        return write_error
    return SaveResult(ok=True, applied="hot_reloaded", new_hash=new_hash)


def update_catalog_item(
    catalog: str,
    item_id: str,
    changes: dict[str, Any],
    base_hash: str,
    config_path: Path,
) -> SaveResult:
    """Apply ``changes`` to the named item in a spec-080 catalog (Phase 4, T025)."""
    unknown = _reject_unknown_catalog(catalog)
    if unknown is not None:
        return unknown

    # Reject a non-mapping ``changes`` up front. A list/string would otherwise hit
    # ``{**it, **changes}`` below and raise TypeError (a 500) instead of the
    # structured validation result the rest of the write paths guarantee (mirrors
    # routing_config_service.update_routing_entry's guard).
    if not isinstance(changes, dict):
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=item_id,
                    code="validation",
                    message=f"{catalog} item changes must be a mapping of fields.",
                )
            ],
        )

    if not base_hash:
        return _conflict_result(config_path, base_hash)

    loaded, read_error = _safe_read_config_dict(config_path, key=catalog)
    if read_error is not None:
        return read_error
    items, shape_error = _read_catalog_items(loaded, catalog)
    if shape_error is not None:
        return shape_error
    found = False
    new_items: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, dict) and it.get("name") == item_id:
            it = {**it, **changes}
            found = True
        new_items.append(it)

    if not found:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=item_id,
                    code="validation",
                    message=f"No {catalog} item named '{item_id}'.",
                )
            ],
        )

    candidate = {**loaded, catalog: new_items}
    errors = _validate_candidate(candidate)
    if errors:
        return SaveResult(ok=False, errors=errors)

    # FR-016: re-check optimistic concurrency immediately before the swap (see
    # save_section) to close the read→write clobber window.
    conflict = _conflict_result(config_path, base_hash)
    if conflict is not None:
        return conflict

    new_hash, write_error = _safe_atomic_write(config_path, candidate, key=catalog)
    if write_error is not None:
        return write_error
    return SaveResult(ok=True, applied="hot_reloaded", new_hash=new_hash)


def delete_catalog_item(
    catalog: str, item_id: str, base_hash: str, config_path: Path
) -> SaveResult:
    """Delete a catalog item unless something references it (Phase 4, T025).

    Delete-protection (research D6): if the item is referenced, return 409
    ``referenced`` naming the referrer(s) so the operator knows what to detach
    first; nothing is written.
    """
    unknown = _reject_unknown_catalog(catalog)
    if unknown is not None:
        return unknown

    if not base_hash:
        return _conflict_result(config_path, base_hash)

    loaded, read_error = _safe_read_config_dict(config_path, key=catalog)
    if read_error is not None:
        return read_error

    existing, shape_error = _read_catalog_items(loaded, catalog)
    if shape_error is not None:
        return shape_error

    # Validate the named item actually exists before doing anything (mirrors
    # update_catalog_item). Without this the filter below is a no-op yet the file
    # is still rewritten and ok=True returned — a misleading "successful" delete
    # that changed nothing.
    if not any(
        isinstance(it, dict) and it.get("name") == item_id for it in existing
    ):
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=item_id,
                    code="validation",
                    message=f"No {catalog} item named '{item_id}'.",
                )
            ],
        )

    # Compute referrers from the current (valid) on-disk config before deleting.
    # FAIL CLOSED: if the reference graph can't be computed we must NOT proceed —
    # silently treating the item as unreferenced could delete something still in
    # use and leave dangling references, violating the delete-protection guarantee.
    try:
        referrers = _referrers(_load_config_for_refs(loaded), catalog, item_id)
    except Exception:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=item_id,
                    code="forbidden",
                    message=(
                        f"Cannot delete '{item_id}' — its reference graph could not "
                        "be computed, so delete-protection cannot be verified. "
                        "Fix the configuration and retry."
                    ),
                )
            ],
        )
    if referrers:
        return SaveResult(
            ok=False,
            errors=[
                FieldError(
                    key=item_id,
                    code="referenced",
                    message=(
                        f"Cannot delete '{item_id}' — it is referenced by "
                        f"{', '.join(referrers)}. Remove those references first."
                    ),
                )
            ],
        )

    items = [
        it
        for it in existing
        if not (isinstance(it, dict) and it.get("name") == item_id)
    ]
    candidate = {**loaded, catalog: items}

    errors = _validate_candidate(candidate)
    if errors:
        return SaveResult(ok=False, errors=errors)

    # FR-016: re-check optimistic concurrency immediately before the swap (see
    # save_section) to close the read→write clobber window.
    conflict = _conflict_result(config_path, base_hash)
    if conflict is not None:
        return conflict

    new_hash, write_error = _safe_atomic_write(config_path, candidate, key=catalog)
    if write_error is not None:
        return write_error
    return SaveResult(ok=True, applied="hot_reloaded", new_hash=new_hash)
