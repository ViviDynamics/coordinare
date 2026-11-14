"""Native conversations bounded by Coordinare admission, accounting and persistence."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from contextlib import aclosing
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Literal

from nare import Policy, dumps, loads, new_session, run
from nare.loop import RetryPolicy

from coordinare.services.agent_runtime.accounting import BudgetBridge, BudgetTransport
from coordinare.services.agent_runtime.checkpoint import (
    COUNTERS,
    AgentCheckpoint,
    CheckpointReference,
    EvidenceReceipt,
)
from coordinare.services.agent_runtime.models import AgentResult
from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
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
            await execution.restore()
            if transport is None:
                assert self._config is not None
                transport = self._config.transport(request.instructions)
            wrapped = BudgetTransport(transport, execution.bridge, session_id=execution.session.id,
                before_send=execution.save, calls=self._calls, config=self._config, instructions=request.instructions)
            names = frozenset(tool.name for tool in request.allowed_tools)
            deadline = min(request.budget.deadline, request.phase_deadline or request.budget.deadline)
            async with asyncio.timeout_at(deadline):
                events = run(execution.session, transport=wrapped,
                    policy=Policy(tools=names, pending=names), tools=request.allowed_tools,
                    schema=request.answer_schema, check=execution.check,
                    max_turns=max(1, request.budget.max_calls - request.budget.calls),
                    max_corrections=max(1, execution.session.schema_corrections
                        + request.budget.max_schema_repairs - request.budget.schema_repairs),
                    retry=self._retry, compaction='elide', compaction_trigger=0.6, compaction_target=0.4)
                async with aclosing(events):
                    async for event in events:
                        await execution.event(event)
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
            try:
                await execution.save()
                await execution.emit('terminal')
            except Exception:
                execution.status, execution.hold_code = 'held', 'agent_checkpoint_persistence'
            if self._config is not None and transport is not None:
                await _close_owned_transport(transport)
        return execution.result()


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
        self.admitted: JsonObject | None = None
        self.status: Literal['accepted', 'held', 'cancelled', 'error'] = 'held'
        self.hold_code: str | None = None
        self.failed_check = False
        self.verified = False
        self.key = request.checkpoint_key
        self.events_seen: set[str] = set()

    async def restore(self) -> None:
        req = self.request
        if self.key is not None:
            if req.checkpoint_store is None:
                raise EnvironmentDiscoveryError('Checkpoint storage is unavailable.', code='agent_checkpoint_unavailable')
            saved = await req.checkpoint_store.load_verified(self.key, req.identity)
            if any(value > getattr(req.budget, name) for name, value in saved.counters.items()):
                raise EnvironmentDiscoveryError('Domain accounting is behind the checkpoint.', code='agent_checkpoint_accounting')
            if saved.phase_calls > req.budget.agent_phase_calls.get(self.slot, 0):
                raise EnvironmentDiscoveryError('Phase accounting is behind the checkpoint.', code='agent_checkpoint_accounting')
            if req.evidence_catalog is not None:
                req.evidence_catalog.restore_receipt(saved.evidence)
            elif saved.evidence.delivered:
                raise EnvironmentDiscoveryError('Resumed evidence catalog is missing.', code='agent_evidence_changed')
            self.session = loads(saved.session_text)
        self.verified = True

    async def save(self) -> None:
        req = self.request
        if not self.verified or req.checkpoint_store is None:
            return
        evidence = (req.evidence_catalog.receipt() if req.evidence_catalog is not None
                    else EvidenceReceipt(catalog_digest='0' * 64))
        saved = AgentCheckpoint(identity=req.identity,
            session_text=dumps(replace(self.session, error='agent_phase_held' if self.session.error else None)),
            evidence=evidence, counters={name: getattr(req.budget, name) for name in COUNTERS},
            phase_calls=req.budget.agent_phase_calls.get(self.slot, 0))
        try:
            self.key = await req.checkpoint_store.save(saved)
            req.budget.agent_checkpoints[self.slot] = CheckpointReference(key=self.key, identity=req.identity)
            await self.bridge.persist()
        except EnvironmentDiscoveryError as exc:
            self.bridge.failure_code = exc.code
            raise

    async def check(self, answer: JsonObject) -> list[JsonObject]:
        try:
            issues = await self.request.check(answer)
        except EnvironmentDiscoveryError as exc:
            self.hold_code = exc.code
            raise
        except Exception:
            self.failed_check = True
            raise
        self.admitted = answer if not issues else None
        self.hold_code = issues[0].code if issues else None
        if issues and self.request.budget.schema_repairs >= self.request.budget.max_schema_repairs:
            raise EnvironmentDiscoveryError('Domain correction allowance exhausted.', code=issues[0].code)
        return [asdict(issue) for issue in issues]

    async def event(self, event: Event) -> None:
        kind = 'progress'
        if 'schema_correction' in event.detail:
            await self.bridge.correction()
            kind = 'correction'
        elif 'compaction' in event.detail:
            self.request.budget.agent_compactions += 1
            kind = 'compaction'
        elif 'retry' in event.detail:
            self.request.budget.agent_retries += 1
            kind = 'retry'
        await self.emit(kind)

    async def emit(self, kind: str) -> None:
        budget = self.request.budget
        safe: JsonObject = {'kind': kind, 'phase': self.request.identity.phase, 'role': self.request.identity.role,
                            'turn': self.session.turns, 'calls': budget.calls, 'schema_repairs': budget.schema_repairs,
                            'compactions': budget.agent_compactions, 'retries': budget.agent_retries,
                            'usage_unknown': budget.agent_usage_unknown}
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
        elif self.hold_code is None:
            self.hold_code = 'attempt_schema_limit' if self.session.stop_reason == 'schema_violation' else 'agent_incomplete'

    def result(self) -> AgentResult:
        return AgentResult(status=self.status, answer=self.admitted if self.status == 'accepted' else None,
            hold_code=self.hold_code, checkpoint_key=self.key,
            usage={'input_tokens': self.session.usage.input, 'output_tokens': self.session.usage.output,
                   'cached_tokens': self.session.usage.cache_read, 'cache_write_tokens': self.session.usage.cache_write})
