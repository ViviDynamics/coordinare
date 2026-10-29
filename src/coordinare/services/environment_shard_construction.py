"""Divided executable construction and review for scopes too large for one author.

One global environment author, with probes, writes installation, activation, services, requirements and
environment-level checks. Parallel check authors each write the required checks and criterion mappings for a
deterministic group of criteria, under a daemon-assigned check-id prefix. The daemon merges the parts, and the merged
proposal passes the unchanged manifest admission. Review is divided the same way and combined into one assessment.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Self, cast

from coordinare_service_inference.environment.models import (
    Command,
    CoverageAssessmentProposal,
    EnvironmentManifestProposal,
    ShardChecks,
    ShardChecksEnvelope,
    content_key,
    validate_execution_graph,
)
from coordinare_service_inference.environment.scope import canonical
from coordinare_service_inference.environment.static_rules import (
    self_matching_process_pattern,
    validate_static_rules,
)
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from coordinare.services.environment_admission import GRAPH_GUIDANCE
from coordinare.services.environment_agent_pool import ShardOverflowError, run_pool
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_delta_schema import apply_group_delta
from coordinare.services.environment_discovery_prompts import SOURCE_NAVIGATION
from coordinare.services.environment_findings import MAX_FINDINGS
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_scope import _ResponseReferenceError, released_criteria
from coordinare.services.environment_shard_scope import (
    MIN_SPLIT,
    AgentContext,
    AgentTask,
    finding_capacities,
    run_agent,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from coordinare_service_inference.environment.findings import FindingLedger
    from coordinare_service_inference.environment.inventory import InventoryEntry
    from coordinare_service_inference.environment.models import EnvironmentManifest
    from coordinare_service_inference.environment.scope import ScopeContract, ScopeCriterion

    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_delta import DeltaPlan
    from coordinare.services.environment_discovery import Fetch

CHECK_AUTHOR_THRESHOLD = 25  # more than one check group's share (the approved plan's gate)
CHECK_GROUP_SIZE = 25
# A group's prefix, or a split remainder's (c00a-, c00ab-): every check-author id is reserved from the global author.
PREFIX = re.compile(r'^c[0-9]{2,3}[a-z]*-')


def owns(prefix: str, identifier: str) -> bool:
    """A group owns its own ids and those of every split remainder authored under it."""
    return re.match(re.escape(prefix[:-1]) + r'[a-z]*-', identifier) is not None


def base_prefix(prefix: str) -> str:
    return re.sub(r'[a-z]+-$', '-', prefix)
GLOBAL_OWNER = 'global'

GLOBAL_AUTHOR_INSTRUCTION = (
    'This scope is divided. You are the global environment author: declare requirements, installation, activation, '
    'services, runtime endpoints, local credentials and any environment-level checks (toolchain usability, service '
    'readiness). Leave criterion_checks empty: check authors map every required criterion to checks under their own '
    'id prefixes, and the daemon merges their checks with your manifest. Never use a check id that starts with c '
    'followed by digits, optional letters and a hyphen, or a scope criterion id (S followed by 16 hex characters) as '
    'any record id. Requirements may reference only your own checks. '
)
# A check verifies what installation persisted; it never rebuilds or re-fetches it, since clean verification is offline.
INSTALL_ARTIFACT_RULE = (
    'A check consumes an artifact that installation persisted (an image, tool, package or cache entry) by inspecting or '
    'running it; it never rebuilds that artifact from source or pulls it again, because clean verification has no network. '
)
CHECK_AUTHOR_INSTRUCTIONS = (
    'You are a check author for one group of required source criteria in a divided environment manifest. The global '
    'environment author already declared installation, activation and services (global_part). For every criterion in '
    'assigned_criteria, write required, unscoped checks that prove its obligation and observable result in the clean '
    'consumer, and map it in criterion_checks to those checks or to global checks in global_part. Every new check id you '
    'write must start with check_id_prefix; keep every id in keep_check_ids unchanged, and you may map or depend on '
    'sibling_check_ids, which another author keeps. depends_on may name your checks, global checks, or service init, start '
    'and health ids. You cannot declare installation, services or activation. bound_inputs indexes verified repository '
    'files with the criteria citing each; read the files you need with read_files. Never write literal credentials. '
    + INSTALL_ARTIFACT_RULE +
    'Return {"shard_checks":{"checks":[...],"criterion_checks":{...}}}. '
)
DELTA_CHECK_INSTRUCTION = (
    'This is a repair. A previous execution failed in the closure_checks, closure_criteria and untrusted_feedback shown, '
    'and only those checks may change. Return {"shard_checks":{"checks":[...],"criterion_checks":{...}}} listing ONLY '
    'the checks you change or add, and only the criterion_checks entries whose mapping you change. Omit every other check; '
    'they are kept unchanged. Keep every id in keep_check_ids, never rename a check, and add a new id only for a new '
    'obligation. Read the bound_inputs you need with read_files. Never write literal credentials. '
)
REVIEW_PART_PREAMBLE = (
    'You independently review ONE part of a divided executable manifest: for a check group, its assigned criteria and '
    'the checks that map them; for the global part, installation, activation, services, requirements and global checks. '
    'manifest holds the complete merged candidate for reference. required_decision_ids are the retained findings '
    'assigned to you; decide exactly those, and report at most findings_capacity new findings. '
)


class GlobalRules(BaseModel):
    """The global author's declaration leaves criterion mappings and check-author prefixes to check authors."""

    model_config = ConfigDict(extra='forbid')
    criterion_checks: dict[str, Any]
    check_ids: list[str]

    @model_validator(mode='after')
    def leaves_mapping_to_check_authors(self) -> Self:
        if self.criterion_checks:
            raise PydanticCustomError('global_part_invalid', 'criterion_checks belong to check authors; leave it empty')
        if any(PREFIX.match(identifier) for identifier in self.check_ids):
            raise PydanticCustomError('global_part_invalid', 'check ids of c, digits, optional letters and a hyphen are reserved')
        return self


def validate_global(data: dict[str, Any]) -> dict[str, Any]:
    """Full proposal validation, then the global author's own rules; both raise correctable validation errors.

    The global part never maps criteria (check authors map every one), so whatever it puts in criterion_checks is set
    aside here rather than corrected: v37's execution repair spent its last schema repairs on that field alone.
    """
    if isinstance(data, dict) and 'criterion_checks' in data:
        data['criterion_checks'] = {}
    proposal = EnvironmentManifestProposal.model_validate(data)
    try:
        validate_execution_graph(proposal.install, proposal.checks, proposal.services)
        validate_static_rules(proposal.install, proposal.activation)
    except PydanticCustomError as exc:
        raise ValidationError.from_exception_data('EnvironmentManifestProposal', [
            {'type': exc, 'loc': (), 'input': None}]) from exc
    GlobalRules.model_validate({'criterion_checks': dict(proposal.criterion_checks),
                                'check_ids': [check.id for check in proposal.checks]})
    return data


