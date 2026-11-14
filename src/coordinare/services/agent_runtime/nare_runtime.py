"""Run native conversations and require completed domain admission for success."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

from nare import Policy, new_session, run
from nare.loop import RetryPolicy

from coordinare.services.agent_runtime.models import AgentResult
from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from nare import Transport

    from coordinare.services.agent_runtime.configuration import AgentModelConfig
    from coordinare.services.agent_runtime.models import AgentRequest, JsonObject


class NareRuntime:
    """Each invocation starts an independent role; no host tools are registered."""

    def __init__(self, *, transport: Transport | None = None, config: AgentModelConfig | None = None) -> None:
        if (transport is None) == (config is None):
            message = 'Supply one configured model or injected transport.'
            raise ValueError(message)
        self._transport = transport
        self._config = config

    async def run(self, request: AgentRequest) -> AgentResult:
        session = new_session(request.task)
        admitted: JsonObject | None = None
        hold_code: str | None = None
        failed_check = False

        async def check(answer: JsonObject) -> list[JsonObject]:
            nonlocal admitted, hold_code, failed_check
            try:
                issues = await request.check(answer)
            except Exception:
                failed_check = True
                raise
            admitted = answer if not issues else None
            hold_code = issues[0].code if issues else None
            if issues and request.budget.schema_repairs >= request.budget.max_schema_repairs:
                raise EnvironmentDiscoveryError('Domain correction allowance exhausted.', code=issues[0].code)
            return [asdict(issue) for issue in issues]

        transport = self._transport
        if transport is None:
            assert self._config is not None
            transport = self._config.transport(request.instructions)
        names = frozenset(tool.name for tool in request.allowed_tools)
        status: Literal['accepted', 'held', 'cancelled', 'error'] = 'held'
        try:
            request.budget.check()
            async with asyncio.timeout_at(request.budget.deadline):
                async for _event in run(
                    session, transport=transport, policy=Policy(tools=names, pending=names),
                    tools=request.allowed_tools, schema=request.answer_schema, check=check,
                    max_turns=max(1, request.budget.max_calls - request.budget.calls),
                    max_corrections=max(1, request.budget.max_schema_repairs - request.budget.schema_repairs),
                    retry=RetryPolicy(retries=0), compaction='elide',
                    compaction_trigger=0.6, compaction_target=0.4,
                ):
                    pass
            if session.status == 'done' and admitted is not None and session.output == admitted:
                status = 'accepted'
            elif failed_check:
                status, hold_code = 'error', 'agent_check_failed'
            elif hold_code is None:
                hold_code = 'agent_incomplete'
        except TimeoutError:
            hold_code = 'attempt_deadline'
        except EnvironmentDiscoveryError as exc:
            hold_code = exc.code
        except asyncio.CancelledError:
            raise
        except Exception:
            status, hold_code = 'error', 'agent_runtime_failed'
        return AgentResult(status=status, answer=admitted if status == 'accepted' else None,
                           hold_code=hold_code, checkpoint_key=request.checkpoint_key,
                           usage={'input_tokens': session.usage.input, 'output_tokens': session.usage.output,
                                  'cached_tokens': session.usage.cache_read, 'cache_write_tokens': session.usage.cache_write})
