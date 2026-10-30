"""Deterministic preservation of established environment obligations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.models import (
    Activation,
    Command,
    EnvironmentManifest,
    Requirement,
    Service,
    VerificationReport,
    content_key,
)

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_repair_triage import ACTIVATION
from coordinare.services.environment_scope import preserve_scope, released_criteria

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from coordinare.services.environment_repair_triage import RepairTriage


def _required_execution_commands(manifest: EnvironmentManifest) -> list[Command]:
    commands = [command for command in manifest.checks if command.required]
    for service in manifest.services:
        if service.required:
            commands.extend(
                command
                for command in (service.init, service.start, service.health, service.stop)
                if command is not None
            )
    return commands


def _executed_manifest(entry: Any, source_commit: str) -> EnvironmentManifest | None:
    manifest = EnvironmentManifest.model_validate(entry["manifest"])
    data = dict(entry["report"])
    data.pop("passed", None)
    report = VerificationReport.model_validate(data)
    digest = manifest.digest()
    by_id = {result.id: result for result in report.results}
    if (
        manifest.source_commit != source_commit
        or entry["manifest_digest"] != digest
        or report.manifest_digest != digest
        or len(by_id) != len(report.results)
        or any(
            result.manifest_digest != digest or result.duration_seconds < 0
            for result in report.results
        )
        or not any(result.required and result.status == "fail" for result in report.results)
    ):
        message = "execution history identity or results differ"
        raise ValueError(message)
    if not any(
        result.exit_code is not None or result.duration_seconds > 0 for result in report.results
    ):
        return None  # Trusted preflight stopped every project command before execution.
    required = [
        command
        for command in _required_execution_commands(manifest)
        if not report.scope or not command.scope or report.scope in command.scope
    ]
    if any(command.id not in by_id or not by_id[command.id].required for command in required):
        message = "execution history omits required commands"
        raise ValueError(message)
    return manifest


_MAX_REJECTED_EXECUTIONS = 3
_MAX_EXECUTION_HISTORY_BYTES = 4 * 1024 * 1024


def _bounded_history(history: object) -> list[Any]:
    if (
        not isinstance(history, list)
        or len(history) > _MAX_REJECTED_EXECUTIONS
        or len(json.dumps(history, allow_nan=False).encode()) > _MAX_EXECUTION_HISTORY_BYTES
    ):
        message = "execution history exceeds its bound"
        raise ValueError(message)
    return history


def executed_manifests(history: object, *, source_commit: str) -> list[EnvironmentManifest]:
    """Derive pins from bounded existing evidence, including after daemon restart."""
    try:
        return [
            manifest
            for entry in _bounded_history(history)
            if (manifest := _executed_manifest(entry, source_commit)) is not None
        ]
    except (KeyError, TypeError, ValueError):
        message = "execution history is malformed or unbound"
        raise EnvironmentDiscoveryError(message, code="execution_history_invalid") from None


def fixed_command_ids(manifests: Sequence[EnvironmentManifest]) -> frozenset[str]:
    """Every command execution preservation fixes: the required checks and required services' lifecycle commands of
    each executed manifest. Only their timeouts may grow."""
    return frozenset(command.id for manifest in manifests for command in _required_execution_commands(manifest))


def fixed_commands(manifests: Sequence[EnvironmentManifest]) -> dict[str, dict[str, Any]]:
    """Each fixed command as preservation accepts it: as executed, with the longest timeout any execution gave it.
    Authors are given these records, never their own last proposal, which may be the rejected change."""
    fixed: dict[str, dict[str, Any]] = {}
    for manifest in manifests:
        for command in _required_execution_commands(manifest):
            record = command.model_dump(mode="json")
            if command.id in fixed:
                earlier = fixed[command.id]
                if {**record, "timeout_seconds": 0} != {**earlier, "timeout_seconds": 0}:
                    # No candidate could preserve both; preservation itself would never have let them diverge.
                    message = f"executions fixed {_id(command.id)} differently; no repair can preserve both"
                    raise EnvironmentDiscoveryError(message, code="execution_history_invalid")
                record["timeout_seconds"] = max(record["timeout_seconds"], earlier["timeout_seconds"])
            fixed[command.id] = record
    return fixed


def preserve_execution_commands(
    manifest: EnvironmentManifest,
    previous: EnvironmentManifest,
) -> None:
    """Repair the environment around established checks, never rewrite their result.

    Every changed command is named at once: Dettmore e2e-18's author, told of one at a time, restored it and changed
    the next, until the schema corrections ran out.
    """
    current = {command.id: command for command in _required_execution_commands(manifest)}
    changed = [old.id for old in _required_execution_commands(previous)
               if (new := current.get(old.id)) is None
               or new.model_dump(exclude={"timeout_seconds"}) != old.model_dump(exclude={"timeout_seconds"})
               or new.timeout_seconds < old.timeout_seconds]
    if len(changed) == 1:
        message = f"required executable command {_id(changed[0])} changed after project execution"
        raise EnvironmentDiscoveryError(message, code="obligation_removed")
    if changed:
        message = (f"required executable commands changed after project execution; keep each as it was: "
                   f"{', '.join(_id(identifier) for identifier in changed[:16])}")
        raise EnvironmentDiscoveryError(message, code="obligation_removed")


def preserve_execution_obligations(manifest: EnvironmentManifest, history: object) -> None:
    for previous in executed_manifests(history, source_commit=manifest.source_commit):
        preserve_execution_commands(manifest, previous)


SAFE_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}')


def _id(identifier: str) -> str:
    """Only id-shaped values are named, so a free-form id the model wrote is never echoed."""
    return identifier if SAFE_ID.fullmatch(identifier) else '(unnamed)'


def _ids(identifiers: set[str]) -> str:
    return ', '.join(sorted(_id(identifier) for identifier in identifiers))


def duplicate_aliases(previous: EnvironmentManifest, candidate: EnvironmentManifest) -> dict[str, str]:
    """Dropped command id -> the kept command that replaces it: an identical argv, cwd, env and scope under another id.

    The kept copy must take over the dropped one's obligations: required if the dropped one was, every dependency it had,
    and at least its timeout. A correction that merges duplicates this way keeps every obligation.
    """
    before, after = _commands(previous), _commands(candidate)
    previous_checks, candidate_checks = {check.id for check in previous.checks}, {check.id for check in candidate.checks}
    aliases: dict[str, str] = {}
    for identifier, old in before.items():
        if identifier in after:
            continue
        # A replacement of the same kind first: only a check can keep a dropped check's criterion coverage, so an
        # identical install command must never be chosen over the kept check (a merge fold keeps the check).
        same_kind = identifier in previous_checks
        for new in sorted(after.values(), key=lambda command: (command.id in candidate_checks) != same_kind):
            if (content_key(new) == content_key(old) and (new.required or not old.required)
                    and set(old.depends_on) <= set(new.depends_on) and new.timeout_seconds >= old.timeout_seconds):
                aliases[identifier] = new.id
                break
    return aliases


def _preserve_criterion_checks(manifest: EnvironmentManifest, previous: EnvironmentManifest, released: set[str],
                               aliases: Mapping[str, str] | None = None) -> None:
    # Every lost mapping at once: one per merge round left v24's author fixing them one criterion at a time.
    aliases = aliases or {}
    lost = [f'{_id(identifier)} must still include {_ids(missing)}'
            for identifier, ids in sorted(previous.criterion_checks.items())
            if identifier not in released and (missing := {item for item in ids if item not in _mapped(manifest, identifier)
                                                         and aliases.get(item) not in _mapped(manifest, identifier)})]
    if lost:
        message = f"required source criterion coverage removed during repair: criterion_checks for {'; '.join(lost[:16])}"
        raise EnvironmentDiscoveryError(message, code='obligation_removed')


def _released_checks(previous: EnvironmentManifest, released: set[str]) -> set[str]:
    """Checks whose every criterion mapping is released: a release retires them with their criteria, and no other."""
    mapped: dict[str, set[str]] = {}
    for criterion, identifiers in previous.criterion_checks.items():
        for identifier in identifiers:
            mapped.setdefault(identifier, set()).add(criterion)
    return {identifier for identifier, criteria in mapped.items() if criteria <= released}


def _mapped(manifest: EnvironmentManifest, criterion: str) -> list[str]:
    return list(manifest.criterion_checks.get(criterion, []))


def preserve_obligations(manifest: EnvironmentManifest, previous: EnvironmentManifest) -> None:
    """A fresh judgment can add coverage, never waive a previously required obligation."""
    aliases = duplicate_aliases(previous, manifest)
    if previous.scope_contract is not None and manifest.scope_contract is None:
        message = 'required source scope removed during repair'
        raise EnvironmentDiscoveryError(message, code='obligation_removed')
    released = released_criteria(manifest.scope_contract.finding_ledger) if manifest.scope_contract is not None else set()
    if previous.scope_contract is not None and manifest.scope_contract is not None:
        preserve_scope(previous.scope_contract.definition, manifest.scope_contract.definition, released=released)
    _preserve_criterion_checks(manifest, previous, released, aliases)
    retired = _released_checks(previous, released)
    for group in ("requirements", "install", "checks", "services"):
        current = {item.id: item for item in getattr(manifest, group) if item.required}
        for old in getattr(previous, group):
            if not old.required:
                continue
            new = current.get(old.id)
            # A check retired with the released criterion it alone served may leave.
            if new is None and aliases.get(old.id) not in current and not (group == "checks" and old.id in retired):
                message = f"required obligation removed during repair: restore required {group} entry {_id(old.id)}"
                raise EnvironmentDiscoveryError(message, code="obligation_removed", global_owned=group != "checks")
            if new is None:
                continue  # Merged into an identical kept command, which the alias rule already checked.
            if group == "requirements" and (lost := {item for item in old.checks if item not in new.checks
                                                     and aliases.get(item) not in new.checks and item not in retired}):
                message = f"required obligation coverage removed: requirement {_id(old.id)} must still list checks {_ids(lost)}"
                raise EnvironmentDiscoveryError(message, code="obligation_removed", global_owned=True)
            if group in {"checks", "install"} and old.scope != new.scope:
                message = f"required obligation scope changed: {group} entry {_id(old.id)} must keep its previous scope"
                raise EnvironmentDiscoveryError(message, code="obligation_removed", global_owned=group != "checks")
            if group == "services":
                for phase in ("init", "start", "health", "stop"):
                    before, after = getattr(old, phase), getattr(new, phase)
                    if before is not None and (
                        after is None
                        or (before.id != after.id and aliases.get(before.id) != after.id)
                        or before.scope != after.scope
                        or (before.required and not after.required)
                    ):
                        message = (f"required service obligation changed: service {_id(old.id)} must keep its required "
                                   f"{phase} command {_id(before.id)} with its previous scope")
                        raise EnvironmentDiscoveryError(message, code="obligation_removed", global_owned=True)


REPAIR_OUTSIDE_CLOSURE = "repair_outside_closure"
REPAIR_TRIAGE_STALE = "repair_triage_stale"
REPAIR_CORRECTABLE = frozenset({REPAIR_OUTSIDE_CLOSURE})
_PHASES = ("init", "start", "health", "stop")


def _commands(manifest: EnvironmentManifest) -> dict[str, Command]:
    commands = {command.id: command for command in [*manifest.install, *manifest.checks]}
    for service in manifest.services:
        commands.update({command.id: command for command in (getattr(service, phase) for phase in _PHASES) if command is not None})
    return commands


def _service_shape(service: Any) -> dict[str, Any]:
    """A service's own declaration; its lifecycle commands are compared as command records."""
    phases = {phase: (command.id if (command := getattr(service, phase)) is not None else None) for phase in _PHASES}
    return {**service.model_dump(mode="json", exclude=set(_PHASES)), **phases}