@dataclass(frozen=True)
class CheckGroup:
    prefix: str
    criteria: tuple[ScopeCriterion, ...]

    @property
    def ids(self) -> set[str]:
        return {criterion.id for criterion in self.criteria}


def divided(scope: ScopeContract) -> bool:
    """One author when it can hold the scope; divided only when required criteria exceed one author's share."""
    required = sum(1 for criterion in scope.definition.criteria if criterion.applicability == 'required')
    return bool(required > CHECK_AUTHOR_THRESHOLD)


def check_groups(scope: ScopeContract) -> list[CheckGroup]:
    """Deterministic groups by first cited source, so related obligations share an author and prefixes are stable."""
    required = sorted((c for c in scope.definition.criteria if c.applicability == 'required'),
                      key=lambda criterion: (sorted(criterion.sources)[0], criterion.id))
    return [CheckGroup(prefix=f'c{index:02d}-', criteria=tuple(required[offset:offset + CHECK_GROUP_SIZE]))
            for index, offset in enumerate(range(0, len(required), CHECK_GROUP_SIZE))]


def global_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    """What a check author may reference: global check ids, service lifecycle ids and activation."""
    services = []
    readiness: list[str] = []
    for service in data.get('services', []) or []:
        lifecycle = {phase: (service.get(phase) or {}).get('id') for phase in ('init', 'start', 'health', 'stop')}
        services.append({'id': service.get('id'), **lifecycle})
        readiness.extend(identifier for phase, identifier in lifecycle.items() if identifier and phase != 'stop')
    checks = [check for check in data.get('checks', []) or [] if isinstance(check, dict)]
    return {'services': services, 'readiness_ids': sorted(readiness), 'activation': data.get('activation', {}),
            'install_ids': [item.get('id') for item in data.get('install', []) or [] if isinstance(item, dict)],
            'checks': [{'id': check.get('id'), 'argv': check.get('argv'), 'required': check.get('required', True),
                        'scope': check.get('scope', [])} for check in checks],
            'local_credentials': [item.get('name') for item in data.get('local_credentials', []) or [] if isinstance(item, dict)],
            'runtime_endpoints': data.get('runtime_endpoints', [])}


@dataclass(frozen=True)
class Inheritance:
    """What a group's author starts from: its previous part, and for a split remainder the ids it keeps."""

    part: dict[str, Any] | None = None
    own: frozenset[str] = frozenset()
    # Previously required own ids the remainder must keep, and sibling ids it may depend on or (if usable) map to.
    required: frozenset[str] = frozenset()
    siblings: frozenset[str] = frozenset()
    usable_siblings: frozenset[str] = frozenset()
    # Per assigned criterion, the checks a reviewed manifest mapped to it: obligations a revision keeps.
    mapped: Mapping[str, frozenset[str]] = field(default_factory=dict)


def inherit(previous: Mapping[str, dict[str, Any]], group: CheckGroup, parent: CheckGroup | None) -> Inheritance:
    """An unsplit group keeps its previous part and its required checks. A split remainder keeps the previous checks
    its criteria owned.

    A check mapped by several criteria, or by none, belongs to the remainder holding the first such criterion in the
    parent's order, so exactly one remainder keeps each previous id and the others may map to it.
    """
    own_part = previous.get(group.prefix)
    base = previous.get(base_prefix(group.prefix))
    if base_prefix(group.prefix) == group.prefix or base is None or parent is None:
        # An unsplit group keeps its previously required checks, as the attempt's obligation ledger already does;
        # v22's c00 author dropped one in a revision and the merge could only report it afterwards.
        kept = [check for check in (own_part or {}).get('checks', []) if isinstance(check, dict) and 'id' in check]
        return Inheritance(own_part, frozenset(check['id'] for check in kept),
                           frozenset(check['id'] for check in kept if check.get('required', True)))
    mapping: dict[str, list[str]] = base.get('criterion_checks', {})
    declared = [check['id'] for check in base.get('checks', []) if isinstance(check, dict) and 'id' in check]
    owner: dict[str, str] = {}
    for criterion in parent.criteria:
        for identifier in mapping.get(criterion.id, []):
            if identifier in declared:
                owner.setdefault(identifier, criterion.id)
    for identifier in declared:
        owner.setdefault(identifier, parent.criteria[0].id)
    own = frozenset(identifier for identifier, criterion in owner.items() if criterion in group.ids)
    checks = {check['id']: check for check in base.get('checks', []) if isinstance(check, dict) and 'id' in check}
    # A redone remainder starts from its own last part; what it keeps is always derived from the parent's.
    part = own_part or {'checks': [check for identifier, check in checks.items() if identifier in own],
                        'criterion_checks': {criterion: ids for criterion, ids in mapping.items() if criterion in group.ids}}
    siblings = frozenset(declared) - own
    return Inheritance(part, own, frozenset(identifier for identifier in own if checks[identifier].get('required', True)),
                       siblings, frozenset(identifier for identifier in siblings if checks[identifier].get('required', True)
                                           and not checks[identifier].get('scope')))


