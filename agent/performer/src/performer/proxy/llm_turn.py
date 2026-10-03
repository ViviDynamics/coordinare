"""080 — canonical, format-neutral representation of one LLM turn.

The proxy accepts a request in the CLI's wire format (Anthropic ``/v1/messages``
or OpenAI ``/v1/chat/completions``), normalizes it to an ``LLMRequest``, runs the
orchestration strategy against ``Upstream`` clients (which speak ``LLMRequest`` ↔
their own wire format), and renders the merged ``LLMResponse`` back to the CLI's
format. Keeping one internal representation avoids N×N format translation.

These are plain dataclasses — the internal rep needs no validation; parsing and
validation happen in the wire adapters (``upstreams`` / ``assembler``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

# Roles are kept as plain strings (the two wire formats agree on
# system/user/assistant/tool); adapters map any format-specific quirks.
Role = str


@dataclass(frozen=True)
class ToolSchema:
    """A tool definition offered to the model (name + JSON-schema parameters)."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation emitted by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    """One conversation message in the canonical representation."""

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # set on role == "tool" results
    name: str | None = None

    @staticmethod
    def system(text: str) -> Message:
        return Message(role="system", content=text)

    @staticmethod
    def user(text: str) -> Message:
        return Message(role="user", content=text)

    @staticmethod
    def assistant(text: str = "", tool_calls: tuple[ToolCall, ...] = ()) -> Message:
        return Message(role="assistant", content=text, tool_calls=tool_calls)

    @property
    def is_error_tool_result(self) -> bool:
        """Heuristic flag set by adapters when a tool result carries failure."""
        return self.role == "tool" and bool(self._is_error)

    # adapters may stamp this; default False. Kept out of the constructor so the
    # think_once error-marker detection (strategies) can also compute it itself.
    _is_error: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class LLMRequest:
    """A normalized chat-completion request."""

    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...] = ()
    stream: bool = False
    generation: dict[str, Any] = field(default_factory=dict)

    def without_tools(self) -> LLMRequest:
        """Return a copy with tools hidden — used for the think (planner) phase."""
        return replace(self, tools=())

    def with_system_prepended(self, text: str) -> LLMRequest:
        """Return a copy with a system message prepended (FR-012 plan injection).

        Inserted ahead of the existing conversation; existing user/assistant
        turns are left unchanged.
        """
        return replace(self, messages=(Message.system(text), *self.messages))

    @property
    def last_message(self) -> Message | None:
        return self.messages[-1] if self.messages else None


@dataclass(frozen=True)
class LLMResponse:
    """A normalized chat-completion response."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning: str | None = None  # planner/thinking text (pre-merge)
    raw: dict[str, Any] | None = None  # original upstream body, when useful

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class StreamDelta:
    """One canonical incremental fragment of an act-phase completion (FR-014).

    ``Upstream.complete_stream`` produces these; the front-door SSE writers
    consume them. Optional fields are only meaningful for some kinds:

    - ``text``/``reasoning`` carry ``text``
    - ``tool`` carries ``tool_index`` plus ``tool_id``/``tool_name`` on the
      first fragment of a call and ``args_fragment`` on argument fragments
    - ``finish`` carries ``finish_reason``
    - ``envelope`` rides the upstream's own chunk metadata (id/created/model)
      when its wire carries one, so front-door writers can echo it
    """

    kind: str  # "text" | "reasoning" | "tool" | "finish"
    text: str = ""
    tool_index: int | None = None
    tool_id: str | None = None
    tool_name: str | None = None
    args_fragment: str = ""
    finish_reason: str | None = None
    envelope: dict[str, Any] | None = None