def _grown(old: Sequence[str], new: Sequence[str], added: set[str]) -> bool:
    """Entries may be added, and only added records may be the additions; nothing established may leave."""
    return set(old) <= set(new) and set(new) - set(old) <= added


@dataclass(frozen=True)
class RecordSet:
    """The records a repair guard compares: a whole manifest, or the part one delta author owns."""

    commands: dict[str, Command] = field(default_factory=dict)
    services: list[Service] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    criterion_checks: dict[str, list[str]] = field(default_factory=dict)
    activation: Activation | None = None


def manifest_records(manifest: EnvironmentManifest) -> RecordSet:
    return RecordSet(_commands(manifest), list(manifest.services), list(manifest.requirements),
                     {key: list(value) for key, value in manifest.criterion_checks.items()}, manifest.activation)


def part_records(part: Mapping[str, Any]) -> RecordSet:
    """A delta part's records, parsed into the models the merge guard compares."""
    services = [Service.model_validate(item) for item in part.get('services') or []]
    commands = {command.id: command for command in (Command.model_validate(item) for item in
                                                     [*(part.get('install') or []), *(part.get('checks') or [])])}
    for service in services:
        commands.update({command.id: command for command in (getattr(service, phase) for phase in _PHASES)
                         if command is not None})
    activation = part.get('activation')
    return RecordSet(commands, services, [Requirement.model_validate(item) for item in part.get('requirements') or []],
                     {key: list(value) for key, value in (part.get('criterion_checks') or {}).items()},
                     Activation.model_validate(activation) if activation is not None else None)