def validate_group(data: dict[str, Any], *, group: CheckGroup, summary: Mapping[str, Any],
                   inherited: Inheritance | None = None) -> ShardChecks:
    """Own prefix, known dependencies, and exactly the assigned criteria mapped to required unscoped checks."""
    inherited = inherited or Inheritance()
    part = ShardChecksEnvelope.model_validate(data).shard_checks
    errors: list[dict[str, Any]] = []
    own = {check.id: check for check in part.checks}
    global_checks = {item['id']: item for item in summary['checks'] if item.get('id')}
    known = set(own) | set(global_checks) | set(summary['readiness_ids']) | inherited.siblings
    for index, check in enumerate(part.checks):
        if not check.id.startswith(group.prefix) and check.id not in inherited.own:
            errors.append({'code': 'check_prefix_invalid', 'location': ['shard_checks', 'checks', str(index), 'id'],
                           'guidance': f'Every check id must start with {group.prefix}.'})
        if self_matching_process_pattern(check):
            errors.append({'code': 'self_matching_process_pattern', 'location': ['shard_checks', 'checks', str(index), 'argv'],
                           'command_id': check.id, 'guidance': GRAPH_GUIDANCE['self_matching_process_pattern']})
        unknown = [dependency for dependency in check.depends_on if dependency not in known]
        if unknown:
            errors.append({'code': 'check_dependency_unknown', 'location': ['shard_checks', 'checks', str(index), 'depends_on'],
                           'unknown': unknown[:8],
                           'guidance': 'depends_on may name your checks, global checks or service init/start/health ids.'})
    mapped = set(part.criterion_checks)
    if mapped != group.ids:
        errors.append({'code': 'criterion_mapping_incomplete', 'location': ['shard_checks', 'criterion_checks'],
                       'missing': sorted(group.ids - mapped)[:32], 'unexpected': sorted(mapped - group.ids)[:32],
                       'guidance': 'Map exactly every assigned criterion id, and no other.'})

    missing = sorted(inherited.required - set(own))
    if missing:
        errors.append({'code': 'inherited_check_missing', 'location': ['shard_checks', 'checks'], 'missing': missing[:32],
                       'guidance': 'Keep every keep_check_ids check that was required; revise it in place, never drop it.'})
    unmapped = {criterion: sorted(ids - set(part.criterion_checks.get(criterion, [])))
                for criterion, ids in sorted(inherited.mapped.items()) if ids - set(part.criterion_checks.get(criterion, []))}
    if unmapped:
        errors.append({'code': 'inherited_mapping_missing', 'location': ['shard_checks', 'criterion_checks'],
                       'missing': dict(list(unmapped.items())[:32]),
                       'guidance': ('Each listed criterion must still map every listed check, as the reviewed manifest '
                                    'did: add checks to a criterion, never remove one.')})

    def usable(identifier: str) -> bool:
        if identifier in inherited.siblings:
            return identifier in inherited.usable_siblings
        if identifier in own:
            return own[identifier].required and not own[identifier].scope
        item = global_checks.get(identifier)
        return item is not None and bool(item.get('required', True)) and not item.get('scope')

    for criterion, identifiers in part.criterion_checks.items():
        if not identifiers or any(not usable(identifier) for identifier in identifiers):
            errors.append({'code': 'criterion_mapping_invalid', 'location': ['shard_checks', 'criterion_checks', criterion],
                           'guidance': 'Map each criterion to one or more required, unscoped checks that exist.'})
    if errors:
        raise _ResponseReferenceError(errors)
    return part


@dataclass(frozen=True)
class FoldPolicy:
    """What a fold must respect: executed checks, the obligation ledger's retained records, and every prior version
    of each check a repair's preservation judges the candidate against."""

    executed: Collection[str] = frozenset()
    retained: Mapping[str, Command] = field(default_factory=dict)
    prior: Mapping[str, Sequence[Command]] = field(default_factory=dict)


def merge_parts(global_data: Mapping[str, Any], parts: Sequence[ShardChecks], *,
                policy: FoldPolicy | None = None, folded: dict[str, str] | None = None, fold: bool = True,
                ) -> dict[str, Any]:
    """Global declaration plus every part's checks and mappings; a collision is an error, never a choice.

    A merge never removes nor rewrites (beyond a longer timeout) a ``policy.executed`` check. Each group copy it folds
    into an identical global check is reported in ``folded`` (dropped id to kept id), so the caller can carry a
    required id's obligation over to the kept command. A copy that changes its ``policy.retained`` record in scope or
    required status is not folded, so the obligation rule still corrects its own author. In a repair, a copy that
    ``policy.prior`` versions exist for folds only when the kept command still replaces every one of them (the alias
    repair preservation accepts against each prior manifest); otherwise every round would fold it and fail again.
    """
    merged = copy.deepcopy(dict(global_data))
    checks = list(merged.get('checks', []) or [])
    mapping: dict[str, list[str]] = {}
    seen = {check.get('id') for check in checks if isinstance(check, dict)}
    global_ids = {str(identifier) for identifier in seen}
    for part in parts:
        for check in part.checks:
            if check.id in seen:
                message = f'check id {check.id} is declared twice'
                raise EnvironmentDiscoveryError(message, code='executable_merge_collision')
            seen.add(check.id)
            checks.append(check.model_dump(mode='json'))
        for criterion, identifiers in part.criterion_checks.items():
            if criterion in mapping:
                message = f'criterion {criterion} is mapped by two parts'
                raise EnvironmentDiscoveryError(message, code='executable_merge_collision')
            mapping[criterion] = list(identifiers)
    merged['checks'] = checks
    merged['criterion_checks'] = mapping
    if not fold:
        return merged
    aliases = _merge_group_copies_of_global_checks(merged, global_ids, {str(identifier) for identifier in seen} - global_ids,
                                                   policy or FoldPolicy())
    if folded is not None:
        folded.update(aliases)
    return merged


def _merge_group_copies_of_global_checks(merged: dict[str, Any], global_ids: set[str], group_ids: set[str],
                                         policy: FoldPolicy) -> dict[str, str]:
    """A check group's identical copy of a global check becomes the global one, keeping every obligation of both.

    No check author sees another part's checks, so it cannot correct a copy (e2e-03 spent every merge round on
    check-maxio-health and c00-maxio-health). The global check takes the copy's dependencies, the larger timeout and
    required if either was, and the copy's references move to it. Every part may name a global check, so the merged
    manifest still splits into valid parts for a later repair. Left to the authors: a copy depending on a group's
    check, which the global part could not declare, or one whose dependencies would reach the global check (a cycle);
    copies between two groups, which neither group could reference; duplicates inside one part; an executed copy,
    which must survive execution preservation; and a fold that would change an executed check beyond a longer timeout,
    whether the global check it lands in or a check depending on the copy. A required (pinned) copy may fold: its
    obligation is the identical command, kept under the global id (e2e-07 deadlocked on two pinned copies).
    """
    graph = {str(record['id']): list(record.get('depends_on') or []) for record in _commands(merged)}
    authored = set(global_ids)
    global_ids = global_ids | _promote_shared_group_copies(merged, global_ids, group_ids, graph, policy)
    checks = [check for check in merged['checks'] if isinstance(check, dict)]
    globals_by_key: dict[str, tuple[dict[str, Any], Command]] = {}
    alias: dict[str, str] = {}
    for check in checks:
        try:
            command = Command.model_validate(check)
        except ValidationError:
            continue
        key = content_key(command)
        if command.id in global_ids:
            globals_by_key.setdefault(key, (check, command))
            continue
        if key not in globals_by_key or command.id not in group_ids:
            continue
        kept, model = globals_by_key[key]
        merged_model = _fold_result(command, model, group_ids, graph, policy)
        if merged_model is None:
            continue
        alias[command.id] = model.id
        model = merged_model
        kept.update(depends_on=model.depends_on, timeout_seconds=model.timeout_seconds, required=model.required)
        graph[model.id] = list(model.depends_on)
        globals_by_key[key] = (kept, model)
    alias.update(_drop_global_copies_of_executed_checks(merged, authored, group_ids, policy, alias))
    if not alias:
        return alias

    def remap(identifiers: list[str], *, exclude: str | None = None) -> list[str]:
        """A dropped copy's id becomes the kept one. Only the duplicate or self-reference that this creates is
        removed: an unrelated list stays byte for byte (an executed check may not change), and an existing
        self-reference stays for validation to reject."""
        if not alias.keys() & set(identifiers):
            return identifiers
        result: list[str] = []
        for identifier in identifiers:
            kept = alias.get(identifier, identifier)
            if identifier in alias and (kept in result or kept in identifiers or kept == exclude):
                continue
            result.append(kept)
        return result

    merged['checks'] = [check for check in checks if check['id'] not in alias]
    for check in merged['checks']:
        if check.get('depends_on'):
            check['depends_on'] = remap(check['depends_on'], exclude=check['id'])
    merged['criterion_checks'] = {criterion: remap(ids) for criterion, ids in merged['criterion_checks'].items()}
    for requirement in merged.get('requirements') or []:
        if isinstance(requirement, dict) and requirement.get('checks'):
            requirement['checks'] = remap(requirement['checks'])
    return alias


