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
from typing import TYPE_CHECKING, Any, Self

from coordinare_service_inference.environment.models import (
    CoverageAssessmentProposal,
    EnvironmentManifestProposal,
    ShardChecks,
    ShardChecksEnvelope,
    validate_execution_graph,
)
from coordinare_service_inference.environment.scope import canonical
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from coordinare.services.environment_agent_pool import ShardOverflowError, run_pool
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
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
CHECK_AUTHOR_INSTRUCTIONS = (
    'You are a check author for one group of required source criteria in a divided environment manifest. The global '
    'environment author already declared installation, activation and services (global_part). For every criterion in '
    'assigned_criteria, write required, unscoped checks that prove its obligation and observable result in the clean '
    'consumer, and map it in criterion_checks to those checks or to global checks in global_part. Every new check id you '
    'write must start with check_id_prefix; keep every id in keep_check_ids unchanged, and you may map or depend on '
    'sibling_check_ids, which another author keeps. depends_on may name your checks, global checks, or service init, start '
    'and health ids. You cannot declare installation, services or activation. bound_inputs indexes verified repository '
    'files with the criteria citing each; read the files you need with read_files. Never write literal credentials. '
    'Return {"shard_checks":{"checks":[...],"criterion_checks":{...}}}. '
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


def merge_parts(global_data: Mapping[str, Any], parts: Sequence[ShardChecks]) -> dict[str, Any]:
    """Global declaration plus every part's checks and mappings; a collision is an error, never a choice."""
    merged = copy.deepcopy(dict(global_data))
    checks = list(merged.get('checks', []) or [])
    mapping: dict[str, list[str]] = {}
    seen = {check.get('id') for check in checks if isinstance(check, dict)}
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
    return merged


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

    async def author(self, group: CheckGroup, *, summary: Mapping[str, Any], probes: Sequence[Any]) -> ShardChecks:
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

    async def author_all(self, *, summary: Mapping[str, Any], probes: Sequence[Any],
                         groups: Sequence[CheckGroup] | None = None) -> dict[str, tuple[CheckGroup, ShardChecks]]:
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
                                  split=unsplittable, concurrency=self.concurrency)
        return {group.prefix: (group, part) for group, part in authored}

    def route_findings(self, ledger: FindingLedger) -> None:
        """Open coverage findings go to the check group whose ids they name; others stay with the global author."""
        routed: dict[str, list[dict[str, Any]]] = {}
        for record in ledger.records:
            if record.id not in ledger.open_ids():
                continue
            owner = finding_part(record.proposal.affected_ids, self.groups)
            routed.setdefault(owner, []).append(record.model_dump(mode='json'))
        # Each round's open findings replace the last round's; execution failures stay until the next execution.
        kept = {prefix: {key: value for key, value in entry.items() if key in EXECUTION_FEEDBACK_KEYS}
                for prefix, entry in self.feedback.items()}
        for prefix, records in routed.items():
            if prefix != GLOBAL_OWNER:
                kept[prefix] = {**kept.get(prefix, {}), 'open_findings': records}
        self.feedback = {prefix: entry for prefix, entry in kept.items() if entry}


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


def finding_part(affected_ids: Sequence[str], groups: Sequence[CheckGroup]) -> str:
    for identifier in affected_ids:
        for group in groups:
            if owns(group.prefix, identifier) or identifier in group.ids:
                return group.prefix
    return GLOBAL_OWNER


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
    """What a review part may name: the manifest's obligation ids and the ledger's retained finding ids."""

    obligations: Collection[str] = ()
    retained: Collection[str] = ()


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
    decisions = sorted((decision.model_dump(mode='json') for _owner, proposal in parts for decision in proposal.decisions),
                       key=lambda decision: order.get(decision['finding_id'], 0))
    findings = [finding.model_dump(mode='json') for _owner, proposal in parts for finding in proposal.findings]
    sources = sorted({path for _owner, proposal in parts for path in proposal.sources})
    summary = 'Divided executable review: ' + '; '.join(f'{owner} {proposal.verdict}' for owner, proposal in parts)
    return {'verdict': verdict, 'rationale': summary.encode()[:2000].decode(errors='ignore'), 'sources': sources,
            'findings': findings, 'decisions': decisions,
            'capability_unavailable': any(proposal.capability_unavailable for _owner, proposal in parts)}


def review_owners(ledger: FindingLedger, groups: Sequence[CheckGroup]) -> dict[str, list[str]]:
    owned: dict[str, list[str]] = {}
    for record in ledger.records:
        owned.setdefault(finding_part(record.proposal.affected_ids, groups), []).append(record.id)
    return owned


def review_capacities(ledger: FindingLedger, owners: Sequence[str]) -> dict[str, int]:
    return dict(zip(owners, finding_capacities(MAX_FINDINGS - len(ledger.records), len(owners)), strict=True))


def part_manifest_view(manifest: EnvironmentManifest, group: CheckGroup | None) -> dict[str, Any]:
    """The part under review: a group's criteria and mapped checks, or the global declaration."""
    data = manifest.model_dump(mode='json')
    if group is None:
        return {key: data.get(key) for key in ('requirements', 'install', 'activation', 'services', 'runtime',
                                                 'local_credentials', 'empty_reason')} | {
            'checks': [check for check in data['checks'] if not PREFIX.match(check['id'])]}
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
    'finding_part',
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
