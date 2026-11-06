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
from pydantic import ValidationError

from coordinare.services.environment_attempt import REPAIR_LIMITS, EnvironmentDiscoveryError
from coordinare.services.environment_repair_triage import ACTIVATION
from coordinare.services.environment_scope import preserve_scope, released_criteria

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

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
    ran = {result.id for result in report.results if result.exit_code is not None or result.duration_seconds > 0}
    if not ran:
        return None  # Trusted preflight stopped every project command before execution.
    required = [
        command
        for command in _required_execution_commands(manifest)
        if not report.scope or not command.scope or report.scope in command.scope
    ]
    if any(command.id not in by_id or not by_id[command.id].required for command in required):
        message = "execution history omits required commands"
        raise ValueError(message)
    return _ran(manifest, ran)


def _ran(manifest: EnvironmentManifest, ran: set[str]) -> EnvironmentManifest:
    """The executed manifest as far as it ran: only a command that executed (an exit code or a duration) is fixed.

    Dettmore e2e-46: ruby failed to install, so every c01 check stayed not_run, yet each was fixed with the wrong
    database port it exported. The reviewer named that port in all 8 coverage revisions and no repair could change it.
    A check that never ran has no result to protect; the obligation ledger still keeps it from being dropped, and the
    independent review judges its revision like any unexecuted check. A service stays fixed once any of its commands ran.
    """
    services = [service if any(command.id in ran for command in (service.init, service.start, service.health, service.stop)
                               if command is not None) else service.model_copy(update={"required": False})
                for service in manifest.services]
    return manifest.model_copy(update={"checks": [check for check in manifest.checks if check.id in ran],
                                       "services": services})


# Every execution of a fully used repair budget: the first and one per execution repair (was 3, below that budget).
MAX_REJECTED_EXECUTIONS = 1 + REPAIR_LIMITS["execution"]
# Dettmore e2e-47 journaled 0.8-1.1 MB per rejected execution; 4 MiB held only three.
_MAX_EXECUTION_HISTORY_BYTES = MAX_REJECTED_EXECUTIONS * 2 * 1024 * 1024


