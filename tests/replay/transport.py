"""A native transport that plays recorded replies in order (modelled on the 376 evidence tests' transports)."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from nare import Reply, ToolCall, Usage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tests.replay.fixtures import ReplayStep


class ReplayDiverged(AssertionError):  # noqa: N818 - the name the replay protocol uses
    """The harness asked for something the recording never answered: its request differs from the recorded one."""


class ReplayEnded(Exception):  # noqa: N818 - an end of input, not an error in the harness
    """Every recorded reply was played and nothing continues the session."""


class Continuation(Protocol):
    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply: ...


def reply_of(step: ReplayStep, number: int) -> Reply:
    """The recorded reply as nare's transport returns it. Call ids are regenerated; the recording's are not kept."""
    blocks: list[dict[str, Any]] = []
    text = step.reply.get('text')
    if 'candidate' in step.reply:
        text = json.dumps(step.reply['candidate'])
    if text is not None:
        blocks.append({'type': 'text', 'text': text})
    calls = [ToolCall(f'replay-{number}-{index}', call['name'], dict(call['args']))
             for index, call in enumerate(step.reply.get('tool_calls') or [])]
    blocks.extend({'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args} for call in calls)
    return Reply(blocks, calls, Usage(input=10, output=5), 'tool_use' if calls else 'end_turn')


class ReplayTransport:
    """Plays ``steps`` one per model request.

    A step's ``request`` (tool names and tool_choice as recorded) must match what the harness sends, or the replay
    raises ``ReplayDiverged``; the divergence is also kept, because the native runtime turns a transport exception
    into a held session. After the last step, ``then`` answers; without it the replay raises ``ReplayEnded``.
    """

    required_tool_choice = 'required'

    def __init__(self, steps: Sequence[ReplayStep], *, then: Continuation | None = None) -> None:
        self.steps = list(steps)
        self.then = then
        self.position = 0
        self.requests: list[list[Any]] = []
        self.offered: list[tuple[frozenset[str], str | None]] = []
        self.diverged: ReplayDiverged | None = None

    async def context_window(self) -> int:
        return 262144

    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
        self.requests.append(list(messages))
        names = frozenset(str(tool['name']) for tool in tools)
        self.offered.append((names, tool_choice))
        if self.position >= len(self.steps):
            if self.then is None:
                raise ReplayEnded(f'all {len(self.steps)} recorded replies were played')
            return await self.then.turn(messages, tools, tool_choice=tool_choice)
        step = self.steps[self.position]
        if step.request is not None:
            expected = (frozenset(step.request.get('tools') or ()), step.request.get('tool_choice'))
            if (names, tool_choice) != expected:
                self.diverged = ReplayDiverged(
                    f'{step.call}: harness offered {sorted(names)} with tool_choice {tool_choice!r}; the recording '
                    f'answered {sorted(expected[0])} with {expected[1]!r}')
                raise self.diverged
        self.position += 1
        return reply_of(step, self.position)

    def assert_followed(self) -> None:
        """Every recorded reply was played, and the harness never diverged from the recording."""
        if self.diverged is not None:
            raise self.diverged
        if self.position != len(self.steps):
            message = f'only {self.position} of {len(self.steps)} recorded replies were requested'
            raise ReplayDiverged(message)
