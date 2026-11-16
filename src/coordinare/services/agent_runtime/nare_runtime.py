"""Native conversations bounded by Coordinare admission, accounting and persistence."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from contextlib import aclosing
from copy import deepcopy
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Literal

from nare import Policy, dumps, loads, new_session, run
from nare.loop import RetryPolicy, check_structured_output
from nare.schema import UnsupportedSchema, check_supported, validate

from coordinare.services.agent_runtime.accounting import (
    BudgetBridge,
    BudgetTransport,
    TransportContext,
)
from coordinare.services.agent_runtime.checkpoint import (
    COUNTERS,
    AgentCheckpoint,
    CheckpointReference,
    EvidenceReceipt,
)
from coordinare.services.agent_runtime.errors import refuse_agent
from coordinare.services.agent_runtime.evidence_protocol import EvidenceFailureBudget
from coordinare.services.agent_runtime.models import AgentResult
from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from typing import Any

    from nare import Event, Transport

    from coordinare.services.agent_runtime.configuration import AgentModelConfig
    from coordinare.services.agent_runtime.models import AgentRequest, JsonObject
    from coordinare.services.conductor_calls import ConductorCallLog


class NareRuntime:
    """One independent role per identity, with only explicit domain tools."""

    def __init__(self, *, transport: Transport | None = None, config: AgentModelConfig | None = None,
                 retry: RetryPolicy | None = None, calls: ConductorCallLog | None = None) -> None:
        if (transport is None) == (config is None):
            message = 'Supply one configured model or injected transport.'
            raise ValueError(message)
        self._transport, self._config = transport, config
        self._retry = retry or RetryPolicy()
        self._calls = calls

    async def run(self, request: AgentRequest) -> AgentResult:
        execution = _Execution(request)
        transport = self._transport
        try:
            request.budget.check()
            schema = _native_schema(request.answer_schema)
            await execution.restore()
            if execution.session.status == 'done':
                if not isinstance(execution.session.output, dict) or validate(execution.session.output, schema):
                    refuse_agent('Saved answer no longer satisfies the declared schema.',
                        code='agent_checkpoint_answer')
                if await execution.diagnose(deepcopy(execution.session.output)):
                    refuse_agent('Saved answer no longer satisfies response diagnostics.',
                        code='agent_checkpoint_answer')
                await execution.check(deepcopy(execution.session.output))
            else:
                if request.effects is not None and request.checkpoint_store is not None:
                    request.effects.persist = execution.save
                if transport is None:
                    if self._config is None:
                        refuse_agent('Native model route is unavailable.', code='agent_context_missing')
                    transport = self._config.transport(request.instructions)
                await self._converse(execution, transport, schema)
            execution.classify()
        except TimeoutError:
            execution.hold_code = 'attempt_deadline' if time.monotonic() >= request.budget.deadline else 'phase_deadline'
        except EnvironmentDiscoveryError as exc:
            execution.hold_code = exc.code
        except asyncio.CancelledError:
            execution.status, execution.hold_code = 'cancelled', 'agent_cancelled'
        except Exception:
            execution.status, execution.hold_code = 'error', 'agent_runtime_failed'
        finally:
            await self._finish(execution, transport)
        return execution.result()

    async def _finish(self, execution: _Execution, transport: Transport | None) -> None:
        execution.terminal = True
        if self._config is not None and transport is not None:
            try:
                await _close_owned_transport(transport)
            except asyncio.CancelledError:
                execution.status, execution.hold_code = 'cancelled', 'agent_cancelled'
            except Exception:
                execution.status, execution.hold_code = 'held', 'agent_transport_cleanup'
        try:
            for event in tuple(execution.session.events):
                if 'schema_correction' in event.detail:
                    await execution.record_correction(event.detail['schema_correction'], reconcile=True)
                    # This event has now crossed the domain accounting boundary.
                    # Keeping it in a resumed native queue would charge it twice.
                    execution.session.events.remove(event)
                await execution.evidence_failures.reconcile(
                    event.detail, session_id=execution.session.id, turn=execution.session.turns)
            await execution.save()
            await execution.emit('terminal')
        except Exception:
            execution.status, execution.hold_code = 'held', 'agent_checkpoint_persistence'

    async def _converse(self, execution: _Execution, transport: Transport, schema: JsonObject) -> None:
        request = execution.request
        wrapped = BudgetTransport(transport, execution.bridge, TransportContext(session_id=execution.session.id,
            before_send=execution.save, calls=self._calls, config=self._config, instructions=request.instructions,
            max_output_bytes=request.max_output_bytes, max_context_bytes=request.max_context_bytes,
            answer_schema=schema, request_stage=request.request_stage))
        mode = self._config.structured_transport if self._config is not None else None
        try:
            check_structured_output(wrapped, schema, mode)
        except ValueError:
            refuse_agent(
                'Selected answer encoding is not supported by the configured transport and schema.',
                code='agent_transport_unsupported')
        names = frozenset(tool.name for tool in request.allowed_tools)
        deadline = min(request.budget.deadline, time.monotonic() + execution.phase_expires_at - time.time())
        diagnostics: dict[str, Any] = {'diagnose': execution.diagnose} if request.diagnose is not None else {}
        async with asyncio.timeout_at(deadline):
            events = run(execution.session, transport=wrapped,
                policy=Policy(tools=names, pending=names), tools=request.allowed_tools,
                schema=schema, check=execution.check, structured_output=mode,
                max_turns=max(1, request.budget.max_calls - request.budget.calls),
                max_corrections=request.shard_corrections if request.shard_corrections is not None else
                    max(1, execution.session.schema_corrections
                        + request.budget.max_schema_repairs - request.budget.schema_repairs
                        + len(request.prerequisites - execution.prerequisite_reminders)),
                retry=self._retry, compaction='elide', compaction_trigger=0.6, compaction_target=0.4, **diagnostics)
            async with aclosing(events):
                async for event in events:
                    await execution.event(event)


def _native_schema(schema: JsonObject) -> JsonObject:
    """Drop Pydantic's routing annotation, retaining every validation branch."""
    result = deepcopy(schema)

    def visit(node: JsonObject) -> None:
        if 'discriminator' in node and isinstance(node.get('oneOf'), list):
            del node['discriminator']  # Annotation only; oneOf and each tag const remain.
        for keyword in ('properties', '$defs', 'definitions'):
            if isinstance(mapping := node.get(keyword), dict):
                for child in mapping.values():
                    if isinstance(child, dict):
                        visit(child)
        for keyword in ('oneOf', 'anyOf', 'allOf'):
            if isinstance(branches := node.get(keyword), list):
                for child in branches:
                    if isinstance(child, dict):
                        visit(child)
        for keyword in ('items', 'additionalProperties'):
            if isinstance(child := node.get(keyword), dict):
                visit(child)

    visit(result)
    try:
        check_supported(result)
    except UnsupportedSchema:
        refuse_agent('Phase schema is not supported by the native validator.',
                                        code='agent_schema_unsupported')
    return result