def _bounded_history(history: object) -> list[Any]:
    if (
        not isinstance(history, list)
        or len(history) > MAX_REJECTED_EXECUTIONS
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
    """Every command execution preservation fixes: the required checks that ran and the lifecycle commands of each
    required service that started, in each executed manifest (see _ran). Only their timeouts may grow."""
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


# Dettmore e2e-58: three required checks were wrong in themselves (an advisory audit required to find nothing,
# `ruby -e 'require "rspec"'` outside Bundler, a jq call on multi-line output). Once they ran they were fixed, so no
# repair could correct them, and the attempt spent its remaining coverage revisions until it held. A check that failed
# and never passed may be retired: kept exactly as it ran but no longer required, its criteria and requirements served
# by new checks the independent review judges. A check that ever passed stays fixed.
RETIRE_FAILED_CHECK = (
    'A check in retirable_check_ids failed in an execution and never passed. When its failure output shows the check '
    'itself is wrong (not the environment), retire it: keep it exactly as it ran but set required to false, remove it '
    'from criterion_checks and from every requirement, map each of its criteria and requirements to a new required '
    'check with a new id that verifies the same obligation and is no weaker, and make no required command depend on '
    'it. Never retire a check the environment failed: repair installation, activation or a service instead.')
RETIREMENT_REVIEW = (
    'A retired check (a check in retirable_check_ids the candidate keeps with required false) is a blocking finding '
    'unless its failure_output shows the check itself was wrong rather than the environment, and the new checks now '
    'mapped to its criteria verify the same obligation and are no weaker.')


def retirable_check_ids(history: object, *, source_commit: str) -> frozenset[str]:
    """Required checks that failed after running in some execution and passed in none: only these may be retired."""
    failed: set[str] = set()
    passed: set[str] = set()
    try:
        for entry in _bounded_history(history):
            if _executed_manifest(entry, source_commit) is None:
                continue
            checks = {check.id for check in EnvironmentManifest.model_validate(entry["manifest"]).checks}
            data = dict(entry["report"])
            data.pop("passed", None)
            for result in VerificationReport.model_validate(data).results:
                if result.id not in checks:
                    continue
                if result.status == "pass":
                    passed.add(result.id)
                elif result.required and result.status == "fail" and (
                        result.exit_code is not None or result.duration_seconds > 0):
                    failed.add(result.id)
    except (KeyError, TypeError, ValueError):
        message = "execution history is malformed or unbound"
        raise EnvironmentDiscoveryError(message, code="execution_history_invalid") from None
    return frozenset(failed - passed)


def retired_checks(manifest: EnvironmentManifest, retirable: Collection[str]) -> frozenset[str]:
    """The retirable checks this manifest keeps but no longer requires."""
    return frozenset(check.id for check in manifest.checks if check.id in retirable and not check.required)


def _retirement_errors(manifest: EnvironmentManifest, previous: Sequence[EnvironmentManifest],
                       retired: frozenset[str], fixed: Mapping[str, Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    checks = {check.id: check for check in manifest.checks}
    commands = [*manifest.install, *manifest.checks,
                *(command for service in manifest.services if service.required
                  for command in (service.init, service.start, service.health, service.stop) if command is not None)]
    for identifier in sorted(retired):
        record = fixed.get(identifier)
        if record is not None and ({**checks[identifier].model_dump(mode="json"), "required": True, "timeout_seconds": 0}
                                   != {**record, "timeout_seconds": 0}
                                   or checks[identifier].timeout_seconds < record["timeout_seconds"]):
            errors.append(f"{_id(identifier)} must keep exactly what it ran (only required may become false)")
        if dependents := {command.id for command in commands if command.required and identifier in command.depends_on}:
            errors.append(f"required {_ids(dependents)} still depend on {_id(identifier)}")
        if any(identifier in ids for ids in manifest.criterion_checks.values()) or any(
                identifier in requirement.checks for requirement in manifest.requirements):
            errors.append(f"{_id(identifier)} must leave criterion_checks and every requirement")
        for old in previous:
            errors.extend(f"criterion {_id(criterion)} needs a new required check in place of {_id(identifier)}"
                          for criterion, ids in old.criterion_checks.items()
                          if identifier in ids and not set(manifest.criterion_checks.get(criterion, [])) - set(ids))
            current = {requirement.id: requirement for requirement in manifest.requirements}
            errors.extend(f"requirement {_id(requirement.id)} needs a new check in place of {_id(identifier)}"
                          for requirement in old.requirements
                          if identifier in requirement.checks and (
                              requirement.id not in current
                              or not set(current[requirement.id].checks) - set(requirement.checks)))
    return sorted(set(errors))


def validate_retirements(manifest: EnvironmentManifest, previous: Sequence[EnvironmentManifest],
                         retirable: Collection[str], fixed: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """The checks this manifest retires, refusing a retirement that keeps any of its obligation unserved."""
    retired = retired_checks(manifest, retirable)
    if errors := _retirement_errors(manifest, previous, retired, fixed):
        message = f"a retired check must leave its obligations to new checks: {'; '.join(errors[:16])}"
        raise EnvironmentDiscoveryError(message, code="obligation_removed")
    return retired


def preservation_view(manifest: EnvironmentManifest, previous: EnvironmentManifest,
                      retired: Collection[str]) -> EnvironmentManifest:
    """The manifest as preservation judges it against ``previous``: each retired check still required and still
    mapped where ``previous`` mapped it. Its replacements were judged by validate_retirements; nothing else changes."""
    retired = set(retired)
    if not retired:
        return manifest
    checks = [check.model_copy(update={"required": True}) if check.id in retired else check for check in manifest.checks]
    mapping = {criterion: [*ids, *(item for item in previous.criterion_checks.get(criterion, [])
                                   if item in retired and item not in ids)]
               for criterion, ids in manifest.criterion_checks.items()}
    before = {requirement.id: requirement for requirement in previous.requirements}
    requirements = [requirement.model_copy(update={"checks": [*requirement.checks, *(
                        item for item in before[requirement.id].checks if item in retired and item not in requirement.checks)]})
                    if requirement.id in before else requirement for requirement in manifest.requirements]
    return manifest.model_copy(update={"checks": checks, "criterion_checks": mapping, "requirements": requirements})


def _restored(item: dict[str, Any], old: Command, restored: list[str], retirable: Collection[str] = ()) -> dict[str, Any]:
    """The executed record, keeping a longer timeout (the one change preservation allows), or the item if unchanged.
    A retirable check the item retires stays retired: only its content is put back."""
    try:
        current = Command.model_validate(item)
    except ValidationError:
        current = None
    retiring = old.id in retirable and current is not None and not current.required
    excluded = {"timeout_seconds", "required"} if retiring else {"timeout_seconds"}
    if current is not None and current.model_dump(exclude=excluded) == old.model_dump(exclude=excluded):
        return item
    record: dict[str, Any] = old.model_dump(mode="json")
    if retiring:
        record["required"] = False
    timeout = item.get("timeout_seconds")
    if type(timeout) in {int, float} and timeout > old.timeout_seconds:
        record["timeout_seconds"] = timeout
    restored.append(old.id)
    return record


def restore_execution_commands(data: dict[str, Any], executed: Sequence[EnvironmentManifest],
                               retirable: Collection[str] = ()) -> list[str]:
    """Put back every executed required check or service command a merged candidate changed; return their ids.

    Dettmore e2e-25: after the first execution, a check group added a dependency to an executed check and the global
    author rewrote the failed mock service's start and health commands. Each merge named all three and asked for them
    back, the authors changed them again, and the merge rounds ran out. A changed executed command can only be
    rejected, so restoring it is exactly the correction; a removed one is still reported by preservation.
    """
    restored: list[str] = []
    for previous in executed:
        frozen = {check.id: check for check in previous.checks if check.required}
        if isinstance(checks := data.get("checks"), list):
            data["checks"] = [_restored(item, frozen[item["id"]], restored, retirable)
                              if isinstance(item, dict) and item.get("id") in frozen else item for item in checks]
        services = {service.id: service for service in previous.services if service.required}
        for service in data.get("services") or []:
            if not isinstance(service, dict) or (old := services.get(service.get("id"))) is None:
                continue
            for phase in ("init", "start", "health", "stop"):
                command = getattr(old, phase)
                if command is not None and isinstance(service.get(phase), dict) and service[phase].get("id") == command.id:
                    service[phase] = _restored(service[phase], command, restored)
    return sorted(set(restored))


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
    executed = executed_manifests(history, source_commit=manifest.source_commit)
    retired = validate_retirements(manifest, executed, retirable_check_ids(history, source_commit=manifest.source_commit),
                                   fixed_commands(executed))
    for previous in executed:
        preserve_execution_commands(preservation_view(manifest, previous, retired), previous)


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


def preserve_obligations(manifest: EnvironmentManifest, previous: EnvironmentManifest,
                         retired: Collection[str] = ()) -> None:
    """A fresh judgment can add coverage, never waive a previously required obligation (a retired check's obligations
    are judged by validate_retirements, so it is judged here as still required)."""
    manifest = preservation_view(manifest, previous, retired)
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
