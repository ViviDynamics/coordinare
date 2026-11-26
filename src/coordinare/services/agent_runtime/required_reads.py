"""Runtime-selected required reads, independent of repository technology."""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from nare import Tool

from coordinare.services.agent_runtime.errors import refuse_agent
from coordinare.services.agent_runtime.request_stage import RequestSelection

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

NAME = 'continue_required_reads'
REQUIRED_READ_POLICY = 'required-reads-v1:packet,requested-sources,observations'


class RequiredReads:
    def __init__(self, next_read: Callable[[], Callable[[], Awaitable[str]] | None],
                 check_budget: Callable[[], None]) -> None:
        self.next_read, self.check_budget = next_read, check_budget
        self.tool_choice: str | None = None
        # nare-native-042: on a forced first turn a shard author, told about review and answer tools it did not yet
        # have, put its answer's fields into this call.
        self.tool = Tool(name=NAME, description='Read the next runtime-selected required batch. Call with {} and no '
            'arguments. Until every required read finishes this is your only tool; the tools and answer your '
            'instructions describe become available afterwards, so never put their arguments or your answer here.',
            input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False}, call=self.read)

    def validate_transport(self, transport: object) -> None:
        # Do not translate a provider's tool-choice spelling or disable thinking
        # implicitly. Only combinations covered by the wire contract are enabled.
        # Provider SDKs are intentionally lazy: importing them in daemon startup
        # doubles cold-start cost even when no bootstrap is running.
        anthropic = import_module('nare.transport.anthropic').AnthropicTransport
        openai = import_module('nare.transport.openai').OpenAITransport
        if isinstance(transport, anthropic):
            if transport.thinking is not None or transport.streaming:
                refuse_agent('Required reads do not support this Anthropic mode.', code='agent_required_tools_unsupported')
            self.tool_choice = 'any'
        elif isinstance(transport, openai) or getattr(transport, 'required_tool_choice', None) == 'required':
            self.tool_choice = 'required'
        else:
            refuse_agent('Transport required-tool semantics are not established.', code='agent_required_tools_unsupported')

    async def read(self, arguments: dict[str, Any]) -> str:
        self.check_budget()
        if arguments:
            refuse_agent('Required-read continuation accepts only {}.', code='invalid_tool_arguments')
        read = self.next_read()
        return await read() if read is not None else '{"pages":[],"unread_count":0,"next_unread":null}'

    def select(self, tools: list[dict[str, Any]], schema: dict[str, Any] | None,
               mode: str | None, choice: str | None) -> RequestSelection:
        if self.tool_choice is None:
            refuse_agent('Required-read transport capability was not verified.', code='agent_required_tools_unsupported')
        if choice == 'none':
            return RequestSelection(tools, schema, mode, choice, frozenset())
        if self.next_read() is not None:
            tool = self.tool
            return RequestSelection([{'name': tool.name, 'description': tool.description,
                                      'input_schema': tool.input_schema}], tool_choice=self.tool_choice,
                                    allowed_calls=frozenset({NAME}))
        available = [tool for tool in tools if tool['name'] != NAME]
        # Final encoded answers can use a provider's synthetic answer function;
        # nare's transport converts it into the declared answer, not an action.
        return RequestSelection(available, schema, mode, choice,
                                frozenset(tool['name'] for tool in available))
