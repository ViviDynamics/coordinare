"""Spec 136 — the declarative config search space.

A space definition (YAML) names a baseline coordinare configuration, the swept
dimensions (each a dotted config path plus an explicit list of allowed values),
and named whole-config candidates. Everything not listed is fixed at the
baseline value — the space is explicitly bounded, never a cross-product.

Loading validates everything up front (FR-002): the baseline must be a valid
``CoordinareConfiguration``, every dimension path must resolve in the baseline,
and every choice/candidate must materialize through the real config schema.
Failures name the offending element; nothing runs on a partially valid space.

The baseline is the multi-symphony ROOT configuration (``CoordinareConfiguration``:
``global_config`` + ``symphonies``) — the same shape the production daemon splits
into ``state["config"]`` / ``state["symphony_configs"]``. Path semantics:
dot-separated keys over that tree (global knobs live under ``global_config.…``);
the segment after ``symphonies`` is a symphony *name* resolved against
``SymphonyConfig.name`` (research.md R2), so definitions survive list reordering.

Contract: ``specs/136-board-bench-sweep/contracts/search-space.md``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePath
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator

from coordinare.config import CoordinareConfiguration


class SpaceError(ValueError):
    """A definition that cannot be fully validated fails loudly (FR-002)."""


#: Dotted-path template for a role's harness (spec 161). Every consumer keys on
#: ``Dimension.path``, so the ``role:`` shorthand is normalized into this at load time
#: rather than branching downstream.
HARNESS_PATH_TEMPLATE = "global_config.performers.{role}.backend"


def _is_harness_path(path: str) -> bool:
    """True when *path* targets a performer's harness (backend) selection."""
    parts = path.split(".")
    return (
        len(parts) == 4
        and parts[0] == "global_config"
        and parts[1] == "performers"
        and parts[3] == "backend"
    )


class Dimension(BaseModel):
    name: str
    path: str = ""
    #: Shorthand for a harness dimension: ``role: reviewer`` expands to
    #: ``global_config.performers.reviewer.backend`` (spec-161 FR-014). Exactly one of
    #: ``path`` or ``role`` must be given.
    role: str = ""
    choices: list[Any] = Field(min_length=1)
    description: str = ""

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Dimension:
        if bool(self.path) == bool(self.role):
            msg = (
                f"dimension {self.name!r}: give exactly one of 'path' or 'role' "
                "(role is shorthand for the performer's backend path)"
            )
            raise ValueError(msg)
        if self.role:
            # Normalize immediately so sweep/optimizer/reporting keep reading `path`.
            self.path = HARNESS_PATH_TEMPLATE.format(role=self.role)
            # Clear `role` once consumed, or the model carries BOTH fields and fails its
            # own exactly-one-of check when dumped and reloaded — breaking every
            # round-trip (persisting a space, model_copy, revalidating a dumped dict).
            # `role` is shorthand for authoring, not state to keep afterwards.
            self.role = ""
        return self

    @field_validator("choices")
    @classmethod
    def _choices_must_be_unique(cls, v: list[Any]) -> list[Any]:
        # A duplicated choice would silently run (and delta-report) the same
        # point twice while coverage still reconciles — corrupt data, not a
        # crash — so it is rejected at definition load time.
        seen: list[Any] = []
        for choice in v:
            if choice in seen:
                msg = f"duplicate choice {choice!r} in dimension choices"
                raise ValueError(msg)
            seen.append(choice)
        return v


class Candidate(BaseModel):
    name: str
    overrides: dict[str, Any] = Field(min_length=1)
    description: str = ""


class SearchSpace(BaseModel):
    name: str
    baseline_config: str
    dimensions: list[Dimension] = []
    candidates: list[Candidate] = []

    @model_validator(mode="after")
    def _at_least_one_of_dimensions_or_candidates(self) -> SearchSpace:
        if not self.dimensions and not self.candidates:
            msg = "empty search space: declare at least one dimension or candidate"
            raise ValueError(msg)
        names = [d.name for d in self.dimensions] + [c.name for c in self.candidates]
        if len(names) != len(set(names)):
            msg = "dimension/candidate names must be unique within the space"
            raise ValueError(msg)
        return self


@dataclass
class LoadedSpace:
    """A fully validated space: the definition plus its parsed baseline."""

    definition: SearchSpace
    baseline: CoordinareConfiguration
    baseline_dump: dict[str, Any]
    source_path: Path


def _walk(dump: dict[str, Any], path: str) -> tuple[Any, str]:
    """Navigate to the parent container of ``path``; return (parent, last_key).

    The segment following ``symphonies`` is a symphony name resolved to its
    list entry. Raises SpaceError naming the first unresolvable segment.
    """
    parts = path.split(".")
    node: Any = dump
    for part in parts[:-1]:
        if isinstance(node, list):
            # only reachable right after a "symphonies" segment
            match = next(
                (entry for entry in node if isinstance(entry, dict) and entry.get("name") == part),
                None,
            )
            if match is None:
                msg = f"path {path!r}: no symphony named {part!r}"
                raise SpaceError(msg)
            node = match
        elif isinstance(node, dict):
            if part not in node or node[part] is None:
                # a None sub-model (e.g. persona_scope: null) can't be descended into
                msg = f"path {path!r}: segment {part!r} does not resolve in the baseline"
                raise SpaceError(msg)
            node = node[part]
        else:
            msg = f"path {path!r}: segment {part!r} is a leaf, cannot descend"
            raise SpaceError(msg)
    last = parts[-1]
    if isinstance(node, dict):
        if last not in node:
            msg = f"path {path!r}: final segment {last!r} does not resolve in the baseline"
            raise SpaceError(msg)
        return node, last
    msg = f"path {path!r}: parent of {last!r} is not a mapping"
    raise SpaceError(msg)