async def _close_owned_transport(transport: Transport) -> None:
    # The pinned nare transports expose their owned clients without a transport
    # close method. OpenAI uses httpx2; Anthropic uses its async SDK client.
    client = getattr(transport, '_client', None)
    close = getattr(client, 'aclose', None) or getattr(client, 'close', None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


class _Execution:
    def __init__(self, request: AgentRequest) -> None:
        self.request = request
        self.session = new_session(request.task)
        self.slot = hashlib.sha256(json.dumps(asdict(request.identity), sort_keys=True).encode()).hexdigest()
        self.bridge = BudgetBridge(request.budget, phase=request.identity.phase,
                                   phase_key=self.slot, phase_limit=request.phase_max_calls)
        self.evidence_failures = EvidenceFailureBudget(self.bridge, request.evidence_tools)
        self.admitted: JsonObject | None = None
        self.status: Literal['accepted', 'held', 'cancelled', 'error'] = 'held'
        self.hold_code: str | None = None
        self.failed_check = False
        self.verified = False
        self.key = request.checkpoint_key
        self.events_seen: set[str] = set()
        self.terminal = False
        self.prerequisite_reminders: set[str] = set()
        self.free_reminder = False
        self.phase_expires_at = time.time() + max(0, min(request.budget.deadline,
            request.phase_deadline or request.budget.deadline) - time.monotonic())

    async def restore(self) -> None:
        req = self.request
        self.evidence_failures.check()
        if self.key is not None:
            if req.checkpoint_store is None:
                refuse_agent('Checkpoint storage is unavailable.', code='agent_checkpoint_unavailable')
            saved = await req.checkpoint_store.load_verified(self.key, req.identity)
            self._restore_authority(saved)
            if saved.terminal_status in {'held', 'error'} and saved.hold_code != 'provider_request_failed':
                refuse_agent('Saved phase has a terminal domain refusal.',
                    code=saved.hold_code or 'agent_checkpoint_terminal')
            await self._restore_tools(saved)
            self.session = loads(saved.session_text)
            if self.session.status in {'error', 'blocked'}:
                if saved.terminal_status == 'held' and saved.hold_code == 'provider_request_failed':
                    # Only the domain's resumable provider failure reopens the
                    # conversation; usage, messages and correction state stay.
                    self.session.status, self.session.error, self.session.stop_reason = 'working', None, None
                else:
                    refuse_agent('Saved phase has a terminal refusal.',
                        code=saved.hold_code or 'agent_checkpoint_terminal')
        self.verified = True

    def _restore_authority(self, saved: AgentCheckpoint) -> None:
        req = self.request
        if saved.phase_expires_at is not None:
            self.phase_expires_at = min(self.phase_expires_at, saved.phase_expires_at)
        elif req.phase_deadline is not None:
            refuse_agent('Saved phase deadline is unavailable.', code='agent_checkpoint_deadline')
        if time.time() >= self.phase_expires_at:
            refuse_agent('The original phase deadline expired.', code='phase_deadline')
        if not saved.prerequisite_reminders <= req.prerequisites:
            refuse_agent('Saved prerequisite policy differs.', code='agent_checkpoint_identity')
        self.prerequisite_reminders = set(saved.prerequisite_reminders)
        if any(value > getattr(req.budget, name) for name, value in saved.counters.items()):
            refuse_agent('Domain accounting is behind the checkpoint.', code='agent_checkpoint_accounting')
        if saved.phase_calls > req.budget.agent_phase_calls.get(self.slot, 0):
            refuse_agent('Phase accounting is behind the checkpoint.', code='agent_checkpoint_accounting')
        if not saved.evidence_error_receipts <= req.budget.agent_evidence_error_receipts:
            refuse_agent('Evidence-call accounting is behind the checkpoint.', code='agent_checkpoint_accounting')

    async def _restore_tools(self, saved: AgentCheckpoint) -> None:
        req = self.request
        if req.effects is not None:
            req.effects.restore(saved.effects)
        elif saved.effects:
            refuse_agent('Resumed action journal is missing.', code='agent_effect_unavailable')
        if req.tool_state is not None:
            await req.tool_state.restore(saved.tool_state)
        elif saved.tool_state:
            refuse_agent('Resumed tool state is missing.', code='agent_tool_state_unavailable')
        if req.evidence_catalog is not None:
            req.evidence_catalog.restore_receipt(saved.evidence)
        elif saved.evidence.delivered:
            refuse_agent('Resumed evidence catalog is missing.', code='agent_evidence_changed')

    async def save(self) -> None:
        req = self.request
        if not self.verified or req.checkpoint_store is None:
            return
        evidence = (req.evidence_catalog.receipt() if req.evidence_catalog is not None
                    else EvidenceReceipt(catalog_digest='0' * 64))
        saved = AgentCheckpoint(identity=req.identity,
            session_text=dumps(replace(self.session, error='agent_phase_held' if self.session.error else None)),
            evidence=evidence, counters={name: getattr(req.budget, name) for name in COUNTERS},
            phase_calls=req.budget.agent_phase_calls.get(self.slot, 0),
            tool_state=req.tool_state.snapshot() if req.tool_state is not None else {},
            prerequisite_reminders=frozenset(self.prerequisite_reminders),
            phase_expires_at=self.phase_expires_at,
            terminal_status=self.status if self.terminal else None,
            hold_code=self.hold_code if self.terminal else None,
            evidence_error_receipts=frozenset(req.budget.agent_evidence_error_receipts),
            effects=req.effects.receipts() if req.effects is not None else ())
        try:
            self.key = await req.checkpoint_store.save(saved)
            req.budget.agent_checkpoints[self.slot] = CheckpointReference(key=self.key, identity=req.identity)
            await self.bridge.persist()
        except EnvironmentDiscoveryError as exc:
            self.bridge.failure_code = exc.code
            raise

    async def diagnose(self, answer: object) -> list[JsonObject]:
        """Independent errors only: no admission, effects, or correction reservation."""
        if self.request.diagnose is None:
            return []
        try:
            issues = await self.request.diagnose(answer)
        except EnvironmentDiscoveryError as exc:
            self.hold_code = exc.code
            raise
        except Exception:
            self.failed_check = True
            raise
        self.request.budget.validation_codes.extend(issue.code for issue in issues)
        return [asdict(issue) for issue in issues]

    async def check(self, answer: JsonObject) -> list[JsonObject]:
        try:
            if self.request.effects is not None:
                await self.request.effects.ensure_settled()
            issues = await self.request.check(answer)
        except EnvironmentDiscoveryError as exc:
            self.hold_code = exc.code
            raise
        except Exception:
            self.failed_check = True
            raise
        self.admitted = answer if not issues else None
        self.hold_code = issues[0].code if issues else None
        self.request.budget.validation_codes.extend(issue.code for issue in issues)
        self.free_reminder = bool(issues and len(issues) == 1 and issues[0].code in self.request.prerequisites)
        if self.free_reminder:
            if issues[0].code in self.prerequisite_reminders:
                refuse_agent('Required prerequisite remains unexecuted.', code=issues[0].code)
            self.prerequisite_reminders.add(issues[0].code)
        if (issues and self.request.shard_corrections is None
                and not self.free_reminder
                and self.request.budget.schema_repairs >= self.request.budget.max_schema_repairs):
            self.hold_code = 'attempt_schema_limit'
            refuse_agent('Domain correction allowance exhausted.', code='attempt_schema_limit')
        return [asdict(issue) for issue in issues]

    async def record_correction(self, correction: JsonObject, *, reconcile: bool = False) -> None:
        budget = self.request.budget
        if self.free_reminder:
            self.free_reminder = False
        elif not correction['charged']:
            budget.validation_codes.append('correction_progress')
        elif self.request.shard_corrections is not None:
            budget.shard_schema_repairs += 1
        elif not reconcile:
            await self.bridge.correction()
            return
        elif budget.schema_repairs < budget.max_schema_repairs:
            # Completed correction, not new inference: account even on expiry.
            budget.schema_repairs += 1
        else:
            self.status, self.hold_code = 'held', 'attempt_schema_limit'
        await self.bridge.persist()

    async def event(self, event: Event) -> None:
        kind = 'progress'
        if 'schema_correction' in event.detail:
            await self.record_correction(event.detail['schema_correction'])
            kind = 'correction'
        elif 'compaction' in event.detail:
            self.request.budget.agent_compactions += 1
            kind = 'compaction'
        elif 'retry' in event.detail:
            self.request.budget.agent_retries += 1
            kind = 'retry'
        await self.evidence_failures.observe(event.detail, session_id=self.session.id, turn=self.session.turns)
        await self.emit(kind)

    async def emit(self, kind: str) -> None:
        budget = self.request.budget
        safe: JsonObject = {'kind': kind, 'phase': self.request.identity.phase, 'role': self.request.identity.role,
                            'turn': self.session.turns, 'calls': budget.calls, 'schema_repairs': budget.schema_repairs,
                            'compactions': budget.agent_compactions, 'retries': budget.agent_retries,
                            'usage_unknown': budget.agent_usage_unknown}
        safe['invalid_evidence_calls'] = budget.agent_evidence_errors
        if (evidence := self.request.evidence_catalog) is not None:
            safe.update(evidence_delivered=len(evidence.delivered), evidence_remaining=evidence.remaining_required)
        if kind == 'terminal':
            safe.update(status=self.status, hold_code=self.hold_code)
        key = json.dumps(safe, sort_keys=True)
        if key not in self.events_seen and self.request.event_sink is not None:
            self.events_seen.add(key)
            await self.request.event_sink(safe)

    def classify(self) -> None:
        if self.bridge.failure_code is not None:
            self.hold_code = self.bridge.failure_code
        elif self.session.status == 'done' and self.admitted is not None and self.session.output == self.admitted:
            self.status, self.hold_code = 'accepted', None
        elif self.failed_check:
            self.status, self.hold_code = 'error', 'agent_check_failed'
        elif self.session.stop_reason == 'schema_violation':
            self.hold_code = 'attempt_schema_limit'
        elif self.hold_code is None:
            self.hold_code = {'max_tokens': 'agent_output_truncated',
                              'refusal': 'agent_provider_refusal'}.get(self.session.stop_reason or '', 'agent_incomplete')

    def result(self) -> AgentResult:
        receipt = self.request.evidence_catalog.receipt() if self.request.evidence_catalog is not None else None
        return AgentResult(status=self.status, answer=self.admitted if self.status == 'accepted' else None,
            hold_code=self.hold_code, checkpoint_key=self.key,
            evidence_receipt=tuple(f'{receipt.catalog_digest}:{page}' for page in receipt.delivered) if receipt else (),
            usage={'input_tokens': self.session.usage.input, 'output_tokens': self.session.usage.output,
                   'cached_tokens': self.session.usage.cache_read, 'cache_write_tokens': self.session.usage.cache_write})
