"""Adapt independent bootstrap phases to native agents without moving admission."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nare import Tool
from nare.tools import MAX_TOOL_OUTPUT
from pydantic import ValidationError

from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceSource, EvidenceStore
from coordinare.services.agent_runtime.models import AdmissionIssue, AgentIdentity, AgentRequest
from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.agent_runtime.models import AgentResult, AgentRuntime, JsonObject
    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_inspection import SourceInspection


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


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
            raise EnvironmentDiscoveryError('Source entry exceeds native output bound.', code='agent_evidence_cap')
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
    tools = (evidence.tool(), *request.tools, *(request.source_tools.tools() if request.source_tools else ()))
    identity = AgentIdentity(attempt_id=request.attempt_id, phase=request.phase, role=request.role,
        source_commit=request.source_commit,
        domain_digests=tuple(sorted((*request.domain_digests, ('packet', _digest(request.packet)),
                                    ('evidence', evidence.receipt().catalog_digest)))),
        policy_digest=_digest({'tools': [{'name': tool.name, 'schema': tool.input_schema} for tool in tools],
            'phase_calls': request.max_model_calls, 'phase_seconds': request.phase_timeout_seconds,
            'attempt_calls': request.budget.max_calls, 'reads': request.budget.max_reads,
            'schema_repairs': request.budget.max_schema_repairs}),
        prompt_digest=_digest(request.instructions),
        schema_digest=_digest(request.schema), model_config_digest=request.model_config_digest)
    value: Any = None

    async def check(answer: JsonObject) -> list[AdmissionIssue]:
        nonlocal value
        if request.source_tools is not None and (issues := request.source_tools.issues()):
            return issues
        if evidence.remaining_required:
            return [AdmissionIssue('review_evidence_unread', 'Read every required evidence page before judgment.',
                                   guidance='Use review_evidence and continue from next_unread until unread_count is zero.')]
        if request.envelope not in answer or not isinstance(answer[request.envelope], dict):
            return [AdmissionIssue('agent_envelope_invalid', 'Return the declared final envelope.')]
        try:
            value = request.validate(answer[request.envelope])
        except EnvironmentDiscoveryError as exc:
            return [AdmissionIssue(exc.code, str(exc), location=getattr(exc, 'location', ()))]
        except ValidationError as exc:
            return [AdmissionIssue(error['type'], 'The proposal does not satisfy the declared schema.',
                                   location=tuple(error['loc'])) for error in exc.errors(include_input=False, include_url=False)]
        return []

    result = await request.runtime.run(AgentRequest(identity=identity,
        instructions=request.instructions, task=f'Read the immutable phase packet and required sources using review_evidence. '
            f'There are {evidence.page_count} pages in this catalog. Then return the {request.envelope} envelope.',
        answer_schema=request.schema, check=check, budget=request.budget, allowed_tools=tools,
        checkpoint_key=request.checkpoint_key, checkpoint_store=request.checkpoint_store,
        evidence_catalog=evidence, event_sink=request.event_sink,
        phase_deadline=time.monotonic() + request.phase_timeout_seconds, phase_max_calls=request.max_model_calls))
    if result.status != 'accepted' or result.answer is None:
        raise EnvironmentDiscoveryError('Native phase did not produce an admitted result.',
                                        code=result.hold_code or 'agent_incomplete')
    return PhaseResult(result.answer[request.envelope], value, result, evidence)