def resolve_path(dump: dict[str, Any], path: str) -> Any:
    parent, last = _walk(dump, path)
    return parent[last]


def materialize(baseline_dump: dict[str, Any], overrides: dict[str, Any]) -> CoordinareConfiguration:
    """Baseline + overrides → a schema-validated CoordinareConfiguration.

    ``CoordinareConfiguration`` is a plain BaseModel (extra="forbid", no env
    sources), so materialization is deterministic. A rejected value raises SpaceError naming the path and value — never a
    silently coerced config (US1 scenario 3)."""
    dump = copy.deepcopy(baseline_dump)
    for path, value in overrides.items():
        parent, last = _walk(dump, path)
        parent[last] = value
    try:
        return CoordinareConfiguration(**dump)
    except ValidationError as exc:
        applied = ", ".join(f"{p}={v!r}" for p, v in overrides.items())
        msg = f"materialization rejected by the config schema (overrides: {applied}): {exc}"
        raise SpaceError(msg) from exc


def _fingerprint_fallback(obj: Any) -> str:
    """JSON fallback for fingerprinting: reveal secret VALUES into the hash
    input (never into any output) so two configs differing only in a secret
    still get distinct fingerprints (FR-004) — ``model_dump_json`` would mask
    every SecretStr as ``**********`` and collapse them."""
    if isinstance(obj, SecretStr):
        return obj.get_secret_value()
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    return str(obj)


def config_fingerprint(config: CoordinareConfiguration) -> str:
    """SHA-256 (16 hex) of the canonical dump — the point's identity (FR-004).

    The hash input is the python-mode dump serialized with secrets revealed;
    only the digest ever leaves this function."""
    canonical = json.dumps(
        config.model_dump(mode="python"), sort_keys=True, default=_fingerprint_fallback,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _validate_harness_choices(dim: Dimension) -> None:
    """Reject an unknown harness name at load time (spec-161 FR-017).

    Necessary because the coordinare config schema does NOT constrain ``backend`` to the
    known harness set: ``materialize`` happily accepts ``backend: not_a_harness``, so a
    typo would expand into a sweep point, run, and only fail once a performer tried to
    dispatch it — partway through what may be a long sweep, with the earlier points'
    budget already spent.

    An unknown *role* needs no handling here: ``_walk`` already rejects it while resolving
    the path, naming the offending segment.
    """
    from performer.backends import SUPPORTED_BACKENDS

    unknown = [c for c in dim.choices if c not in SUPPORTED_BACKENDS]
    if unknown:
        supported = ", ".join(sorted(SUPPORTED_BACKENDS))
        msg = (
            f"dimension {dim.name!r}: unknown harness "
            f"{', '.join(repr(u) for u in unknown)} in choices; supported: {supported}"
        )
        raise SpaceError(msg)


def load_space(path: str | Path) -> LoadedSpace:
    """Load + fully validate a space definition (FR-001/FR-002)."""
    space_path = Path(path)
    try:
        raw = yaml.safe_load(space_path.read_text()) or {}
    except OSError as exc:
        msg = f"cannot read space definition {space_path}: {exc}"
        raise SpaceError(msg) from exc
    try:
        definition = SearchSpace(**raw)
    except ValidationError as exc:
        msg = f"invalid space definition {space_path}: {exc}"
        raise SpaceError(msg) from exc

    baseline_path = (space_path.parent / definition.baseline_config).resolve()
    try:
        baseline_raw = yaml.safe_load(baseline_path.read_text()) or {}
    except OSError as exc:
        msg = f"cannot read baseline config {baseline_path}: {exc}"
        raise SpaceError(msg) from exc
    try:
        baseline = CoordinareConfiguration(**baseline_raw)
    except ValidationError as exc:
        msg = f"baseline config {baseline_path} is not a valid root (global_config + symphonies) coordinare configuration: {exc}"
        raise SpaceError(msg) from exc
    baseline_dump = baseline.model_dump(mode="json")

    for dim in definition.dimensions:
        resolve_path(baseline_dump, dim.path)  # path must exist (raises naming it)
        if _is_harness_path(dim.path):
            _validate_harness_choices(dim)
        for choice in dim.choices:
            try:
                materialize(baseline_dump, {dim.path: choice})
            except SpaceError as exc:
                msg = f"dimension {dim.name!r}: choice {choice!r} is invalid: {exc}"
                raise SpaceError(msg) from exc
    for cand in definition.candidates:
        try:
            materialize(baseline_dump, cand.overrides)
        except SpaceError as exc:
            msg = f"candidate {cand.name!r} does not materialize: {exc}"
            raise SpaceError(msg) from exc

    return LoadedSpace(
        definition=definition,
        baseline=baseline,
        baseline_dump=baseline_dump,
        source_path=space_path,
    )
