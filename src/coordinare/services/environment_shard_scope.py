"""Sharded source scope: parallel shard authors and independent shard reviewers feeding one definition and one ledger.

Each agent conversation holds only its own share of the pinned repository. The daemon merges shard
summaries into one ScopeDefinition and shard judgments into one review, so the decision protocol,
finding ledger, journal and acceptance gates are exactly those of the single-conversation path.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Unpack

from coordinare_service_inference.environment.findings import FindingLedger
from coordinare_service_inference.environment.scope import (
    MAX_SCOPE_CRITERIA,
    ScopeAssessmentEnvelope,
    ScopeAssessmentProposal,
    ScopeCriterion,
    ScopeCriterionProposal,
    ScopeDefinition,
    ShardSummary,
    ShardSummaryEnvelope,
    canonical,
    criterion_id,
    normalize_source_assessment,
)
from coordinare_service_inference.environment.scope_revision import (
    ScopeRevisionEnvelope,
    apply_scope_revision,
)
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from coordinare.services.environment_admission import (
    _checked_inventory,
    _decode_reply,
    _validation_feedback,
    explain_rejection,
)
from coordinare.services.environment_agent_pool import ShardOverflowError, run_pool
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery_prompts import SOURCE_NAVIGATION
from coordinare.services.environment_evidence import (
    check_evidence_context,
    prepare_mandatory_evidence,
)
from coordinare.services.environment_findings import MAX_FINDINGS, FindingValidationError
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_response_schema import (
    constrain_source_criteria,
    invoke_chat,
    response_schema,
)
from coordinare.services.environment_scope import (
    PHASE_CONTRACT,
    REVIEW_INSTRUCTIONS,
    REVISION_INSTRUCTIONS,
    _criterion_citation_errors,
    _current_investigations,
    _decision_reference_errors,
    _fail,
    _reassessment_target_errors,
    _Request,
    _request_rule_errors,
    _ResponseReferenceError,
    _validate_responses,
    _validate_review,
    conclude_review,
    discover_scope,
    preserve_scope,
    run_scope,
)
from coordinare.services.environment_scope_investigation import (
    ScopeInvestigationResponseError,
    investigate_scope_requests,
    investigation_evidence,
    validate_investigation_responses,
)
from coordinare.services.environment_shards import (
    Shard,
    plan_shards,
    readable,
    shard_weight,
    share_bytes,
    spine_paths,
    split_by_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from coordinare_service_inference.environment.findings import FindingRecord, FindingResponse
    from coordinare_service_inference.environment.inventory import InventoryEntry
    from coordinare_service_inference.environment.scope import ScopeContract
    from coordinare_service_inference.environment.scope_investigation import ScopeInspectionBinding
    from coordinare_service_inference.environment.scope_revision import ScopeRevision

    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_discovery import ChatJson, Fetch
    from coordinare.services.environment_scope import ScopeRunOptions

SHARD_SCHEMA_REPAIRS = 2
SPINE_FRACTION = 4
MERGE_OWNER = 'merge'
MIN_SPLIT = 2

SHARD_AUTHOR_INSTRUCTIONS = (
    'You author source-defined obligations for ONE shard of a repository too large for one conversation. '
    'Your shard is the set of files in root_directory; other agents handle the rest of the repository, and '
    'spine_evidence holds shared root-level files. Inspect your shard: development/test entry points, dependency '
    'declarations, build and test automation and nested instructions. Report every obligation of the whole documented '
    'development/test environment that evidence in your shard or the spine establishes. Each criterion needs exact '
    'inspected source paths, description, applicability (required, not_applicable or unknown), cited rationale, '
    'observable usability and generic runtime_needs. Do not generate commands or choose a runtime implementation. '
    'Daemon assigns criterion IDs; do not supply them. When your files depend on files outside your shard, list their '
    'exact repository paths in cross_references; never guess paths. Put genuine open questions in questions. '
    'An empty criteria list is valid when your shard establishes no environment obligations. '
    'Return {"shard_summary":{"criteria":[...],"cross_references":[...],"questions":[...]}}. '
)
SHARD_REVIEW_PREAMBLE = (
    'You independently review ONE shard of a repository too large for one conversation. mandatory_evidence holds your '
    'shard\'s inspected files and the shared root-level files; root_directory navigates the whole repository so you can '
    'follow references, but read only what judging your shard requires: your reads are limited. scope_criteria are the '
    'current criteria citing your shard and criterion_index names every current criterion. Judge omissions, conflicts '
    'and applicability for obligations your shard establishes; other reviewers judge the rest. required_decision_ids '
    'are the retained findings assigned to you. '
)
MERGE_REVIEW_PREAMBLE = (
    'You independently review the repository-wide consistency of a scope assembled from per-shard work. '
    'criterion_index names every current criterion with its citations. Judge cross-shard omissions, conflicts and '
    'applicability that no single shard can see, reading any repository file you need. required_decision_ids are the '
    'retained findings assigned to you. '
)
SHARD_REVISION_PREAMBLE = (
    'You revise source-defined obligations for the open findings assigned to you (required_response_ids). '
    'scope_criteria are the criteria those findings concern; criterion_index names every current criterion. '
    'Other agents revise other findings. Respond to exactly your assigned finding IDs. '
)


@dataclass(frozen=True)
class ShardPlan:
    shards: tuple[Shard, ...]
    spine: tuple[str, ...]
    share: int
    owners: Mapping[str, str]

    def digest(self) -> str:
        return hashlib.sha256(canonical({'share': self.share, 'spine': list(self.spine),
            'shards': [[shard.id, list(shard.paths)] for shard in self.shards]})).hexdigest()


def build_plan(entries: Mapping[str, InventoryEntry], *, context_tokens: int, max_file_bytes: int,
               max_reads: int | None = None) -> ShardPlan:
    """Each shard fits half an agent's context and half its per-conversation reads (the rest is for navigation)."""
    share = share_bytes(context_tokens)
    spine = spine_paths(entries, limit_bytes=share // SPINE_FRACTION, max_file_bytes=max_file_bytes)
    max_files = max(1, max_reads // 2 - len(spine)) if max_reads is not None else None
    shards = plan_shards(entries, share=share, spine=spine, max_file_bytes=max_file_bytes, max_files=max_files)
    owners = {path: shard.id for shard in shards for path in shard.paths}
    return ShardPlan(tuple(shards), tuple(spine), share, owners)


def needs_sharding(entries: Mapping[str, InventoryEntry], *, context_tokens: int, max_file_bytes: int) -> bool:
    """One conversation suffices when every readable file fits one share."""
    total = sum(shard_weight(entry) for entry in entries.values() if readable(entry, max_file_bytes=max_file_bytes))
    return total > share_bytes(context_tokens)


def merge_summaries(summaries: Sequence[ShardSummary]) -> list[ScopeCriterionProposal]:
    """Deterministic union by criterion identity: exact duplicates collapse, distinct obligations never fold.

    Identity covers description, sources, applicability, usability and runtime needs, so no citation is lost and
    no applicability is downgraded; reviewers adjudicate near-duplicates as findings.
    """
    merged: dict[str, ScopeCriterionProposal] = {}
    for summary in summaries:
        for criterion in summary.criteria:
            merged.setdefault(criterion_id(criterion), criterion)
    if len(merged) > MAX_SCOPE_CRITERIA:
        _fail('scope_size_limit')  # Obligations are never dropped or folded to fit.
    return list(merged.values())


def route_cross_references(summaries: Sequence[ShardSummary], plan: ShardPlan, *, read: Collection[str],
                           assigned: set[str], generation: int) -> list[Shard]:
    """Unread referenced paths go once to follow-up work of their owning shard; a cycle cannot requeue."""
    groups: dict[str, list[str]] = {}
    for summary in summaries:
        for path in summary.cross_references:
            owner = plan.owners.get(path)
            if owner is None or path in read or path in assigned:
                continue
            assigned.add(path)
            groups.setdefault(owner, []).append(path)
    order = {shard.id: index for index, shard in enumerate(plan.shards)}
    return [Shard(id=f'{owner}.r{generation}', paths=tuple(sorted(paths)), readable_bytes=0, depth=0)
            for owner, paths in sorted(groups.items(), key=lambda item: order[item[0]])]


def criterion_owner(criterion: ScopeCriterion, plan: ShardPlan) -> str:
    for path in sorted(criterion.sources):
        if path in plan.owners:
            return plan.owners[path]
    return MERGE_OWNER


def finding_owner(record: FindingRecord, definition: ScopeDefinition, plan: ShardPlan) -> str:
    for path in sorted(record.proposal.sources):
        if path in plan.owners:
            return plan.owners[path]
    by_id = {criterion.id: criterion for criterion in definition.criteria}
    for identifier in record.proposal.affected_ids:
        if identifier in by_id and (owner := criterion_owner(by_id[identifier], plan)) != MERGE_OWNER:
            return owner
    return MERGE_OWNER


Envelope = Literal['shard_summary', 'scope_assessment', 'scope_revision']
_CORRECTABLE = frozenset({'invalid_json_envelope', 'duplicate_json_key', 'unsupported_envelope',
                          'invalid_tool_arguments', 'scope_citations_invalid'})


class ReviewMaterial(NamedTuple):
    criteria: Sequence[ScopeCriterion]
    owned: Sequence[str]
    evidence: list[dict[str, str]]
    capacity: int


class Assignment(NamedTuple):
    owner: str
    ids: Sequence[str]
    bindings: Sequence[ScopeInspectionBinding]


@dataclass(frozen=True)
class AgentTask:
    envelope: Envelope
    validate: Callable[[dict[str, Any]], Any]
    decision_ids: Sequence[str] | None = None
    criterion_ids: Sequence[str] | None = None

    def schema(self, *, final: bool) -> dict[str, Any]:
        """Source tools only until the agent holds evidence, as in the single-conversation path."""
        if not final:
            return response_schema('source_tool')
        schema = response_schema(self.envelope, review_decision_ids=self.decision_ids)
        if self.envelope == 'scope_assessment' and self.criterion_ids is not None:
            constrain_source_criteria(schema, list(self.criterion_ids))
        return schema


def _rejection_errors(exc: Exception, envelope: str) -> list[dict[str, Any]]:
    """Correctable rejections become feedback; anything else ends the attempt."""
    if isinstance(exc, ValidationError):
        return _validation_feedback(exc)
    if isinstance(exc, PydanticCustomError):
        return [{'location': [envelope], 'code': exc.type}]
    if isinstance(exc, (_ResponseReferenceError, ScopeInvestigationResponseError)):
        return list(exc.errors)
    if isinstance(exc, FindingValidationError):
        return [{'location': list(exc.location), 'code': exc.code, 'guidance': str(exc)}]
    if isinstance(exc, EnvironmentDiscoveryError) and exc.code in _CORRECTABLE:
        return [{'location': [], 'code': exc.code}]
    raise exc


def _restricted(entries: Mapping[str, InventoryEntry], paths: Collection[str]) -> dict[str, InventoryEntry]:
    return {path: entries[path] for path in sorted(paths) if path in entries}


COMPACT_DESCRIPTION_CHARS = 160


def _compact(criterion: ScopeCriterion) -> dict[str, Any]:
    """Index entry: enough to name a criterion; full text travels only to agents that judge or revise it."""
    return {'id': criterion.id, 'description': criterion.description[:COMPACT_DESCRIPTION_CHARS],
            'applicability': criterion.applicability}


def _fingerprints(criteria: Sequence[ScopeCriterion]) -> list[str]:
    """Complete criterion content, including rationale that criterion IDs omit."""
    return sorted(hashlib.sha256(canonical(c.model_dump(mode='json'))).hexdigest() for c in criteria)


@dataclass
class ShardedScope:
    """One attempt's sharded scope work over the shared budget."""

    request: _Request
    entries: Mapping[str, InventoryEntry]
    plan: ShardPlan
    concurrency: int
    spine_files: dict[str, str] = field(default_factory=dict)

    @property
    def budget(self) -> AttemptBudget:
        return self.request.budget

    def view(self, paths: Collection[str] | None = None, *, name: str) -> SourceInspection:
        entries = self.entries if paths is None else _restricted(self.entries, {*paths, *self.plan.spine})
        inspection = SourceInspection(entries, self.request.fetch, self.request.limits, self.budget,
                                      retained_inputs=self.budget.scope_inputs)
        inspection.name = name
        return inspection

    async def load_spine(self) -> None:
        missing = [path for path in self.plan.spine if path not in self.spine_files]
        reader = self.view(name='scope_spine')
        for offset in range(0, len(missing), 8):
            for item in (await reader.read_files(missing[offset:offset + 8]))['files']:
                self.spine_files[item['path']] = item['content']

    def admit_spine(self, inspection: SourceInspection) -> list[dict[str, str]]:
        return [inspection.admit_content(path, self.spine_files[path], fetched=False) for path in self.plan.spine]

    def _persist(self, **state: Any) -> None:
        saved = dict(self.budget.scope_shards or {})
        if saved.get('plan_digest') != self.plan.digest():
            saved = {'plan_digest': self.plan.digest()}
        saved.update(state)
        self.budget.scope_shards = saved

    def _split(self, shard: Shard, _overflow: ShardOverflowError) -> list[Shard]:
        return split_by_bytes(shard, self.entries)

    async def agent_phase(self, inspection: SourceInspection, messages: list[dict[str, str]], task: AgentTask) -> Any:
        """One shard conversation; its own correction allowance, the shared call and read budget."""
        budget, limits = self.budget, self.request.limits
        budget.shard_agents += 1
        base_system = messages[0]['content']
        corrections = 0
        try:
            async with asyncio.timeout_at(min(budget.deadline, time.monotonic() + limits.phase_timeout_seconds)):
                for _ in range(limits.max_model_calls):
                    schema = task.schema(final=bool(inspection.inputs))
                    messages[0] = {'role': 'system', 'content': base_system +
                        '\nReturn one complete envelope matching this schema: ' + canonical(schema).decode()}
                    raw = await self._ask(inspection, messages, schema)
                    try:
                        reply = _decode_reply(raw, inspection.name, limits)
                        if 'tool' in reply:
                            result = await _bounded_tool_result(inspection, reply)
                            messages.extend([{'role': 'assistant', 'content': raw},
                                             {'role': 'user', 'content': canonical({'tool_result': result}).decode()}])
                            continue
                        return task.validate(reply)
                    except (ValidationError, PydanticCustomError, FindingValidationError, EnvironmentDiscoveryError) as exc:
                        errors = _rejection_errors(exc, task.envelope)
                    if corrections >= SHARD_SCHEMA_REPAIRS:
                        budget.validation_codes.extend(error['code'] for error in errors)
                        message = 'shard agent exhausted its schema corrections'
                        raise EnvironmentDiscoveryError(message, code='attempt_schema_limit')
                    corrections += 1
                    budget.shard_schema_repairs += 1
                    budget.expected_envelope = task.envelope
                    explain_rejection(messages, raw, errors, budget)
                _fail('scope_phase_call_limit')
        except TimeoutError:
            budget.check()
            _fail('scope_phase_deadline')

    async def _ask(self, inspection: SourceInspection, messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
        """Charge one call; a conversation outgrowing its context returns its work to the pool for splitting."""
        budget, limits = self.budget, self.request.limits
        budget.reserve_call()
        await budget.flush()
        try:
            budget.context_bytes += check_evidence_context(messages, max_bytes=limits.max_review_context_bytes,
                                                           phase=inspection.name)
            return budget.record_reply(await invoke_chat(self.request.chat, messages, schema=schema,
                max_context_bytes=limits.max_review_context_bytes, context_phase=inspection.name))
        except EnvironmentDiscoveryError as exc:
            if exc.code.endswith('_context_limit'):
                raise ShardOverflowError(None) from None
            raise

    # ----- authoring -----

    async def author_shard(self, shard: Shard) -> ShardSummary:
        inspection = self.view(shard.paths, name='scope_shard_author')
        spine = self.admit_spine(inspection)
        messages = [{'role': 'system', 'content': SOURCE_NAVIGATION + SHARD_AUTHOR_INSTRUCTIONS + PHASE_CONTRACT},
                    {'role': 'user', 'content': canonical({'requested_scope': 'whole_documented_development_test_environment',
                        'phase_contract': {'subject': 'source_obligations', 'runtime_evidence': 'required_after_scope_acceptance',
                                           'readiness_authority': False},
                        'shard': {'id': shard.id, 'files': len(shard.paths)},
                        'root_directory': inspection.list_directory(), 'spine_evidence': spine}).decode()}]
        summary: ShardSummary = await self.agent_phase(inspection, messages, AgentTask('shard_summary',
            partial(self._validate_summary, inspection=inspection, shard=shard)))
        completed = dict((self.budget.scope_shards or {}).get('summaries', {}))
        completed[shard.id] = summary.model_dump(mode='json')
        self._persist(summaries=completed)
        await self.budget.flush()
        return summary

    def _validate_summary(self, data: dict[str, Any], *, inspection: SourceInspection, shard: Shard) -> ShardSummary:
        proposal = ShardSummaryEnvelope.model_validate(data).shard_summary
        errors = []
        for index, criterion in enumerate(proposal.criteria):
            errors.extend(_criterion_citation_errors(criterion.sources, inspection.inputs,
                ('shard_summary', 'criteria', str(index), 'sources')))
        for index, path in enumerate(proposal.cross_references):
            if path not in self.entries:
                errors.append({'code': 'shard_cross_reference_invalid',
                    'location': ['shard_summary', 'cross_references', str(index)],
                    'guidance': 'Cross references must be exact paths present in the pinned repository inventory; '
                                'remove guesses. Preserve every criterion while correcting them.'})
        if errors:
            raise _ResponseReferenceError(errors)
        return ShardSummary(**proposal.model_dump(), shard_id=shard.id, inputs=dict(inspection.inputs))

    async def author_round(self) -> ScopeDefinition:
        budget = self.budget
        await self.load_spine()
        saved = (budget.scope_shards or {}) if (budget.scope_shards or {}).get('plan_digest') == self.plan.digest() else {}
        done = {key: ShardSummary.model_validate(value) for key, value in saved.get('summaries', {}).items()}
        pending = [shard for shard in self.plan.shards if shard.id not in done]
        summaries = [*done.values(), *await run_pool(pending, self.author_shard, budget=budget,
                                                      split=self._split, concurrency=self.concurrency)]
        assigned: set[str] = set(saved.get('assigned', []))
        generation = int(saved.get('generation', 0))
        # Each path is routed once, so following references always terminates; nothing is left unrouted.
        while True:
            read = {path for summary in summaries for path in summary.inputs}
            routed = set(assigned)
            follow = [shard for shard in route_cross_references(summaries, self.plan, read=read, assigned=routed,
                                                                generation=generation + 1) if shard.id not in done]
            if not follow:
                break
            summaries.extend(await run_pool(follow, self.author_shard, budget=budget, split=self._split,
                                            concurrency=self.concurrency))
            generation += 1
            assigned = routed
            self._persist(assigned=sorted(assigned), generation=generation)
            await budget.flush()
        summaries.sort(key=lambda summary: summary.shard_id)
        criteria = merge_summaries(summaries)
        if not criteria:
            _fail('scope_no_obligations')
        inputs = {path: budget.scope_inputs[path] for summary in summaries for path in summary.inputs}
        definition = ScopeDefinition(source_commit=self.request.commit, inventory_digest=self.view(name='scope').inventory_digest,
            inputs=inputs, criteria=[ScopeCriterion(**c.model_dump(), id=criterion_id(c)) for c in criteria])
        budget.scope_definition = definition
        budget.scope_responses = []
        self._persist(summaries={}, assigned=[], generation=0)
        budget.scope_phase = 'review'
        budget.scope_revision_base_digest = None
        await budget.flush()
        return definition

    # ----- review -----

    def _owned(self, definition: ScopeDefinition, ledger: FindingLedger) -> dict[str, list[str]]:
        owned: dict[str, list[str]] = {}
        for record in ledger.records:
            owned.setdefault(finding_owner(record, definition, self.plan), []).append(record.id)
        return owned

    def _review_messages(self, inspection: SourceInspection, *, preamble: str, definition: ScopeDefinition,
                         material: ReviewMaterial) -> list[dict[str, str]]:
        criteria, owned, evidence, capacity = material
        ledger = self.budget.scope_findings
        bindings = [binding for binding in _current_investigations(self.budget) if binding.finding_id in owned]
        records = [record.model_dump(mode='json') for record in ledger.records if record.id in owned] if ledger else []
        responses = [response for response in self.budget.scope_responses if response.get('finding_id') in owned]
        return [{'role': 'system', 'content': SOURCE_NAVIGATION + preamble + REVIEW_INSTRUCTIONS + PHASE_CONTRACT},
                {'role': 'user', 'content': canonical({'requested_scope': 'whole_documented_development_test_environment',
                    'phase_contract': {'subject': 'source_obligations', 'runtime_evidence': 'required_after_scope_acceptance',
                                       'readiness_authority': False},
                    'root_directory': inspection.list_directory(), 'scope_digest': definition.digest(),
                    'criterion_index': [_compact(c) for c in definition.criteria],
                    'scope_criteria': [c.model_dump(mode='json') for c in criteria],
                    'mandatory_evidence': evidence, 'assigned_findings': records,
                    'required_decision_ids': list(owned), 'finding_responses': responses, 'findings_capacity': capacity,
                    'investigation_evidence': investigation_evidence(inspection=inspection, bindings=bindings,
                        results=self.budget.scope_investigation_results),
                    'review_resolution': {'unread_available_source': 'inspect', 'actionable_findings': 'revise',
                                          'unable_to_judge': 'indeterminate'}}).decode()}]

    def _validate_part(self, data: dict[str, Any], *, inspection: SourceInspection, definition: ScopeDefinition,
                       material: ReviewMaterial) -> tuple[ScopeAssessmentProposal, dict[str, str]]:
        owned = material.owned
        assessment = ScopeAssessmentEnvelope.model_validate(normalize_source_assessment(data)).scope_assessment
        if len(assessment.findings) > material.capacity:
            raise _ResponseReferenceError([{'code': 'review_findings_limit', 'location': ['scope_assessment', 'findings'],
                'findings_capacity': material.capacity,
                'guidance': 'Report at most findings_capacity new findings; adjudicate retained findings in decisions. '
                            'If more blocking objections remain, return indeterminate rather than omitting them.'}])
        decided = [decision.finding_id for decision in assessment.decisions]
        if len(decided) != len(set(decided)) or set(decided) != set(owned):
            raise _ResponseReferenceError([{'code': 'review_findings_invalid', 'location': ['scope_assessment', 'decisions'],
                'required_decision_ids': list(owned),
                'guidance': 'Supply exactly one decision for every required_decision_ids entry and no others.'}])
        errors = [{'code': 'scope_citations_invalid', 'location': ['scope_assessment', 'sources', str(index)],
                   'guidance': 'Cite only files you inspected in this review.'}
                  for index, path in enumerate(assessment.sources) if path not in inspection.inputs]
        errors.extend(_decision_reference_errors(assessment, definition=definition))
        errors.extend(_request_rule_errors(assessment, reviewer=self.view(name='scope_review')))
        gaps = any(decision.disposition == 'source_gap' for decision in assessment.decisions)
        if (assessment.verdict == 'accept' and (assessment.findings or gaps)) or (
                assessment.verdict == 'revise' and not assessment.findings and not gaps):
            errors.append({'code': 'review_findings_invalid', 'location': ['scope_assessment', 'verdict'],
                'guidance': 'accept requires no new findings and no source_gap decisions; revise requires at least one.'})
        if errors:
            raise _ResponseReferenceError(errors)
        return assessment, dict(inspection.inputs)

    async def _review_evidence(self, inspection: SourceInspection, definition: ScopeDefinition, *,
                               paths: Collection[str], owned: Sequence[str]) -> list[dict[str, str]]:
        """Reviewers read their own evidence: their shard's inputs, the spine and their findings' investigations."""
        wanted = set(paths) | set(self.plan.spine)
        wanted.update(binding.request.path for binding in _current_investigations(self.budget)
                      if binding.finding_id in owned and binding.request.operation == 'read_file')
        expected = {path: digest for path, digest in self.budget.scope_inputs.items()
                    if path in wanted and (path in definition.inputs or path in self.plan.spine or path not in self.plan.owners)}
        expected.update({path: digest for path, digest in definition.inputs.items() if path in paths})
        return await prepare_mandatory_evidence(inspection, dict(sorted(expected.items())), phase='scope_review')

    async def review_shard(self, shard: Shard, *, definition: ScopeDefinition,
                           material: ReviewMaterial) -> tuple[ScopeAssessmentProposal, dict[str, str]]:
        inspection = self.view(name='scope_shard_review')
        evidence = await self._review_evidence(inspection, definition, paths=shard.paths, owned=material.owned)
        criteria = [c for c in definition.criteria if any(self.plan.owners.get(path) == shard.id for path in c.sources)]
        material = material._replace(criteria=criteria, evidence=evidence)
        messages = self._review_messages(inspection, preamble=SHARD_REVIEW_PREAMBLE, definition=definition, material=material)
        return await self._judge(inspection, messages, definition=definition, material=material)

    async def review_merge(self, *, definition: ScopeDefinition,
                           material: ReviewMaterial) -> tuple[ScopeAssessmentProposal, dict[str, str]]:
        """Repository-wide consistency, and full judgment of criteria no shard owns (root-level obligations)."""
        inspection = self.view(name='scope_merge_review')
        evidence = await self._review_evidence(inspection, definition, paths=(), owned=material.owned)
        criteria = [c for c in definition.criteria if criterion_owner(c, self.plan) == MERGE_OWNER]
        material = material._replace(criteria=criteria, evidence=evidence)
        messages = self._review_messages(inspection, preamble=MERGE_REVIEW_PREAMBLE, definition=definition, material=material)
        return await self._judge(inspection, messages, definition=definition, material=material)

    async def _judge(self, inspection: SourceInspection, messages: list[dict[str, str]], *, definition: ScopeDefinition,
                     material: ReviewMaterial) -> tuple[ScopeAssessmentProposal, dict[str, str]]:
        part: tuple[ScopeAssessmentProposal, dict[str, str]] = await self.agent_phase(inspection, messages, AgentTask(
            'scope_assessment', partial(self._validate_part, inspection=inspection, definition=definition, material=material),
            decision_ids=list(material.owned), criterion_ids=[c.id for c in definition.criteria]))
        return part

    def _review_targets(self, definition: ScopeDefinition, owned: Mapping[str, list[str]],
                        carried: Mapping[str, str]) -> list[Shard]:
        """Shards owning a retained finding, changed criteria or an input no reviewer has read; others carry forward."""
        reviewed = (self.budget.scope_shards or {}).get('reviewed_criteria', {})
        unread = {self.plan.owners.get(path) for path, digest in definition.inputs.items() if carried.get(path) != digest}
        targets = []
        for shard in self.plan.shards:
            current = _fingerprints([c for c in definition.criteria if criterion_owner(c, self.plan) == shard.id])
            if shard.id in owned or shard.id in unread or reviewed.get(shard.id) != current:
                targets.append(shard)
        return targets

    async def review_round(self, definition: ScopeDefinition) -> ScopeContract | None:
        budget = self.budget
        ledger = budget.scope_findings
        if ledger is None:
            _fail('scope_source_identity')
        await self.load_spine()
        owned = self._owned(definition, ledger)
        # Earlier independent reads stand for unchanged shards: the pinned bytes are identical.
        previous = budget.scope_assessment.reviewer_inputs if budget.scope_assessment is not None else {}
        targets = self._review_targets(definition, owned, previous)
        target_paths = {path for shard in targets for path in shard.paths}
        carried = {path: digest for path, digest in previous.items() if path not in target_paths}
        work: list[Shard | None] = [*targets, None]
        # Each part's share of the ledger's remaining capacity, so the combined round always fits.
        capacities = finding_capacities(MAX_FINDINGS - len(ledger.records), len(work))
        slots = {MERGE_OWNER if target is None else target.id: capacity for target, capacity in zip(work, capacities, strict=True)}

        async def review(target: Shard | None) -> tuple[str, ScopeAssessmentProposal, dict[str, str]]:
            owner = MERGE_OWNER if target is None else target.id
            material = ReviewMaterial([], owned.get(owner, []), [], slots[owner])
            if target is None:
                assessment, inputs = await self.review_merge(definition=definition, material=material)
            else:
                assessment, inputs = await self.review_shard(target, definition=definition, material=material)
            return owner, assessment, inputs

        def unsplittable(item: Shard | None, overflow: ShardOverflowError) -> list[Shard | None]:
            return [item]  # A review covers its whole share; overflow holds.

        parts = await run_pool(work, review, budget=budget, split=unsplittable, concurrency=self.concurrency)
        data = combine_round(parts, ledger=ledger)
        reviewer = self.view(name='scope_review')
        reviewer.inputs.update(carried)
        for _owner, _assessment, inputs in parts:
            reviewer.inputs.update(inputs)
        try:
            assessment, updated, record = _validate_review(data, reviewer=reviewer, definition=definition,
                                                           ledger=ledger, commit=self.request.commit)
        except (ValidationError, FindingValidationError, _ResponseReferenceError, PydanticCustomError):
            _fail('scope_shard_review_invalid')
        reviewed = dict((budget.scope_shards or {}).get('reviewed_criteria', {}))
        for shard in targets:
            reviewed[shard.id] = _fingerprints([c for c in definition.criteria if criterion_owner(c, self.plan) == shard.id])
        self._persist(reviewed_criteria=reviewed)
        return await conclude_review(budget, (assessment, updated, record), definition=definition, reviewer=reviewer)

    # ----- revision -----

    async def revise_group(self, item: tuple[str, tuple[str, ...]], *, base: ScopeDefinition,
                           investigator: SourceInspection) -> tuple[ScopeRevision, list[FindingResponse]]:
        owner, ids = item
        budget = self.budget
        ledger = budget.scope_findings
        if ledger is None:
            _fail('scope_source_identity')
        inspection = self.view(name='scope_revision')
        spine = self.admit_spine(inspection)
        bindings = [binding for binding in _current_investigations(budget) if binding.finding_id in ids]
        for binding in bindings:
            path = binding.request.path
            if binding.request.operation == 'read_file' and path in investigator.source_files:
                inspection.admit_content(path, investigator.source_files[path], fetched=False)
        records = [record for record in ledger.records if record.id in ids]
        by_id = {criterion.id: criterion for criterion in base.criteria}
        # Only the criteria these findings name, so splitting the findings also shrinks the evidence.
        concerned = list({identifier: by_id[identifier] for record in records
                          for identifier in record.proposal.affected_ids if identifier in by_id}.values())
        needed = {path for record in records for path in record.proposal.sources}
        needed.update(path for criterion in concerned for path in criterion.sources)
        expected = {path: budget.scope_inputs[path] for path in sorted(needed)
                    if path in budget.scope_inputs and path not in inspection.inputs}
        evidence = await prepare_mandatory_evidence(inspection, expected, phase='scope_revision')
        messages = [{'role': 'system', 'content': SOURCE_NAVIGATION + SHARD_REVISION_PREAMBLE + REVISION_INSTRUCTIONS + PHASE_CONTRACT},
                    {'role': 'user', 'content': canonical({'requested_scope': 'whole_documented_development_test_environment',
                        'root_directory': inspection.list_directory(), 'inventory_digest': inspection.inventory_digest,
                        'scope_base_digest': base.digest(), 'criterion_index': [_compact(c) for c in base.criteria],
                        'scope_criteria': [c.model_dump(mode='json') for c in concerned], 'spine_evidence': spine,
                        'mandatory_evidence': evidence, 'assigned_findings': [r.model_dump(mode='json') for r in records],
                        'required_response_ids': list(ids),
                        'investigation_evidence': investigation_evidence(inspection=inspection, bindings=bindings,
                            results=budget.scope_investigation_results)}).decode()}]
        result: tuple[ScopeRevision, list[FindingResponse]] = await self.agent_phase(inspection, messages, AgentTask(
            'scope_revision', partial(self._validate_group, inspection=inspection, base=base,
                                      assignment=Assignment(owner, ids, bindings))))
        return result

    def _validate_group(self, data: dict[str, Any], *, inspection: SourceInspection, base: ScopeDefinition,
                        assignment: Assignment) -> tuple[ScopeRevision, list[FindingResponse]]:
        owner, ids, bindings = assignment
        envelope = ScopeRevisionEnvelope.model_validate(data)
        revision = envelope.scope_revision
        if len(base.criteria) + len(revision.add) > MAX_SCOPE_CRITERIA:
            _fail('scope_size_limit')  # Obligations are never dropped or folded to fit.
        errors = self._operation_errors(revision, inspection=inspection, base=base, owner=owner)
        if errors:
            raise _ResponseReferenceError(errors)
        try:
            definition = apply_scope_revision(base, revision, inspected_inputs=dict(self.budget.scope_inputs))
        except PydanticCustomError as exc:
            if exc.type == 'scope_revision_target' and (target_errors := _reassessment_target_errors(base, revision)):
                raise _ResponseReferenceError(target_errors) from None
            raise
        responses = envelope.finding_responses
        answered = [response.finding_id for response in responses]
        if len(answered) != len(set(answered)) or set(answered) != set(ids):
            errors.append({'code': 'scope_response_ids_invalid', 'location': ['finding_responses'],
                'required_finding_ids': list(ids),
                'guidance': 'Respond exactly once to every required_response_ids entry and to no other finding. '
                            'Keep the proposed scope_revision operations while correcting responses.'})
        current = {criterion.id for criterion in definition.criteria}
        for index, response in enumerate(responses):
            if any(path not in inspection.inputs for path in response.sources):
                errors.append({'code': 'scope_response_ids_invalid', 'location': ['finding_responses', str(index), 'sources'],
                               'guidance': 'Cite exact files you inspected; keep the proposed scope_revision operations.'})
            if not set(response.affected_ids) <= current:
                errors.append({'code': 'scope_response_ids_invalid', 'location': ['finding_responses', str(index), 'affected_ids'],
                               'guidance': 'Use current criterion IDs, or [] for new or reassessed criteria.'})
        if errors:
            raise _ResponseReferenceError(errors)
        validate_investigation_responses(bindings=bindings, results=self.budget.scope_investigation_results,
                                         responses=responses, inputs=inspection.inputs)
        return revision, list(responses)

    def _operation_errors(self, revision: ScopeRevision, *, inspection: SourceInspection, base: ScopeDefinition,
                          owner: str) -> list[dict[str, Any]]:
        """Citations from this agent's reads; reassessment only of criteria its assignment owns."""
        errors: list[dict[str, Any]] = []
        if revision.base_digest != base.digest():
            errors.append({'code': 'scope_revision_base', 'location': ['scope_revision', 'base_digest'],
                           'guidance': 'Use the exact scope_base_digest.'})
        for index, criterion in enumerate(revision.add):
            errors.extend(_criterion_citation_errors(criterion.sources, inspection.inputs,
                                                     ('scope_revision', 'add', str(index), 'sources')))
        by_id = {criterion.id: criterion for criterion in base.criteria}
        for index, change in enumerate(revision.reassess):
            errors.extend(_criterion_citation_errors(change.criterion.sources, inspection.inputs,
                                                     ('scope_revision', 'reassess', str(index), 'criterion', 'sources')))
            target = by_id.get(change.criterion_id)
            if owner != MERGE_OWNER and target is not None and criterion_owner(target, self.plan) != owner:
                errors.append({'code': 'scope_revision_target', 'location': ['scope_revision', 'reassess', str(index), 'criterion_id'],
                    'guidance': 'Reassess only criteria in scope_criteria owned by your assigned findings; add a criterion instead.'})
        return errors

    async def revision_round(self, base: ScopeDefinition) -> ScopeDefinition:
        budget = self.budget
        ledger = budget.scope_findings
        if ledger is None or base.digest() != budget.scope_revision_base_digest:
            _fail('scope_source_identity')
        await self.load_spine()
        # Investigation is daemon work over every requested file, not a conversation's read allowance.
        investigator = self.view(name='scope_revision')
        investigator.limits = replace(investigator.limits, max_reads=max(investigator.limits.max_reads,
                                      len(_current_investigations(budget)) + 1))
        completed = {result.binding_id for result in budget.scope_investigation_results}
        await investigate_scope_requests(investigator, [binding for binding in _current_investigations(budget)
                                                        if binding.id not in completed])
        for binding in _current_investigations(budget):
            path = binding.request.path
            if binding.request.operation == 'read_file' and path in budget.scope_inputs and path not in investigator.source_files:
                await investigator.read_files([path])
        if budget.scope_phase == 'investigation':
            budget.scope_phase = 'revision'
            await budget.flush()
        groups: dict[str, list[str]] = {}
        for record in ledger.records:
            if record.id in ledger.open_ids():
                groups.setdefault(finding_owner(record, base, self.plan), []).append(record.id)
        order = {shard.id: index for index, shard in enumerate(self.plan.shards)} | {MERGE_OWNER: len(self.plan.shards)}
        work = [(owner, tuple(ids)) for owner, ids in sorted(groups.items(), key=lambda item: order[item[0]])]

        def halves(item: tuple[str, tuple[str, ...]], _overflow: ShardOverflowError) -> list[tuple[str, tuple[str, ...]]]:
            owner, ids = item
            if len(ids) < MIN_SPLIT:
                return [item]
            return [(owner, ids[:len(ids) // 2]), (owner, ids[len(ids) // 2:])]

        results = await run_pool(work, partial(self.revise_group, base=base, investigator=investigator),
                                 budget=budget, split=halves, concurrency=self.concurrency)
        definition, responses = combine_revisions(base, results, inputs=budget.scope_inputs)
        preserve_scope(base, definition)
        try:
            budget.scope_responses = _validate_responses(responses, ledger=ledger, inputs=dict(budget.scope_inputs),
                                                         definition=definition)
        except _ResponseReferenceError:
            _fail('scope_revision_conflict')
        budget.scope_definition = definition
        budget.scope_phase = 'review'
        budget.scope_revision_base_digest = None
        await budget.flush()
        return definition


def finding_capacities(remaining: int, parts: int) -> list[int]:
    """Split the ledger's remaining capacity exactly; earlier parts take the remainder."""
    base, extra = divmod(max(0, remaining), parts)
    return [base + (index < extra) for index in range(parts)]


async def _bounded_tool_result(inspection: SourceInspection, reply: dict[str, Any]) -> dict[str, Any]:
    """A conversation whose read allowance is spent is told so and must conclude; the attempt continues."""
    requested = reply.get('paths') if reply.get('tool') == 'read_files' else [reply.get('path')] if reply.get('tool') == 'read_file' else []
    if isinstance(requested, list) and inspection.reads + len(requested) > inspection.limits.max_reads:
        return {'errors': [{'code': 'read_allowance_exhausted', 'remaining_reads': inspection.limits.max_reads - inspection.reads}],
                'guidance': 'This conversation has no read allowance left for that request. Conclude from the evidence you '
                            'hold; report unread relevant files as findings or open questions rather than guessing.'}
    return await inspection.tool_result(reply)


def combine_revisions(base: ScopeDefinition, results: Sequence[tuple[ScopeRevision, list[FindingResponse]]], *,
                      inputs: Mapping[str, str]) -> tuple[ScopeDefinition, list[FindingResponse]]:
    """Apply every group's revision in order. Identical additions collapse; a response naming a criterion another
    group reassessed follows it to its replacement. Genuinely conflicting reassessments hold."""
    definition, responses, replaced = base, [], {}
    for revision, answered in results:
        current = {criterion.id for criterion in definition.criteria}
        additions = [criterion for criterion in revision.add if criterion_id(criterion) not in current]
        # An identical reassessment already applied by another group is the same change, not a conflict.
        reassess = [item for item in revision.reassess
                    if not (item.criterion_id in replaced and criterion_id(item.criterion) in current)]
        if len(definition.criteria) + len(additions) > MAX_SCOPE_CRITERIA:
            _fail('scope_size_limit')  # Obligations are never dropped or folded to fit.
        change = revision.model_copy(update={'base_digest': definition.digest(), 'add': additions, 'reassess': reassess})
        try:
            definition = apply_scope_revision(definition, change, inspected_inputs=dict(inputs))
        except (PydanticCustomError, ValidationError):
            _fail('scope_revision_conflict')
        replaced.update({item.criterion_id: criterion_id(item.criterion) for item in revision.reassess})
        responses.extend(answered)
    final = [response.model_copy(update={'affected_ids': [replaced.get(identifier, identifier)
                                                          for identifier in response.affected_ids]})
             for response in responses]
    return definition, final


def combine_round(parts: Sequence[tuple[str, ScopeAssessmentProposal, Mapping[str, str]]], *,
                  ledger: FindingLedger) -> dict[str, Any]:
    """One review sequence from every part: accept only when every part accepts; merge-level decisions win."""
    verdicts = [assessment.verdict for _owner, assessment, _inputs in parts]
    verdict = 'indeterminate' if 'indeterminate' in verdicts else 'accept' if set(verdicts) == {'accept'} else 'revise'
    decisions: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    sources: set[str] = set()
    for owner, assessment, _inputs in sorted(parts, key=lambda part: part[0] == MERGE_OWNER):
        findings.extend(item.model_dump(mode='json') for item in assessment.findings)
        sources.update(assessment.sources)
        for decision in assessment.decisions:
            if owner == MERGE_OWNER or decision.finding_id not in decisions:
                decisions[decision.finding_id] = decision.model_dump(mode='json')
    order = {record.id: index for index, record in enumerate(ledger.records)}
    summary = 'Sharded independent review: ' + '; '.join(f'{owner} {assessment.verdict}'
                                                          for owner, assessment, _inputs in parts)
    rationale = summary.encode()[:2000].decode(errors='ignore')
    return {'scope_assessment': {'verdict': verdict, 'rationale': rationale, 'sources': sorted(sources),
        'findings': findings, 'decisions': [decisions[key] for key in sorted(decisions, key=lambda key: order.get(key, 0))]}}


def _sharded(request: _Request) -> bool:
    limits, entries = request.limits, request.entries
    return bool(entries and limits.shard_context_tokens and request.budget.scope_contract is None and needs_sharding(
        entries, context_tokens=limits.shard_context_tokens, max_file_bytes=limits.max_file_bytes))


async def discover_source_scope(*, paths: Sequence[str], fetch: Fetch, chat: ChatJson, commit: str,
                                **options: Unpack[ScopeRunOptions]) -> ScopeContract:
    """One conversation when the repository fits one share; otherwise the same protocol divided into shards."""
    request = _Request(paths, fetch, chat, commit, options['limits'], options['budget'], entries=options.get('entries'))
    if not _sharded(request):
        return await discover_scope(paths=paths, fetch=fetch, chat=chat, commit=commit, **options)
    _checked_inventory(paths, request.limits)
    return await run_scope(request, discover_sharded_scope)


async def discover_sharded_scope(request: _Request) -> ScopeContract:
    """The single-conversation loop's states and verdict handling, with each phase divided into shards."""
    budget, limits, entries = request.budget, request.limits, request.entries
    if not entries or not limits.shard_context_tokens:
        _fail('scope_source_identity')
    plan = build_plan(entries, context_tokens=limits.shard_context_tokens, max_file_bytes=limits.max_file_bytes,
                      max_reads=limits.max_reads)
    saved = budget.scope_shards
    if saved is not None and saved.get('plan_digest') not in (None, plan.digest()):
        _fail('scope_shard_identity')
    scope = ShardedScope(request, entries, plan, limits.agent_concurrency)
    if budget.scope_findings is None:
        budget.scope_findings = FindingLedger(source_commit=request.commit)
    elif budget.scope_findings.source_commit != request.commit:
        _fail('scope_source_identity')
    while True:
        if budget.scope_phase == 'initial':
            definition = await scope.author_round()
        elif budget.scope_phase in {'investigation', 'revision'}:
            if budget.scope_definition is None:
                _fail('scope_source_identity')
            definition = await scope.revision_round(budget.scope_definition)
        else:
            if budget.scope_definition is None:
                _fail('scope_source_identity')
            definition = budget.scope_definition
        contract = await scope.review_round(definition)
        if contract is not None:
            return contract


def shard_plan_summary(plan: ShardPlan) -> dict[str, Any]:
    return {'shards': len(plan.shards), 'spine': len(plan.spine), 'share': plan.share,
            'largest_shard_files': max((len(shard.paths) for shard in plan.shards), default=0)}


__all__ = [
    'ShardPlan',
    'ShardedScope',
    'build_plan',
    'combine_revisions',
    'combine_round',
    'discover_sharded_scope',
    'discover_source_scope',
    'finding_owner',
    'merge_summaries',
    'needs_sharding',
    'route_cross_references',
    'shard_plan_summary',
]