def frozen_records(before: RecordSet, after: RecordSet, triage: RepairTriage,
                   aliases: Mapping[str, str] | None = None) -> set[str]:
    """Records of ``before`` outside the triage closure whose established content changed or vanished in ``after``.

    Growth stays allowed: a requirement's or criterion's check list may gain added checks, and activation may gain
    PATH entries and environment keys while keeping its existing ones. Only records ``before`` holds are judged.
    """
    aliases = aliases or {}
    closure = set(triage.closure_ids)
    added = set(after.commands) - set(before.commands)
    # A dropped command that an identical kept one replaces is not a frozen record that changed or vanished.
    frozen = {identifier for identifier, command in before.commands.items() if identifier not in closure and (
        (identifier not in after.commands and aliases.get(identifier) not in after.commands)
        or (identifier in after.commands
            and after.commands[identifier].model_dump(mode="json") != command.model_dump(mode="json")))}
    current_services = {service.id: service for service in after.services}
    for service in before.services:
        members = {c.id for c in (getattr(service, phase) for phase in _PHASES) if c is not None}
        if members & closure:
            continue
        if service.id not in current_services or _service_shape(current_services[service.id]) != _service_shape(service):
            frozen.add(service.id)
    current_requirements = {requirement.id: requirement for requirement in after.requirements}
    for requirement in before.requirements:
        if requirement.id in triage.requirement_ids:
            continue
        new = current_requirements.get(requirement.id)
        if (new is None or new.model_dump(mode="json", exclude={"checks"}) != requirement.model_dump(mode="json", exclude={"checks"})
                or not _grown([aliases.get(item, item) for item in requirement.checks], new.checks, added)):
            frozen.add(requirement.id)
    for criterion, identifiers in before.criterion_checks.items():
        if criterion in triage.criterion_ids:
            continue
        if criterion not in after.criterion_checks or not _grown([aliases.get(item, item) for item in identifiers],
                                                                 after.criterion_checks[criterion], added):
            frozen.add(criterion)
    if ACTIVATION not in closure and before.activation is not None:
        old, new_activation = before.activation, after.activation
        if (new_activation is None or new_activation.path[:len(old.path)] != old.path
                or any(new_activation.env.get(key) != value for key, value in old.env.items())):
            frozen.add(ACTIVATION)
    return frozen


def refuse_frozen(frozen: set[str]) -> None:
    if frozen:
        message = (f"repair may change only its failure closure; frozen records were changed or removed: "
                   f"{_ids(set(sorted(frozen)[:16]))}. Restore each listed record exactly as it was; a consequence "
                   "re-runs unchanged once its prerequisites pass. Add new records for any new obligation.")
        raise EnvironmentDiscoveryError(message, code=REPAIR_OUTSIDE_CLOSURE)


def guard_repair_diff(candidate: EnvironmentManifest, previous: EnvironmentManifest, triage: RepairTriage) -> None:
    """Every record outside the triage closure keeps its established content; added records are judged by review.

    Freezing is by the failure closure, not by execution: a record a preflight stopped before it ran is frozen too.
    Corrections name only safe ids, never model text.
    """
    if previous.digest() != triage.manifest_digest:
        message = "repair triage was computed for another manifest"
        raise EnvironmentDiscoveryError(message, code=REPAIR_TRIAGE_STALE)
    refuse_frozen(frozen_records(manifest_records(previous), manifest_records(candidate), triage,
                                 duplicate_aliases(previous, candidate)))