def _drop_global_copies_of_executed_checks(merged: dict[str, Any], authored: set[str], group_ids: set[str],
                                           policy: FoldPolicy, alias: Mapping[str, str]) -> dict[str, str]:
    """A new global check identical to an executed group check is dropped for it: the executed id must survive.

    Dettmore e2e-12: in a full repair the global author copied every group check it was shown, and the correction for
    reserved ids led it to rename all 25 copies. The fold into a global check refuses an executed copy, and the groups,
    told to keep their executed ids, wrote them again each round until the merge rounds ran out. The copy goes only
    when nothing could miss it: no requirement, command or criterion mapping names it (a mapping belongs to a group
    that may not name another group's check), it is new (never executed, retained or a prior version), it keeps no
    group copy, and the executed check takes it whole with at most a longer timeout. The last global check always
    stays: a global part needs one to validate on its own.
    """
    # A requirement, a dependent or a criterion mapping that names a global copy keeps it.
    named = {str(identifier) for requirement in merged.get('requirements') or [] if isinstance(requirement, dict)
             for identifier in requirement.get('checks') or []}
    named |= {str(dependency) for record in _commands(merged) for dependency in record.get('depends_on') or []}
    named |= {str(identifier) for identifiers in (merged.get('criterion_checks') or {}).values()
              for identifier in identifiers}
    remaining = sum(1 for check in merged['checks'] if isinstance(check, dict) and check.get('id') not in group_ids)
    executed: dict[str, tuple[dict[str, Any], Command]] = {}
    copies: list[Command] = []
    for check in (check for check in merged['checks'] if isinstance(check, dict)):
        try:
            command = Command.model_validate(check)
        except ValidationError:
            continue
        if command.id in group_ids and command.id in policy.executed and command.id not in alias:
            executed.setdefault(content_key(command), (check, command))
        elif (command.id in authored and command.id not in named and command.id not in alias.values()
              and command.id not in policy.executed and command.id not in policy.retained and command.id not in policy.prior):
            copies.append(command)
    dropped: dict[str, str] = {}
    for command in copies:
        found = executed.get(content_key(command))
        if found is None:
            continue
        kept, model = found
        if (not set(command.depends_on) <= set(model.depends_on) or (command.required and not model.required)
                or remaining - len(dropped) <= 1):
            continue
        kept['timeout_seconds'] = max(model.timeout_seconds, command.timeout_seconds)
        dropped[command.id] = model.id
    return dropped


def global_copies_dropped(folded: Mapping[str, str]) -> set[str]:
    """The global copies a merge dropped for executed group checks, from its reported folds."""
    return {dropped for dropped, kept in folded.items() if PREFIX.match(kept) and not PREFIX.match(dropped)}


_GROUP_BASE = re.compile(r'^(c[0-9]{2,3})[a-z]*-')
# A copy is shared, and promoted, only when distinct groups wrote it; one group's copies are its own correction.
MIN_SHARING_GROUPS = 2
MAX_COMMAND_ID_CHARS = 128


def _promote_shared_group_copies(merged: dict[str, Any], global_ids: set[str], group_ids: set[str],
                                 graph: dict[str, list[str]], policy: FoldPolicy) -> set[str]:
    """Identical checks written by two or more groups, with no identical global check, become one new global check.

    Dettmore e2e-10: c00 and c01 each wrote the full cucumber suite for their own criteria; neither group may name the
    other's check, so every merge round rejected the pair. The new check takes the shared name without its group
    prefix (cucumber-full), placed where the first copy was, and the ordinary fold then merges every copy into it with
    all its rules. It is made only when every copy can fold; otherwise the authors are corrected as before.
    """
    taken = {str(record.get('id')) for key in ('install', 'checks', 'requirements', 'services')
             for record in merged.get(key) or [] if isinstance(record, dict)} | set(graph) | set(merged.get('criterion_checks') or {})
    by_key: dict[str, list[Command]] = {}
    global_keys: set[str] = set()
    for check in merged['checks']:
        try:
            command = Command.model_validate(check)
        except ValidationError:
            continue
        if command.id in global_ids:
            global_keys.add(content_key(command))
        elif command.id in group_ids:
            by_key.setdefault(content_key(command), []).append(command)
    promoted: set[str] = set()
    for key, copies in by_key.items():
        owners = {match[1] for command in copies if (match := _GROUP_BASE.match(command.id))}
        if key in global_keys or len(owners) < MIN_SHARING_GROUPS:
            continue
        identifier = _promoted_id(copies[0].id, taken)
        if identifier is None:
            continue
        model: Command | None = copies[0].model_copy(update={'id': identifier, 'depends_on': [], 'timeout_seconds': 1.0,
                                                              'required': False})
        graph[identifier] = []
        for command in copies:
            model = _fold_result(command, model, group_ids, graph, policy) if model is not None else None
        if model is None:
            del graph[identifier]
            continue
        # The ordinary fold merges every copy into the new check, so it starts with no obligations of its own.
        start = Command.model_validate({**copies[0].model_dump(mode='json'), 'id': identifier, 'depends_on': [],
                                        'timeout_seconds': 1.0, 'required': False})
        position = next(index for index, check in enumerate(merged['checks'])
                        if isinstance(check, dict) and check.get('id') == copies[0].id)
        merged['checks'].insert(position, start.model_dump(mode='json'))
        taken.add(identifier)
        promoted.add(identifier)
    return promoted


