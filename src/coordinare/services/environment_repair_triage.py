"""Failure triage for an execution repair: the closure a delta repair may change, computed by the daemon without a model.

Pure and deterministic: the same failed manifest and report always give the same triage and digest. Roots are the
failed or indeterminate results; not_run results are consequences and never roots. The closure is the roots, the
service lifecycle units they belong to, their depends_on ancestors and their transitive dependents, plus the criteria
and requirements linked to any of those records. Every other record is frozen by the repair guard.

Criterion ownership is not recorded in the manifest (groups come from the reviewed scope), so ``touched_parts`` names
the owners of the closure's records only; a linked criterion belongs to the group that owns it in the scope.

Clean-phase roots (checks, services, activation) cannot depend on install commands, so depends_on never names the
installer that produced what they read. For those roots every install command joins the closure as a candidate
producer. An install-rooted failure keeps the depends_on closure alone.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from coordinare_service_inference.environment.models import (
        Command,
        EnvironmentManifest,
        VerificationReport,
    )

RootClass = Literal["command", "activation_path", "service", "capability", "network_gate"]
GLOBAL = "global"
ACTIVATION = "activation"
LIFECYCLE = ("init", "start", "health", "stop")
_CHECK_GROUP = re.compile(r"^(c[0-9]{2,3})[a-z]*-")
_CONNECTIVITY = re.compile(r"\b(?:curl|wget)\b[^;&|]*?(?:\s-I\b|\s--head\b|/dev/null)|\bping\s|\bnc\s+-[A-Za-z]*z\b")
_MISSING_EXECUTABLE = re.compile(r"not found|No such file or directory", re.IGNORECASE)


class RepairTriage(BaseModel):
    """What one failed execution implicates, as the daemon persists it before any author is called."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_digest: str
    roots: dict[str, RootClass]
    consequences: tuple[str, ...]
    closure_ids: tuple[str, ...]
    criterion_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    touched_parts: tuple[str, ...]
    digest: str


def is_connectivity_gate(command: Command) -> bool:
    """An install that only tests reachability (a HEAD request, a download to /dev/null, ping or nc -z)."""
    return _CONNECTIVITY.search(" ".join(command.argv)) is not None


def part_of(identifier: str) -> str:
    """The check group owning a check id by prefix; checks outside every prefix are the global author's."""
    match = _CHECK_GROUP.match(identifier)
    return f"{match[1]}-" if match else GLOBAL


def _records(manifest: EnvironmentManifest) -> dict[str, tuple[Command, str | None, str]]:
    """Every executable command by id: (command, service id or None, kind)."""
    records: dict[str, tuple[Command, str | None, str]] = {}
    records.update({command.id: (command, None, "install") for command in manifest.install})
    records.update({command.id: (command, None, "check") for command in manifest.checks})
    for service in manifest.services:
        for phase in LIFECYCLE:
            if (command := getattr(service, phase)) is not None:
                records[command.id] = (command, service.id, "service")
    return records


def _prerequisites(manifest: EnvironmentManifest, records: dict[str, tuple[Command, str | None, str]]) -> dict[str, set[str]]:
    edges = {identifier: {item for item in command.depends_on if item in records}
             for identifier, (command, _service, _kind) in records.items()}
    for service in manifest.services:
        lifecycle = [command for command in (service.init, service.start, service.health) if command is not None]
        for previous, current in pairwise(lifecycle):
            edges[current.id].add(previous.id)
    return edges


def _root_class(command: Command, kind: str, service: str | None, output: str) -> RootClass:
    if service is not None:
        return "service"
    if kind == "install" and is_connectivity_gate(command):
        return "network_gate"
    if _MISSING_EXECUTABLE.search(output):
        return "capability"
    return "command"


def _reach(seeds: set[str], neighbours: dict[str, set[str]]) -> set[str]:
    """Every node reachable from the seeds along one edge direction, seeds included."""
    reached = set(seeds)
    pending = list(seeds)
    while pending:
        for node in neighbours.get(pending.pop(), set()):
            if node not in reached:
                reached.add(node)
                pending.append(node)
    return reached


def _closure(seeds: set[str], edges: dict[str, set[str]]) -> set[str]:
    """Seeds, their prerequisites, and what depends on the seeds. Prerequisites of dependents are not pulled in."""
    dependents: dict[str, set[str]] = defaultdict(set)
    for child, parents in edges.items():
        for parent in parents:
            dependents[parent].add(child)
    return _reach(seeds, edges) | _reach(seeds, dependents)


def triage_failure(manifest: EnvironmentManifest, report: VerificationReport) -> RepairTriage:
    """Triage one failed execution. Raises ValueError when the report has no failed or indeterminate result."""
    records = _records(manifest)
    results = {result.id: result for result in report.results}
    roots: dict[str, RootClass] = {}
    for result in report.results:
        if result.status not in {"fail", "indeterminate"}:
            continue
        if result.id == ACTIVATION and manifest.activation.path:
            roots[result.id] = "activation_path"
        elif result.id in records:
            command, service, kind = records[result.id]
            roots[result.id] = _root_class(command, kind, service, result.output)
    if not roots:
        message = "execution failure has no failed result to triage"
        raise ValueError(message)

    members: dict[str, set[str]] = defaultdict(set)
    for identifier, (_command, service, _kind) in records.items():
        if service is not None:
            members[service].add(identifier)
    seeds = set(roots)
    for identifier in roots:
        service = records[identifier][1] if identifier in records else None
        if service is not None:
            seeds |= members[service]
    closure = _closure(seeds, _prerequisites(manifest, records))
    clean_root = any(identifier == ACTIVATION or (identifier in records and records[identifier][2] != "install")
                     for identifier in roots)
    if clean_root:
        closure |= {identifier for identifier, (_command, _service, kind) in records.items() if kind == "install"}
        closure = _closure(closure, _prerequisites(manifest, records))
    if ACTIVATION in roots:
        closure.add(ACTIVATION)

    criteria = sorted(criterion for criterion, identifiers in manifest.criterion_checks.items() if set(identifiers) & closure)
    requirements = sorted(requirement.id for requirement in manifest.requirements if set(requirement.checks) & closure)
    touched = sorted({part_of(identifier) if identifier in records and records[identifier][2] == "check" else GLOBAL
                      for identifier in closure})
    consequences = sorted(result.id for result in report.results if result.status == "not_run")
    closed_records = {identifier: records[identifier][0].model_dump(mode="json") for identifier in sorted(closure) if identifier in records}
    if ACTIVATION in closure:
        closed_records[ACTIVATION] = manifest.activation.model_dump(mode="json")
    payload = {"manifest_digest": manifest.digest(), "roots": dict(sorted(roots.items())), "consequences": consequences,
               "closure": closed_records, "criteria": criteria, "requirements": requirements, "touched": touched,
               "root_results": {identifier: results[identifier].status for identifier in sorted(roots)}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return RepairTriage(manifest_digest=manifest.digest(), roots=dict(sorted(roots.items())), consequences=tuple(consequences),
                        closure_ids=tuple(sorted(closure)), criterion_ids=tuple(criteria), requirement_ids=tuple(requirements),
                        touched_parts=tuple(touched), digest=digest)
