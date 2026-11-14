"""Account for physical requests before inference and never replay token charges."""
from __future__ import annotations

import inspect
import json
from contextlib import aclosing, asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol, cast

from nare import Reply

from coordinare.services.conductor_calls import NULL_CALL_LOG, ModelReply, ModelRequest, ToolCall
from coordinare.services.environment_attempt import DiscoveryChatReply, EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    from nare import Transport
    from nare.session import Message
    from nare.transport import Delta

    from coordinare.services.agent_runtime.configuration import AgentModelConfig
    from coordinare.services.conductor_calls import CallRecord, ConductorCallLog
    from coordinare.services.environment_attempt import AttemptBudget


def usage_of(reply: Reply) -> dict[str, int]:
    return {'input_tokens': reply.usage.input, 'output_tokens': reply.usage.output,
            'cache_read': reply.usage.cache_read, 'cache_write': reply.usage.cache_write}


class BudgetBridge:
    def __init__(self, budget: AttemptBudget, *, phase: str, phase_key: str | None = None,
                 phase_limit: int | None = None) -> None:
        self.budget = budget
        self.phase = phase
        self.phase_key = phase_key
        self.phase_limit = phase_limit
        self.failure_code: str | None = None

    async def persist(self) -> None:
        # Persist terminal accounting even after expiry. This performs no new
        # inference or external tool work; flush() remains the pre-effect gate.
        if self.budget.checkpoint is not None:
            try:
                await self.budget.checkpoint()
            except EnvironmentDiscoveryError as exc:
                self.failure_code = exc.code
                raise
            except Exception:
                self.failure_code = 'attempt_persistence'
                message = 'Domain accounting could not be persisted.'
                raise EnvironmentDiscoveryError(message, code='attempt_persistence') from None

    async def reserve(self) -> str:
        try:
            if (self.phase_key is not None and self.phase_limit is not None
                    and self.budget.agent_phase_calls.get(self.phase_key, 0) >= self.phase_limit):
                raise EnvironmentDiscoveryError('Native phase call allowance exhausted.', code='phase_call_limit')
            self.budget.reserve_call()
            if self.phase_key is not None:
                self.budget.agent_phase_calls[self.phase_key] = self.budget.agent_phase_calls.get(self.phase_key, 0) + 1
            await self.persist()
            self.budget.check()
        except EnvironmentDiscoveryError as exc:
            self.failure_code = exc.code
            raise
        return str(self.budget.calls)

    async def record_reply(self, receipt: str, reply: Reply) -> None:
        if receipt in self.budget.agent_usage_receipts:
            return
        self.budget.agent_usage_receipts.add(receipt)
        if reply.usage.total_tokens == 0:
            self.budget.agent_usage_unknown += 1
        self.budget.record_reply(DiscoveryChatReply(text='', usage=usage_of(reply) if reply.usage.total_tokens else None,
            finish_reason=reply.stop_reason), phase=self.phase)
        await self.persist()

    async def record_unknown(self, receipt: str) -> None:
        if receipt not in self.budget.agent_usage_receipts:
            self.budget.agent_usage_receipts.add(receipt)
            self.budget.agent_usage_unknown += 1
            await self.persist()

    async def correction(self) -> None:
        try:
            self.budget.reserve_repair('schema')
            await self.persist()
            self.budget.check()
        except EnvironmentDiscoveryError as exc:
            self.failure_code = exc.code
            raise


class ExtendedTransport(Protocol):
    async def turn(self, messages: list[Message], tools: list[dict[str, Any]], **kwargs: Any) -> Reply: ...
    def stream_turn(self, messages: list[Message], tools: list[dict[str, Any]], **kwargs: Any) -> AsyncGenerator[Delta | Reply, None]: ...