# The daemon's scope criterion id shape; a record may never take one, mapped or not.
_CRITERION_ID = re.compile(r'S[0-9a-f]{16}')
MAX_PROMOTED_CANDIDATES = 100


def _promoted_id(source: str, taken: set[str]) -> str | None:
    """A valid command id for a promoted check: the copy's name without any group prefix, made unique, never shaped
    like a group check or a scope criterion, and within the id length; None when no candidate is valid."""
    name = source
    while (match := _GROUP_BASE.match(name)) is not None:
        name = name[match.end():]
    name = name or 'shared-check'
    for index in range(MAX_PROMOTED_CANDIDATES):
        suffix = f'-shared-{index}' if index else ''
        candidate = name[:MAX_COMMAND_ID_CHARS - len(suffix)] + suffix
        if candidate in taken or _GROUP_BASE.match(candidate) or _CRITERION_ID.fullmatch(candidate):
            continue
        try:
            Command.model_validate({'id': candidate, 'argv': ['true']})
        except ValidationError:
            continue
        return candidate
    return None


def _fold_result(command: Command, model: Command, group_ids: set[str], graph: Mapping[str, list[str]],
                 policy: FoldPolicy) -> Command | None:
    """The global check ``model`` after absorbing the identical group ``command``, or None when the fold is unsafe."""
    kept_before = policy.retained.get(command.id)
    added = [dependency for dependency in command.depends_on if dependency not in model.depends_on]
    unsafe = (command.id in policy.executed
              # It would change its retained obligation record's scope or required status.
              or (kept_before is not None
                  and (kept_before.scope != command.scope or (kept_before.required and not command.required)))
              # The global part could not declare a group's check, and a cycle would close.
              or any(dependency in group_ids or _reaches(graph, dependency, model.id) for dependency in added)
              # An executed check may change beyond a longer timeout neither where it lands nor where it is named.
              or (model.id in policy.executed and bool(added or (command.required and not model.required)))
              or any(command.id in graph.get(identifier, []) for identifier in policy.executed))
    if unsafe:
        return None
    merged = model.model_copy(update={'depends_on': [*model.depends_on, *added],
                                      'timeout_seconds': max(model.timeout_seconds, command.timeout_seconds),
                                      'required': model.required or command.required})
    key = content_key(command)
    if any(not (content_key(before) == key and (merged.required or not before.required)
                and set(before.depends_on) <= set(merged.depends_on) and merged.timeout_seconds >= before.timeout_seconds)
           for before in policy.prior.get(command.id, ())):
        return None
    return merged


