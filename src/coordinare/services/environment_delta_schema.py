"""Delta records: the envelope a touched author returns (changed and added records only) and the merge onto a base."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.findings import FindingResponse
from coordinare_service_inference.environment.models import (  # noqa: TC002 - pydantic resolves field types at runtime
    Activation,
    Command,
    LocalCredential,
    Requirement,
    Service,
)
from pydantic import BaseModel, ConfigDict, Field, create_model

if TYPE_CHECKING:
    from collections.abc import Mapping

RECORD_GROUPS = ("requirements", "install", "checks", "services")


class ManifestDelta(BaseModel):
    """Changed and added global records only; omitted records keep their base content."""

    model_config = ConfigDict(extra="forbid", strict=True)

    requirements: list[Requirement] = Field(default_factory=list, max_length=256)
    install: list[Command] = Field(default_factory=list, max_length=256)
    checks: list[Command] = Field(default_factory=list, max_length=256)
    services: list[Service] = Field(default_factory=list, max_length=64)
    activation: Activation | None = None
    criterion_checks: dict[str, list[str]] = Field(default_factory=dict)
    # A failure such as an undeclared ${NAME} is repaired by declaring a generated credential (Dettmore e2e-05).
    local_credentials: list[LocalCredential] = Field(default_factory=list, max_length=64)


MANIFEST_DELTA_ENVELOPE = create_model(
    "ManifestDeltaEnvelope", manifest_delta=(ManifestDelta, ...), finding_responses=(list[FindingResponse], ...),
    __config__=ConfigDict(extra="forbid", strict=True),
)


def _merge(base: list[Any], changes: list[Any]) -> list[Any]:
    """A change replaces the first base record with its id; every other change is an addition, so a duplicate id
    stays visible to the record-id admission rule rather than being silently chosen."""
    known = {item["id"] for item in base if isinstance(item, dict)}
    replacing: dict[str, Any] = {}
    added: list[Any] = []
    for item in changes:
        identifier = item["id"]
        if identifier in known and identifier not in replacing:
            replacing[identifier] = item
        else:
            added.append(item)
    merged = [replacing.get(item["id"], item) if isinstance(item, dict) else item for item in base]
    return merged + added


def apply_delta(base: Mapping[str, Any], delta: Mapping[str, Any]) -> dict[str, Any]:
    """The base proposal with the delta's changed and added global records applied."""
    result = copy.deepcopy(dict(base))
    for key in RECORD_GROUPS:
        if delta.get(key):
            result[key] = _merge(list(result.get(key) or []), list(delta[key]))
    if delta.get("activation") is not None:
        result["activation"] = delta["activation"]
    if delta.get("criterion_checks"):
        result["criterion_checks"] = {**(result.get("criterion_checks") or {}), **delta["criterion_checks"]}
    if delta.get("local_credentials"):
        # Keyed by name, as records are by id: the first delta credential of a name replaces the base one, and every
        # other is added, so a duplicate name stays visible to admission rather than being silently chosen.
        base_credentials = list(result.get("local_credentials") or [])
        known = {item["name"] for item in base_credentials}
        replacing: dict[str, Any] = {}
        added: list[Any] = []
        for item in delta["local_credentials"]:
            if item["name"] in known and item["name"] not in replacing:
                replacing[item["name"]] = item
            else:
                added.append(item)
        result["local_credentials"] = [replacing.get(item["name"], item) for item in base_credentials] + added
    return result


def apply_group_delta(base_part: Mapping[str, Any] | None, delta: Mapping[str, Any]) -> dict[str, Any]:
    """A check author's changed and added checks and mappings applied to its base part."""
    base = dict(base_part or {"checks": [], "criterion_checks": {}})
    return {"checks": _merge(list(base.get("checks") or []), list(delta.get("checks") or [])),
            "criterion_checks": {**(base.get("criterion_checks") or {}), **(delta.get("criterion_checks") or {})}}