class BudgetTransport:
    """Nare retries re-enter this seam, so every physical request is reserved."""

    def __init__(self, inner: Transport, bridge: BudgetBridge, *, session_id: str,
                 before_send: Callable[[], Awaitable[None]], calls: ConductorCallLog | None = None,
                 config: AgentModelConfig | None = None, instructions: str = '',
                 max_output_bytes: int | None = None, max_context_bytes: int | None = None,
                 answer_schema: dict[str, Any] | None = None) -> None:
        self.inner = inner
        self.bridge = bridge
        self.session_id = session_id
        self.before_send = before_send
        self.calls = calls or NULL_CALL_LOG
        self.config = config
        self.instructions = instructions
        self.max_output_bytes = max_output_bytes
        self.max_context_bytes = max_context_bytes
        self.answer_schema = answer_schema
        self.streaming = bool(getattr(inner, 'streaming', False))
        self.supports_structured_output = bool(getattr(inner, 'supports_structured_output', False))

    async def context_window(self) -> int | None:
        self.bridge.budget.check()
        window = await self.inner.context_window()
        if type(window) is not int or window <= 0:
            self.bridge.failure_code = 'agent_context_unavailable'
            raise EnvironmentDiscoveryError('Configured model context window is unavailable.',
                                            code=self.bridge.failure_code)
        return window

    def declared_concurrency(self) -> int | None:
        hook = getattr(self.inner, 'declared_concurrency', None)
        return hook() if callable(hook) else None

    @asynccontextmanager
    async def _record(self, messages: list[Message], tools: list[dict[str, Any]]) -> AsyncIterator[tuple[str, CallRecord]]:
        size = len(json.dumps({'system': self.instructions, 'messages': [
            {'role': message.role, 'content': message.content} for message in messages],
            'tools': tools, 'schema': self.answer_schema}, ensure_ascii=False, separators=(',', ':')).encode())
        if self.max_context_bytes is not None and size > self.max_context_bytes:
            self.bridge.failure_code = self.bridge.phase + '_context_limit'
            raise EnvironmentDiscoveryError('Complete native context exceeds its bound.', code=self.bridge.failure_code)
        self.bridge.budget.context_bytes += size
        receipt = self.session_id + ':' + await self.bridge.reserve()
        await self.before_send()
        self.bridge.budget.check()
        async with self.calls.call('environment_' + self.bridge.phase) as record:
            record.request = ModelRequest(model=self.config.model if self.config else 'injected',
                endpoint=self.config.provider if self.config else 'injected', system=self.instructions,
                prompt=json.dumps([{'role': message.role, 'content': message.content} for message in messages]),
                parameters=({'max_tokens': self.config.max_tokens, 'temperature': self.config.temperature,
                             'effort': self.config.effort} if self.config else {}))
            try:
                yield receipt, record
            except BaseException as exc:
                record.error = type(exc).__name__
                if self.bridge.failure_code is None:
                    self.bridge.failure_code = 'provider_request_failed'
                await self.bridge.record_unknown(receipt)
                raise

    @staticmethod
    def _parameters(method: Any, response_schema: dict[str, Any] | None, tool_choice: str | None) -> dict[str, Any]:
        desired = {key: value for key, value in {'response_schema': response_schema, 'tool_choice': tool_choice}.items()
                   if value is not None}
        if desired:
            parameters = inspect.signature(method).parameters
            if (not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
                    and not desired.keys() <= parameters.keys()):
                message = 'Native transport cannot preserve the requested response constraints.'
                raise EnvironmentDiscoveryError(message, code='agent_transport_unsupported')
        return desired

    async def _reply(self, receipt: str, record: CallRecord, reply: Reply) -> None:
        self.bridge.failure_code = None
        record.reply = ModelReply(text=''.join(str(block.get('text', '')) for block in reply.content
                                               if block.get('type') == 'text'),
            usage=usage_of(reply) if reply.usage.total_tokens else None, finish_reason=reply.stop_reason,
            tool_calls=tuple(ToolCall(name=call.name, call_id=call.id, input=call.args) for call in reply.tool_calls))
        record.decide('model reply received; domain admission is separate')
        await self.bridge.record_reply(receipt, reply)
        size = len(record.reply.text.encode()) + sum(len(json.dumps(call.args, ensure_ascii=False).encode())
                                                    for call in reply.tool_calls)
        if self.max_output_bytes is not None and size > self.max_output_bytes:
            self.bridge.failure_code = 'phase_output_limit'
            raise EnvironmentDiscoveryError('Native output exceeds its bound.', code='phase_output_limit')

    async def turn(self, messages: list[Message], tools: list[dict[str, Any]], *,
                   response_schema: dict[str, Any] | None = None, tool_choice: str | None = None) -> Reply:
        kwargs = self._parameters(self.inner.turn, response_schema, tool_choice)
        async with self._record(messages, tools) as (receipt, record):
            reply = await cast('ExtendedTransport', self.inner).turn(messages, tools, **kwargs)
            await self._reply(receipt, record, reply)
            return reply

    async def stream_turn(self, messages: list[Message], tools: list[dict[str, Any]], *,
                          response_schema: dict[str, Any] | None = None,
                          tool_choice: str | None = None) -> AsyncIterator[Delta | Reply]:
        inner = cast('ExtendedTransport', self.inner)
        kwargs = self._parameters(inner.stream_turn, response_schema, tool_choice)
        async with (self._record(messages, tools) as (receipt, record),
                    aclosing(inner.stream_turn(messages, tools, **kwargs)) as stream):
            async for item in stream:
                if isinstance(item, Reply):
                    await self._reply(receipt, record, item)
                yield item