def _commands(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every install, check and service lifecycle command record with an id, as raw data."""
    services = [service.get(phase) for service in data.get('services') or [] if isinstance(service, dict)
                for phase in ('init', 'start', 'health', 'stop')]
    return [record for record in [*(data.get('install') or []), *(data.get('checks') or []), *services]
            if isinstance(record, dict) and record.get('id')]


def _reaches(graph: Mapping[str, list[str]], start: str, target: str) -> bool:
    """Whether ``target`` is ``start`` or one of its transitive dependencies."""
    pending, visited = [start], set()
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node not in visited:
            visited.add(node)
            pending.extend(graph.get(node, []))
    return False


def parts_from_manifest(manifest: EnvironmentManifest | None, groups: Sequence[CheckGroup]) -> dict[str, dict[str, Any]]:
    """A previous manifest's checks and mappings per group, so repairs start from what was executed."""
    if manifest is None:
        return {}
    previous: dict[str, dict[str, Any]] = {}
    for group in groups:
        checks = [check.model_dump(mode='json') for check in manifest.checks if owns(group.prefix, check.id)]
        mapping = {criterion: ids for criterion, ids in manifest.criterion_checks.items() if criterion in group.ids}
        if checks or mapping:
            previous[group.prefix] = {'checks': checks, 'criterion_checks': mapping}
    return previous


@dataclass
class DividedConstruction:
    """Check authors for one attempt, sharing the attempt budget and the global author's discovery inspection."""

    context: AgentContext
    entries: Mapping[str, InventoryEntry] | Sequence[str]
    fetch: Fetch
    scope: ScopeContract
    concurrency: int
    feedback: dict[str, Any] = field(default_factory=dict)
    previous: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The last reviewed manifest's parts: unlike ``previous``, never replaced by a part the merge rejected.
    obligations: dict[str, dict[str, Any]] = field(default_factory=dict)
    group_inputs: dict[str, str] = field(default_factory=dict)
    # The global author's last admitted part: the base of its coverage revisions, never the merged manifest.
    global_part: dict[str, Any] | None = None
    # Delta repair: the failed accepted manifest's closure. A touched group is authored as changed and added checks.
    delta: DeltaPlan | None = None
    carrier_process_policy: dict[str, Any] | None = None

    @property
    def groups(self) -> list[CheckGroup]:
        return check_groups(self.scope)

    def _citations(self, group: CheckGroup) -> dict[str, str]:
        cited = {path for criterion in group.criteria for path in criterion.sources}
        return {path: digest for path, digest in self.scope.all_inputs.items() if path in cited}

    def _kept_mappings(self, group: CheckGroup) -> dict[str, frozenset[str]]:
        reviewed = self.obligations.get(group.prefix) or self.obligations.get(base_prefix(group.prefix))
        if not reviewed:
            return {}
        released = released_criteria(self.scope.finding_ledger)
        return {criterion: frozenset(ids) for criterion, ids in (reviewed.get('criterion_checks') or {}).items()
                if criterion in group.ids and criterion not in released and ids}

    async def author_delta(self, group: CheckGroup, *, summary: Mapping[str, Any], probes: Sequence[Any]) -> ShardChecks:
        """A touched group's author sees only its closure: changed and added checks, returned as a patch and applied to
        the failed accepted part. The saved patch is keyed by the triage digest, so a restart reuses it."""
        plan = cast('DeltaPlan', self.delta)
        inspection = SourceInspection(self.entries, self.fetch, self.context.limits, self.context.budget)
        inspection.name = 'executable_checks'
        assigned = [criterion for criterion in group.criteria if criterion.id in plan.triage.criterion_ids]
        cited_by: dict[str, list[str]] = {}
        for criterion in assigned:
            for path in criterion.sources:
                cited_by.setdefault(path, []).append(criterion.id)
        index = await inspection.bind_verified({path: digest for path, digest in self._citations(group).items()
                                                if path in cited_by}, phase='construction', cited_by=cited_by)
        parent = next((candidate for candidate in self.groups if candidate.prefix == base_prefix(group.prefix)), None)
        inherited = replace(inherit(self.previous, group, parent), mapped=self._kept_mappings(group))
        brief = canonical({
            'check_id_prefix': group.prefix, 'delta_instruction': DELTA_CHECK_INSTRUCTION,
            **({'carrier_process_policy': self.carrier_process_policy} if self.carrier_process_policy is not None else {}),
            'closure_checks': plan.closure_checks(group), 'closure_criteria': plan.closure_criteria(frozenset(group.ids)),
            'assigned_criteria': [criterion.model_dump(mode='json') for criterion in assigned],
            'global_part': dict(summary), 'probe_evidence': list(probes), 'bound_inputs': index,
            'untrusted_feedback': self.feedback.get(group.prefix, self.feedback.get(base_prefix(group.prefix)))}).decode()
        captured: dict[str, Any] = {}

        def validate(reply: dict[str, Any]) -> ShardChecks:
            patch = ShardChecksEnvelope.model_validate(reply).shard_checks.model_dump(mode='json')
            captured['patch'] = patch
            applied = {'shard_checks': apply_group_delta(plan.base_group(group), patch)}
            admitted = validate_group(applied, group=group, summary=summary, inherited=inherited)
            plan.guard_part(plan.base_group(group) or {}, applied['shard_checks'])
            return admitted

        key = delta_part_key(plan.digest, self.carrier_process_policy)
        saved = saved_part(self.context.budget, 'delta', group.prefix, key)
        if saved is not None:
            await inspection.bind_verified(saved['inputs'], phase='construction')
            reused = validate(saved['data'])
            self.group_inputs.update(inspection.inputs)
            return reused
        messages = [{'role': 'system', 'content': SOURCE_NAVIGATION + CHECK_AUTHOR_INSTRUCTIONS},
                    {'role': 'user', 'content': brief}]
        part: ShardChecks = await run_agent(self.context, inspection, messages, AgentTask('shard_checks', validate))
        self.group_inputs.update(inspection.inputs)
        save_part(self.context.budget, 'delta', group.prefix, key, data={'shard_checks': captured['patch']},
                  inputs=dict(inspection.inputs))
        await self.context.budget.flush()
        return part

    async def author(self, group: CheckGroup, *, summary: Mapping[str, Any], probes: Sequence[Any]) -> ShardChecks:
        if self.delta is not None and self.delta.group_touched(group):
            return await self.author_delta(group, summary=summary, probes=probes)
        inspection = SourceInspection(self.entries, self.fetch, self.context.limits, self.context.budget)
        inspection.name = 'executable_checks'
        cited_by: dict[str, list[str]] = {}
        for criterion in group.criteria:
            for path in criterion.sources:
                cited_by.setdefault(path, []).append(criterion.id)
        index = await inspection.bind_verified(self._citations(group), phase='construction', cited_by=cited_by)
        parent = next((candidate for candidate in self.groups if candidate.prefix == base_prefix(group.prefix)), None)
        inherited = replace(inherit(self.previous, group, parent), mapped=self._kept_mappings(group))
        brief = canonical({
            'check_id_prefix': group.prefix,
            **({'carrier_process_policy': self.carrier_process_policy} if self.carrier_process_policy is not None else {}),
            'assigned_criteria': [criterion.model_dump(mode='json') for criterion in group.criteria],
            'global_part': dict(summary), 'probe_evidence': list(probes), 'bound_inputs': index,
            'previous_part': inherited.part, 'keep_check_ids': sorted(inherited.own),
            'sibling_check_ids': sorted(inherited.siblings),
            'untrusted_feedback': self.feedback.get(group.prefix, self.feedback.get(base_prefix(group.prefix)))}).decode()
        validate = partial(validate_group, group=group, summary=summary, inherited=inherited)
        key = hashlib.sha256(brief.encode()).hexdigest()
        saved = saved_part(self.context.budget, 'groups', group.prefix, key)
        if saved is not None:
            # A restart reuses the part its author already returned for this exact brief; inputs are verified again.
            await inspection.bind_verified(saved['inputs'], phase='construction')
            reused = validate(saved['data'])
            self.group_inputs.update(inspection.inputs)
            return reused
        messages = [{'role': 'system', 'content': SOURCE_NAVIGATION + CHECK_AUTHOR_INSTRUCTIONS},
                    {'role': 'user', 'content': brief}]
        part: ShardChecks = await run_agent(self.context, inspection, messages, AgentTask('shard_checks', validate))
        self.group_inputs.update(inspection.inputs)
        save_part(self.context.budget, 'groups', group.prefix, key,
                  data={'shard_checks': part.model_dump(mode='json')}, inputs=dict(inspection.inputs))
        await self.context.budget.flush()
        return part

    def admits(self, group: CheckGroup, part: ShardChecks, *, summary: Mapping[str, Any]) -> bool:
        """Whether a part authored from an earlier global summary passes the same admission under the final one."""
        parent = next((candidate for candidate in self.groups if candidate.prefix == base_prefix(group.prefix)), None)
        inherited = replace(inherit(self.previous, group, parent), mapped=self._kept_mappings(group))
        try:
            validate_group({'shard_checks': part.model_dump(mode='json')}, group=group, summary=summary, inherited=inherited)
        except _ResponseReferenceError:
            return False
        return True

    async def author_all(self, *, summary: Mapping[str, Any], probes: Sequence[Any],
                         groups: Sequence[CheckGroup] | None = None,
                         concurrency: int | None = None) -> dict[str, tuple[CheckGroup, ShardChecks]]:
        """Author every group (or only the named ones); keyed by the prefix each part was authored under."""
        async def one(group: CheckGroup) -> tuple[CheckGroup, ShardChecks]:
            return group, await self.author(group, summary=summary, probes=probes)

        def unsplittable(group: CheckGroup, _overflow: ShardOverflowError) -> list[CheckGroup]:
            if len(group.criteria) < MIN_SPLIT:
                return [group]
            # A group too large for one conversation splits under suffixed prefixes; ids stay unambiguous.
            middle = len(group.criteria) // 2
            return [CheckGroup(prefix=f'{group.prefix[:-1]}{suffix}-', criteria=part)
                    for suffix, part in (('a', group.criteria[:middle]), ('b', group.criteria[middle:]))]

        authored = await run_pool(list(self.groups if groups is None else groups), one, budget=self.context.budget,
                                  split=unsplittable, concurrency=self.concurrency if concurrency is None else concurrency)
        return {group.prefix: (group, part) for group, part in authored}

    def route_findings(self, ledger: FindingLedger) -> None:
        """Open coverage findings go to the check group whose ids they name; others stay with the global author."""
        routed: dict[str, list[dict[str, Any]]] = {}
        for record in ledger.records:
            if record.id not in ledger.open_ids():
                continue
            for owner in finding_parts(record.proposal.affected_ids, self.groups):
                routed.setdefault(owner, []).append(record.model_dump(mode='json'))
        # Each round's open findings replace the last round's; execution failures stay until the next execution.
        kept = {prefix: {key: value for key, value in entry.items() if key in EXECUTION_FEEDBACK_KEYS}
                for prefix, entry in self.feedback.items()}
        for prefix, records in routed.items():
            if prefix != GLOBAL_OWNER:
                kept[prefix] = {**kept.get(prefix, {}), 'open_findings': records}
        self.feedback = {prefix: entry for prefix, entry in kept.items() if entry}


# Bumped whenever what a delta author is shown changes, so a part saved under an earlier brief is never replayed:
# 2 = repair feedback lists failures first and per owner (e2e-06's authors saw only consequences).
DELTA_BRIEF_VERSION = 2


def delta_part_key(triage_digest: str, process_policy: Mapping[str, Any] | None) -> str:
    """Keep native delta keys stable; private parts also depend on the delivered carrier policy and brief version."""
    if process_policy is None:
        return triage_digest
    return hashlib.sha256(canonical({'triage_digest': triage_digest, 'carrier_process_policy': process_policy,
                                     'brief_version': DELTA_BRIEF_VERSION})).hexdigest()


def saved_part(budget: AttemptBudget, kind: str, name: str, key: str) -> dict[str, Any] | None:
    """A persisted part, only when its author was given exactly this brief."""
    saved = ((budget.executable_parts or {}).get(kind) or {}).get(name)
    return saved if isinstance(saved, dict) and saved.get('key') == key else None


def save_part(budget: AttemptBudget, kind: str, name: str, key: str, **record: Any) -> None:
    parts = dict(budget.executable_parts or {})
    parts[kind] = {**(parts.get(kind) or {}), name: {'key': key, **record}}
    budget.executable_parts = parts


EXECUTION_FEEDBACK_KEYS = frozenset({'untrusted_execution_failures', 'rejected_manifest_digest'})


def execution_feedback_by_group(feedback: Mapping[str, Any], groups: Sequence[CheckGroup]) -> dict[str, Any]:
    """A failed check reaches the group that owns it; installation and service failures stay with the global author."""
    results = feedback.get('results') or []
    routed: dict[str, Any] = {}
    for group in groups:
        failed = [result for result in results if isinstance(result, dict) and isinstance(result.get('id'), str)
                  and owns(group.prefix, result['id']) and result.get('status') != 'pass']
        if failed:
            routed[group.prefix] = {'untrusted_execution_failures': failed,
                                    'rejected_manifest_digest': feedback.get('rejected_manifest_digest')}
    return routed


def finding_parts(affected_ids: Sequence[str], groups: Sequence[CheckGroup]) -> list[str]:
    """Every part whose records a finding names: each check group owning a named id, and the global part for an id no
    group owns. A finding about two groups reaches both, so neither keeps a part the finding still concerns."""
    parts: list[str] = []
    for identifier in affected_ids:
        owner = next((group.prefix for group in groups if owns(group.prefix, identifier) or identifier in group.ids), GLOBAL_OWNER)
        if owner not in parts:
            parts.append(owner)
    return parts or [GLOBAL_OWNER]


def manifest_digest(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(dict(data))).hexdigest()


# ----- divided review -----

def _finding_reference_errors(proposal: CoverageAssessmentProposal, *, inspection: SourceInspection,
                              obligation_ids: Collection[str]) -> list[dict[str, Any]]:
    """The full assessment's finding rules, applied per part so a reviewer can still correct them."""
    errors: list[dict[str, Any]] = []
    known = set(obligation_ids)
    for index, finding in enumerate(proposal.findings):
        if known and not set(finding.affected_ids) <= known:
            errors.append({'code': 'review_findings_invalid', 'location': ['assessment', 'findings', str(index), 'affected_ids'],
                           'obligation_ids': sorted(known)[:300],
                           'guidance': 'affected_ids must name manifest obligations (requirement, install, check or '
                                       'service ids) from obligation_ids, never scope criterion ids. Name the check '
                                       'or requirement that should cover a criterion, or leave affected_ids empty.'})
        if not set(finding.sources) <= inspection.inputs.keys():
            errors.append({'code': 'review_citations_invalid', 'location': ['assessment', 'findings', str(index), 'sources'],
                           'guidance': 'Cite only files indexed in mandatory_evidence or read with tools.'})
    return errors


def _decision_errors(proposal: CoverageAssessmentProposal, *, inspection: SourceInspection,
                     retained_ids: Collection[str]) -> list[dict[str, Any]]:
    """The combined review's decision rules, per part: v33 held when a part's duplicate decision stayed open and only
    the combined assessment, which no reviewer can correct, rejected it."""
    errors: list[dict[str, Any]] = []
    retained = set(retained_ids)
    for index, decision in enumerate(proposal.decisions):
        location = ['assessment', 'decisions', str(index)]
        if decision.duplicate_of is not None and decision.status != 'resolved':
            errors.append({'code': 'review_findings_invalid', 'location': [*location, 'status'],
                           'guidance': 'A duplicate entry must use status resolved; its original finding remains '
                                       'independently blocking.'})
        if decision.duplicate_of is not None and (decision.duplicate_of not in retained
                                                  or decision.duplicate_of == decision.finding_id):
            errors.append({'code': 'review_findings_invalid', 'location': [*location, 'duplicate_of'],
                           'guidance': 'duplicate_of must name another retained finding ID, never itself or a new finding.'})
        if not set(decision.sources) <= inspection.inputs.keys():
            errors.append({'code': 'review_citations_invalid', 'location': [*location, 'sources'],
                           'guidance': 'Cite only files indexed in mandatory_evidence or read with tools.'})
    return errors


@dataclass(frozen=True)
class ReviewIds:
    """What a review part may name: the manifest's obligation ids, the ledger's retained finding ids, and, for a repair, the
    criteria whose part needs one ruling each (``rulings`` is None when the part is not a repair's)."""

    obligations: Collection[str] = ()
    retained: Collection[str] = ()
    rulings: Sequence[str] | None = None


NO_REVIEW_IDS = ReviewIds()


def validate_review_part(data: dict[str, Any], *, inspection: SourceInspection, owned: Sequence[str],
                         capacity: int, known: ReviewIds = NO_REVIEW_IDS) -> tuple[CoverageAssessmentProposal, dict[str, str]]:
    proposal = CoverageAssessmentProposal.model_validate(data.get('assessment') if isinstance(data, dict) else data)
    errors: list[dict[str, Any]] = []
    decided = [decision.finding_id for decision in proposal.decisions]
    if len(decided) != len(set(decided)) or set(decided) != set(owned):
        errors.append({'code': 'review_findings_invalid', 'location': ['assessment', 'decisions'],
                       'required_decision_ids': list(owned),
                       'guidance': 'Decide exactly every required_decision_ids entry and no others.'})
    if known.rulings is not None:
        ruled = [ruling.criterion_id for ruling in proposal.criterion_rulings]
        if len(ruled) != len(set(ruled)) or set(ruled) != set(known.rulings):
            errors.append({'code': 'review_rulings_invalid', 'location': ['assessment', 'criterion_rulings'],
                           'required_criterion_ids': list(known.rulings),
                           'guidance': 'Give exactly one criterion_rulings entry per required_criterion_ids id and no others.'})
    if len(proposal.findings) > capacity:
        errors.append({'code': 'review_findings_limit', 'location': ['assessment', 'findings'], 'findings_capacity': capacity,
                       'guidance': 'Report at most findings_capacity new findings; return indeterminate if more must block.'})
    unread = [path for path in proposal.sources if path not in inspection.inputs]
    if unread:
        errors.append({'code': 'review_citations_invalid', 'location': ['assessment', 'sources'],
                       'guidance': 'Cite only files indexed in mandatory_evidence or read with tools.'})
    errors.extend(_finding_reference_errors(proposal, inspection=inspection, obligation_ids=known.obligations))
    errors.extend(_decision_errors(proposal, inspection=inspection, retained_ids=known.retained))
    opened = any(decision.status == 'open' for decision in proposal.decisions)
    if (proposal.verdict == 'accept' and (proposal.findings or opened)) or (
            proposal.verdict == 'revise' and not proposal.findings and not opened):
        errors.append({'code': 'review_findings_invalid', 'location': ['assessment', 'verdict'],
                       'guidance': 'accept requires no new or open findings; revise requires at least one.'})
    if errors:
        raise _ResponseReferenceError(errors)
    return proposal, dict(inspection.inputs)


def combine_review(parts: Sequence[tuple[str, CoverageAssessmentProposal]], ledger: FindingLedger) -> dict[str, Any]:
    """One assessment: accept only when every part accepts; any capability gap or indeterminate part carries."""
    verdicts = [proposal.verdict for _owner, proposal in parts]
    verdict = 'indeterminate' if 'indeterminate' in verdicts else 'accept' if set(verdicts) == {'accept'} else 'revise'
    order = {record.id: index for index, record in enumerate(ledger.records)}
    # A finding several parts decide is open while any of them keeps it open; otherwise its first resolution stands.
    chosen: dict[str, dict[str, Any]] = {}
    for _owner, proposal in parts:
        for decision in proposal.decisions:
            data = decision.model_dump(mode='json')
            if data['finding_id'] not in chosen or (data['status'] == 'open' and chosen[data['finding_id']]['status'] != 'open'):
                chosen[data['finding_id']] = data
    decisions = sorted(chosen.values(), key=lambda decision: order.get(decision['finding_id'], 0))
    findings = [finding.model_dump(mode='json') for _owner, proposal in parts for finding in proposal.findings]
    sources = sorted({path for _owner, proposal in parts for path in proposal.sources})
    summary = 'Divided executable review: ' + '; '.join(f'{owner} {proposal.verdict}' for owner, proposal in parts)
    return {'verdict': verdict, 'rationale': summary.encode()[:2000].decode(errors='ignore'), 'sources': sources,
            'findings': findings, 'decisions': decisions,
            'capability_unavailable': any(proposal.capability_unavailable for _owner, proposal in parts)}


def review_owners(ledger: FindingLedger, groups: Sequence[CheckGroup]) -> dict[str, list[str]]:
    owned: dict[str, list[str]] = {}
    for record in ledger.records:
        for owner in finding_parts(record.proposal.affected_ids, groups):
            owned.setdefault(owner, []).append(record.id)
    return owned


def review_capacities(ledger: FindingLedger, owners: Sequence[str]) -> dict[str, int]:
    return dict(zip(owners, finding_capacities(MAX_FINDINGS - len(ledger.records), len(owners)), strict=True))


def part_manifest_view(manifest: EnvironmentManifest, group: CheckGroup | None,
                       *, groups: Sequence[CheckGroup] = ()) -> dict[str, Any]:
    """A group's checks, or the global declaration and checks no group covers in full."""
    data = manifest.model_dump(mode='json')
    if group is None:
        covered = {identifier for owner in groups for criterion in owner.ids
                   for identifier in manifest.criterion_checks.get(criterion, [])}
        covered.update(check['id'] for check in data['checks'] if any(owns(owner.prefix, check['id']) for owner in groups))
        return {key: data.get(key) for key in ('requirements', 'install', 'activation', 'services', 'runtime',
                                                 'local_credentials', 'empty_reason')} | {
            'checks': [check for check in data['checks'] if not PREFIX.match(check['id']) or check['id'] not in covered]}
    mapped = {identifier for criterion in group.ids for identifier in manifest.criterion_checks.get(criterion, [])}
    return {'criterion_checks': {criterion: manifest.criterion_checks.get(criterion, []) for criterion in sorted(group.ids)},
            'checks': [check for check in data['checks'] if check['id'] in mapped or owns(group.prefix, check['id'])],
            'criteria': [criterion.model_dump(mode='json') for criterion in group.criteria]}


__all__ = [
    'CHECK_AUTHOR_THRESHOLD',
    'CHECK_GROUP_SIZE',
    'GLOBAL_AUTHOR_INSTRUCTION',
    'GLOBAL_OWNER',
    'REVIEW_PART_PREAMBLE',
    'CheckGroup',
    'DividedConstruction',
    'GlobalRules',
    'Inheritance',
    'ReviewIds',
    'base_prefix',
    'check_groups',
    'combine_review',
    'divided',
    'execution_feedback_by_group',
    'finding_parts',
    'global_summary',
    'inherit',
    'manifest_digest',
    'merge_parts',
    'owns',
    'part_manifest_view',
    'parts_from_manifest',
    'review_capacities',
    'review_owners',
    'save_part',
    'saved_part',
    'validate_global',
    'validate_group',
    'validate_review_part',
]
