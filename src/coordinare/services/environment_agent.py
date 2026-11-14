"""Adapt independent bootstrap phases to native agents without moving admission."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nare import Tool
from nare.tools import MAX_TOOL_OUTPUT
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import PydanticCustomError

from coordinare.services.agent_runtime.checkpoint import (
    EvidenceReceipt,  # noqa: TC001 - pydantic resolves the field at runtime
)
from coordinare.services.agent_runtime.errors import invalid_checkpoint, refuse_agent
from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceSource, EvidenceStore
from coordinare.services.agent_runtime.models import AdmissionIssue, AgentIdentity, AgentRequest
from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.agent_runtime.effects import EffectJournal
    from coordinare.services.agent_runtime.models import AgentResult, AgentRuntime, JsonObject
    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_inspection import SourceInspection


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


async def unavailable_legacy_chat(messages: list[dict[str, str]]) -> str:
    """Refuse an accidental old-loop call while those loops await acceptance/deletion."""
    refuse_agent('Native bootstrap cannot invoke the old chat transport.', code='agent_legacy_transport_refused')


@dataclass(frozen=True)
class EnvironmentAgentContext:
    runtime: AgentRuntime
    attempt_id: str
    model_config_digest: str
    checkpoint_store: CheckpointStore | None = None
    event_sink: Callable[[JsonObject], Awaitable[None]] | None = None


class PinnedSourceTools:
    """Read-only navigation with complete pages, backed by the domain's reader."""

    def __init__(self, inspection: SourceInspection, *,
                 observe_sources: Callable[[dict[str, str]], Awaitable[None]] | None = None) -> None:
        self.inspection = inspection
        self.observe_sources = observe_sources
        self.catalogs: dict[str, EvidenceStore] = {}
        self.failure: EnvironmentDiscoveryError | None = None

    def issues(self) -> list[AdmissionIssue]:
        if self.failure is not None:
            raise self.failure
        if any(store.remaining_required for store in self.catalogs.values()):
            return [AdmissionIssue('review_evidence_unread', 'Finish reading each requested source using read_source.')]
        return []

    async def read(self, arguments: JsonObject) -> str:
        path = arguments['path']
        if path not in self.catalogs:
            policy = self.inspection.policy_status(path)
            if policy != 'readable':
                return _json({'path': path, 'error': policy})
            try:
                item = await self.inspection.read(path)
                if self.observe_sources is not None:
                    await self.observe_sources(self.inspection.inputs)
                self.catalogs[path] = EvidenceStore((EvidenceSource(path, item['content'], item['sha256']),),
                                                   required=frozenset({path}))
            except EnvironmentDiscoveryError as exc:
                if exc.code != 'invalid_tool_arguments':
                    self.failure = exc
                raise
        return (await self.catalogs[path].read(EvidenceRead(tuple(arguments.get('pages', ()))))).serialized

    async def listing(self, arguments: JsonObject) -> str:
        cursor = arguments.get('cursor', 0)
        result = self.inspection.list_directory(arguments.get('path', ''), cursor)
        while len(_json(result)) > MAX_TOOL_OUTPUT and result['entries']:
            result['entries'].pop()
            result['next_cursor'] = cursor + len(result['entries'])
        if not result['entries'] and result['next_cursor'] == cursor:
            refuse_agent('Source entry exceeds native output bound.', code='agent_evidence_cap')
        return _json(result)

    def tools(self) -> tuple[Tool, ...]:
        return (
            Tool(name='read_source', description='Read a pinned source in complete pages. Continue the same path '
                 'until unread_count is zero; supply page IDs to reread.',
                 input_schema={'type': 'object', 'properties': {'path': {'type': 'string'}, 'pages': {
                     'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'maxItems': 128}},
                     'required': ['path'], 'additionalProperties': False}, call=self.read),
            Tool(name='list_sources', description='List the immutable repository directory, following next_cursor.',
                 input_schema={'type': 'object', 'properties': {'path': {'type': 'string'},
                     'cursor': {'type': 'integer', 'minimum': 0}}, 'additionalProperties': False}, call=self.listing),
        )


class ObservationTools:
    """Whole runtime observations, never tool-output truncation or readiness proof."""

    def __init__(self) -> None:
        self.catalogs: dict[str, EvidenceStore] = {}
        self.contents: dict[str, str] = {}

    def issues(self) -> list[AdmissionIssue]:
        if any(store.remaining_required for store in self.catalogs.values()):
            return [AdmissionIssue('runtime_observation_unread', 'Finish reading each observation using read_observation.')]
        return []

    def register(self, value: JsonObject) -> str:
        content = _json(value)
        key = hashlib.sha256(content.encode()).hexdigest()
        if key not in self.catalogs:
            source = '@observation/' + key
            self.catalogs[key] = EvidenceStore((EvidenceSource(source, content, key),),
                required=frozenset({source}), output_cap=MAX_TOOL_OUTPUT - 128)
            self.contents[key] = content
        return key

    async def add(self, value: JsonObject) -> str:
        key = self.register(value)
        return await self.read({'observation_id': key})

    async def read(self, arguments: JsonObject) -> str:
        key = arguments['observation_id']
        if key not in self.catalogs:
            refuse_agent('Observation identity is unavailable.', code='invalid_tool_arguments')
        batch = await self.catalogs[key].read(EvidenceRead(tuple(arguments.get('pages', ()))))
        return _json({'observation_id': key, **json.loads(batch.serialized)})

    def tool(self) -> Tool:
        return Tool(name='read_observation', description='Read or reread complete untrusted probe/trial observation pages. '
                    'Continue until unread_count is zero. Observations never establish readiness.',
            input_schema={'type': 'object', 'properties': {'observation_id': {'type': 'string'},
                'pages': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'maxItems': 128}},
                'required': ['observation_id'], 'additionalProperties': False}, call=self.read)


class _CachedSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(repr=False)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    receipt: EvidenceReceipt | None = None


class _PhaseToolSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sources: dict[str, _CachedSource] = Field(default_factory=dict)
    observations: dict[str, _CachedSource] = Field(default_factory=dict)
    reads: int = Field(default=0, ge=0, strict=True)
    source_bytes: int = Field(default=0, ge=0, strict=True)
    failure: str | None = None


class PhaseToolState:
    """Private immutable content and exact delivery receipts, bound by the checkpoint hash."""

    def __init__(self, sources: PinnedSourceTools | None, observations: ObservationTools | None,
                 effects: EffectJournal | None = None) -> None:
        self.sources, self.observations, self.effects = sources, observations, effects

    def snapshot(self) -> JsonObject:
        state = _PhaseToolSnapshot()
        if self.sources is not None:
            inspection = self.sources.inspection
            state.reads, state.source_bytes = inspection.reads, inspection.total_bytes
            state.failure = self.sources.failure.code if self.sources.failure is not None else None
            state.sources = {path: _CachedSource(content=content, sha256=inspection.inputs[path],
                receipt=self.sources.catalogs[path].receipt() if path in self.sources.catalogs else None)
                for path, content in inspection.source_files.items()}
        if self.observations is not None:
            state.observations = {key: _CachedSource(content=self.observations.contents[key], sha256=key,
                receipt=catalog.receipt()) for key, catalog in self.observations.catalogs.items()}
        return state.model_dump(mode='json')

    async def restore(self, saved: JsonObject) -> None:
        try:
            state = _PhaseToolSnapshot.model_validate(saved)
            if state.failure:
                refuse_agent('Saved source action requires domain reconciliation.', code=state.failure)
            self._check_tool_presence(state)
            await self._restore_sources(state)
            self._restore_observations(state)
        except (ValueError, TypeError, KeyError):
            refuse_agent('Saved native tool state cannot be verified.', code='agent_tool_state_integrity')

    def _check_tool_presence(self, state: _PhaseToolSnapshot) -> None:
        if (state.sources and self.sources is None) or (state.observations and self.observations is None):
            invalid_checkpoint()

    async def _restore_sources(self, state: _PhaseToolSnapshot) -> None:
        if self.sources is None:
            return
        inspection = self.sources.inspection
        if state.reads > inspection.limits.max_reads or state.source_bytes > inspection.limits.max_source_bytes:
            invalid_checkpoint()
        for path, item in state.sources.items():
            catalog = EvidenceStore((EvidenceSource(path, item.content, item.sha256),), required=frozenset({path}))
            if inspection.inputs.get(path, item.sha256) != item.sha256:
                invalid_checkpoint()
            if path not in inspection.source_files:
                inspection.admit_content(path, item.content, fetched=False)
            elif inspection.source_files[path] != item.content:
                invalid_checkpoint()
            if item.receipt is not None:
                catalog.restore_receipt(item.receipt)
                self.sources.catalogs[path] = catalog
        inspection.reads = max(inspection.reads, state.reads)
        inspection.total_bytes = max(inspection.total_bytes, state.source_bytes)
        if self.sources.observe_sources is not None:
            try:
                await self.sources.observe_sources(inspection.inputs)
            except EnvironmentDiscoveryError as exc:
                self.sources.failure = exc
                raise

    def _restore_observations(self, state: _PhaseToolSnapshot) -> None:
        if self.observations is None:
            return
        for key, item in state.observations.items():
            if key != item.sha256 or hashlib.sha256(item.content.encode()).hexdigest() != key:
                invalid_checkpoint()
            if self.observations.register(json.loads(item.content)) != key or item.receipt is None:
                invalid_checkpoint()
            self.observations.catalogs[key].restore_receipt(item.receipt)
        # An action completion can be durable before its first tool page.
        # Reconstruct that observation, with ZERO delivered pages.
        if self.effects is not None:
            for effect in self.effects.receipts():
                if effect.status == 'completed' and effect.result_json is not None:
                    self.observations.register({effect.tool + '_result': json.loads(effect.result_json),
                        'authority': 'untrusted observation, never readiness'})


@dataclass(frozen=True)
class EnvironmentPhaseRequest:
    attempt_id: str
    phase: str
    role: str
    source_commit: str
    domain_digests: tuple[tuple[str, str], ...]
    model_config_digest: str
    instructions: str
    packet: JsonObject
    sources: tuple[EvidenceSource, ...]
    required: frozenset[str]
    envelope: str
    schema: JsonObject
    validate: Callable[[JsonObject], Any]
    runtime: AgentRuntime
    budget: AttemptBudget
    phase_timeout_seconds: float
    checkpoint_store: CheckpointStore | None = None
    checkpoint_key: str | None = None
    tools: tuple[Tool, ...] = ()
    event_sink: Callable[[JsonObject], Awaitable[None]] | None = None
    source_tools: PinnedSourceTools | None = None
    max_model_calls: int = 128
    max_output_bytes: int | None = None
    max_context_bytes: int | None = None
    validate_envelope: bool = False
    require_source: bool = False
    shard_corrections: int | None = None
    effects: EffectJournal | None = None
    observations: ObservationTools | None = None
    rejection_errors: Callable[[Exception], list[JsonObject]] | None = None
    prerequisites: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PhaseResult:
    data: JsonObject
    value: Any
    agent: AgentResult
    evidence: EvidenceStore


async def run_environment_phase(request: EnvironmentPhaseRequest) -> PhaseResult:
    """An admitted assessment can still reject the environment it assesses."""
    request.budget.check()
    request.budget.stage = request.phase
    packet = _json(request.packet)
    sources = (*request.sources, EvidenceSource('@phase-packet', packet, hashlib.sha256(packet.encode()).hexdigest()))
    evidence = EvidenceStore(sources, required=request.required | {'@phase-packet'})
    tools = (evidence.tool(), *request.tools, *(request.source_tools.tools() if request.source_tools else ()),
             *((request.observations.tool(),) if request.observations else ()))
    identity = AgentIdentity(attempt_id=request.attempt_id, phase=request.phase, role=request.role,
        source_commit=request.source_commit,
        domain_digests=tuple(sorted((*request.domain_digests, ('packet', _digest(request.packet)),
                                    ('evidence', evidence.receipt().catalog_digest)))),
        policy_digest=_digest({'tools': [{'name': tool.name, 'schema': tool.input_schema} for tool in tools],
            'phase_calls': request.max_model_calls, 'phase_seconds': request.phase_timeout_seconds,
            'attempt_calls': request.budget.max_calls, 'reads': request.budget.max_reads,
            'schema_repairs': request.budget.max_schema_repairs, 'output_bytes': request.max_output_bytes,
            'context_bytes': request.max_context_bytes, 'require_source': request.require_source,
            'shard_corrections': request.shard_corrections, 'prerequisites': sorted(request.prerequisites)}),
        prompt_digest=_digest(request.instructions),
        schema_digest=_digest(request.schema), model_config_digest=request.model_config_digest)
    value: Any = None
    admitted: JsonObject | None = None
    terminal_error: EnvironmentDiscoveryError | None = None

    async def check(answer: JsonObject) -> list[AdmissionIssue]:
        nonlocal value, admitted, terminal_error
        candidate = deepcopy(answer)
        for tool_owner in (request.source_tools, request.observations):
            if tool_owner is not None and (issues := tool_owner.issues()):
                return issues
        if evidence.remaining_required:
            return [AdmissionIssue('review_evidence_unread', 'Read every required evidence page before judgment.',
                                   guidance='Use review_evidence and continue from next_unread until unread_count is zero.')]
        if request.require_source and (request.source_tools is None or not request.source_tools.inspection.inputs):
            return [AdmissionIssue('source_evidence_required', 'Read pinned source using read_source before answering.')]
        if request.envelope not in answer or not isinstance(answer[request.envelope], dict):
            return [AdmissionIssue('agent_envelope_invalid', 'Return the declared final envelope.')]
        try:
            value = request.validate(candidate if request.validate_envelope else candidate[request.envelope])
        except (EnvironmentDiscoveryError, PydanticCustomError, ValidationError) as exc:
            try:
                errors = (request.rejection_errors(exc) if request.rejection_errors is not None
                          else _review_rejection(exc))
            except EnvironmentDiscoveryError as fatal:
                terminal_error = fatal
                raise
            return [AdmissionIssue(error['code'], str(error.get('guidance') or error['code']),
                    location=tuple(error.get('location', ()))) for error in errors]
        admitted = candidate
        return []

    checkpoint_key = request.checkpoint_key
    if checkpoint_key is None:
        checkpoint_key = next((reference.key for reference in request.budget.agent_checkpoints.values()
                               if reference.identity == identity), None)
    result = await request.runtime.run(AgentRequest(identity=identity,
        instructions=request.instructions, task=f'Read the immutable phase packet and required sources using review_evidence. '
            f'There are {evidence.page_count} pages in this catalog. Then return the {request.envelope} envelope.',
        answer_schema=request.schema, check=check, budget=request.budget, allowed_tools=tools,
        checkpoint_key=checkpoint_key, checkpoint_store=request.checkpoint_store,
        evidence_catalog=evidence, event_sink=request.event_sink,
        phase_deadline=time.monotonic() + request.phase_timeout_seconds, phase_max_calls=request.max_model_calls,
        max_output_bytes=request.max_output_bytes, max_context_bytes=request.max_context_bytes,
        shard_corrections=request.shard_corrections, effects=request.effects,
        tool_state=PhaseToolState(request.source_tools, request.observations, request.effects),
        prerequisites=request.prerequisites))
    if result.status == 'cancelled':
        request.budget.error_code = result.hold_code or 'agent_cancelled'
        raise asyncio.CancelledError
    if terminal_error is not None:
        raise terminal_error
    if request.source_tools is not None and request.source_tools.failure is not None:
        raise request.source_tools.failure
    if result.status != 'accepted' or result.answer is None:
        refuse_agent(f'Native phase did not produce an admitted result ({result.hold_code or "agent_incomplete"}).',
                                        code=result.hold_code or 'agent_incomplete')
    if admitted is None:
        refuse_agent('Native result has no admission receipt.', code='agent_check_failed')
    return PhaseResult(admitted[request.envelope], value, result, evidence)


def _review_rejection(exc: Exception) -> list[JsonObject]:
    if isinstance(exc, ValidationError):
        return [{'code': error['type'], 'location': list(error['loc'])}
                for error in exc.errors(include_input=False, include_url=False)]
    if isinstance(exc, PydanticCustomError):
        return [{'code': exc.type}]
    if isinstance(exc, EnvironmentDiscoveryError) and exc.code in {
        'review_citations_invalid', 'review_findings_invalid', 'finding_response_ids_invalid',
    }:
        return [{'code': exc.code, 'location': list(getattr(exc, 'location', ())), 'guidance': str(exc)}]
    raise exc


@dataclass(frozen=True)
class SourcePhase:
    envelope: str
    schema: JsonObject
    source_commit: str
    validate: Callable[[JsonObject], Any]
    role: str
    listing_only: bool = False
    shard_corrections: int | None = None
    instance_id: str = ''
    rejection_errors: Callable[[Exception], list[JsonObject]] | None = None


async def run_source_phase(inspection: SourceInspection, messages: list[dict[str, str]],
                           phase: SourcePhase) -> Any:
    """A source role supplies domain rules; native tools own conversation delivery."""
    budget = inspection.budget
    if budget is None or budget.agent_context is None:
        refuse_agent('Native phase context is missing.', code='agent_context_missing')
    native = budget.agent_context
    result = await run_environment_phase(EnvironmentPhaseRequest(
        attempt_id=native.attempt_id, phase=inspection.name, role=phase.role, source_commit=phase.source_commit,
        domain_digests=(('inventory', inspection.inventory_digest), ('instance', _digest(phase.instance_id))),
        model_config_digest=native.model_config_digest,
        instructions=messages[0]['content'], packet={'context': messages[1:]},
        sources=tuple(EvidenceSource(path, text, inspection.inputs[path])
                      for path, text in sorted(inspection.source_files.items())),
        required=frozenset(inspection.source_files), envelope=phase.envelope, schema=phase.schema,
        validate=phase.validate, validate_envelope=True, require_source=not phase.listing_only, rejection_errors=phase.rejection_errors,
        runtime=native.runtime, budget=budget, phase_timeout_seconds=inspection.limits.phase_timeout_seconds,
        max_model_calls=inspection.limits.max_model_calls, max_output_bytes=inspection.limits.max_output_bytes,
        max_context_bytes=inspection.limits.max_review_context_bytes,
        shard_corrections=phase.shard_corrections,
        checkpoint_store=native.checkpoint_store, event_sink=native.event_sink,
        source_tools=None if phase.listing_only else PinnedSourceTools(inspection)))
    return result.value
