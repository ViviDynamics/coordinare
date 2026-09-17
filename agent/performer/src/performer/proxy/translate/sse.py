"""OpenAI ``/v1/chat/completions`` streaming → Anthropic ``/v1/messages``
streaming response translation (spec 084, FR-003 / FR-004 / FR-005).

:class:`TranslatingSSEFilter` subclasses
:class:`proxy.normalizers.base.StatefulSSEFilter`, inheriting the ``\\n\\n``
chunk-boundary buffering so it never sees (or emits) a torn frame (FR-003). It is
appended to the ``_FilterChain`` **after** the normalizer filters, so it consumes
an already-reassembled, structured OpenAI ``chat.completion.chunk`` stream and
restructures it into the Anthropic event protocol:

    message_start
      → content_block_start / content_block_delta (repeats) / content_block_stop
      → message_delta → message_stop

Text deltas stream per upstream chunk (one ``content_block_delta`` per
``delta.content`` chunk) — incremental delivery is preserved rather than
coalesced. ``tool_use`` blocks stream the OpenAI ``tool_calls`` argument
fragments as Anthropic ``input_json_delta`` deltas (US2, T017): the first
fragment of a call opens a ``content_block_start`` (type ``tool_use``, carrying
``id``/``name``); each subsequent ``function.arguments`` chunk is an
``input_json_delta`` (``partial_json``); a block switch closes the prior block
first. Content-block indices increment per opened block.

The terminal ``finish_reason`` is captured and mapped through the shared
:func:`proxy.translate.finish_reason.map_finish_reason` helper — the single
source of truth shared with the non-streaming path (Decision 6) — onto the
closing ``message_delta.delta.stop_reason``.

Pure: no network/disk I/O and no logging of body/token content (FR-011).
"""

from __future__ import annotations

from ..normalizers.base import StatefulSSEFilter
from .finish_reason import map_finish_reason


class TranslatingSSEFilter(StatefulSSEFilter):
    """Restructure an OpenAI chat-completion SSE stream into Anthropic events.

    One filter instance handles exactly one response stream; all state is
    per-stream and discarded when the stream ends.
    """

    def __init__(self) -> None:
        super().__init__()
        self._emitted_message_start = False
        self._emitted_stop = False
        self._captured_finish_reason: str | None = None
        self._message_id: str | None = None
        self._model: str | None = None
        # Content blocks open lazily and sequentially; only one is open at a
        # time (Anthropic streams one block start→stop before the next). We
        # assign incrementing block indices and remember the currently-open one
        # so a text→tool / tool→tool switch can close the prior block first.
        self._next_block_index = 0
        self._open_block_index: int | None = None
        self._open_block_kind: str | None = None  # "text" | "tool"
        # Map an OpenAI ``tool_calls[].index`` to the Anthropic block index it
        # opened, so argument-fragment chunks (which omit id/name) land on the
        # right block.
        self._tool_block_for: dict[int, int] = {}

    # -- frame transform ---------------------------------------------------- #

    def process_frame(self, frame: bytes) -> bytes | None:
        _event_name, payload = self.parse_frame(frame)

        # ``data: [DONE]`` and any non-JSON frame parse to ``payload is None``.
        # ``[DONE]`` is the terminal sentinel → flush and close the message.
        if payload is None:
            return self._join(self._emit_closing())

        out: list[bytes] = []

        if not self._emitted_message_start:
            self._message_id = payload.get("id") or self._message_id
            self._model = payload.get("model") or self._model
            out.append(self._message_start_frame())
            self._emitted_message_start = True

        choice = (payload.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}

        text = delta.get("content")
        if text:
            if self._open_block_kind != "text":
                self._close_open_block(out)
                self._open_text_block(out)
            out.append(self._text_delta_frame(self._open_block_index, text))

        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                self._process_tool_call_delta(tool_call, out)

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            self._captured_finish_reason = finish_reason

        return self._join(out)

    # -- block lifecycle ---------------------------------------------------- #

    def _open_text_block(self, out: list[bytes]) -> None:
        index = self._next_block_index
        self._next_block_index += 1
        self._open_block_index = index
        self._open_block_kind = "text"
        out.append(self._content_block_start_text_frame(index))

    def _process_tool_call_delta(
        self, tool_call: dict, out: list[bytes],
    ) -> None:
        call_index = tool_call.get("index", 0)
        function = tool_call.get("function") or {}

        # First fragment of a call carries id/name → open a new tool_use block.
        if call_index not in self._tool_block_for:
            self._close_open_block(out)
            index = self._next_block_index
            self._next_block_index += 1
            self._tool_block_for[call_index] = index
            self._open_block_index = index
            self._open_block_kind = "tool"
            out.append(
                self._content_block_start_tool_frame(
                    index, tool_call.get("id"), function.get("name"),
                ),
            )

        index = self._tool_block_for[call_index]
        arguments = function.get("arguments")
        if arguments:
            out.append(self._input_json_delta_frame(index, arguments))

    def _close_open_block(self, out: list[bytes]) -> None:
        if self._open_block_index is not None:
            out.append(self._content_block_stop_frame(self._open_block_index))
            self._open_block_index = None
            self._open_block_kind = None

    def flush(self) -> bytes:
        """Close the message if the upstream stream ended without ``[DONE]``.

        In the normal path ``[DONE]`` already emitted the closing events and set
        :attr:`_emitted_stop`, so this is a no-op. Any malformed trailing partial
        frame from the base buffer is dropped rather than forwarded as raw
        OpenAI-wire bytes to the Anthropic CLI.
        """
        super().flush()  # discard any partial trailing bytes
        if self._emitted_message_start and not self._emitted_stop:
            closing = self._join(self._emit_closing())
            if closing is not None:
                return closing + b"\n\n"
        return b""

    # -- closing sequence --------------------------------------------------- #

    def _emit_closing(self) -> list[bytes]:
        if self._emitted_stop:
            return []
        frames: list[bytes] = []
        self._close_open_block(frames)
        frames.append(self._message_delta_frame())
        frames.append(self._message_stop_frame())
        self._emitted_stop = True
        return frames

    # -- Anthropic event builders ------------------------------------------- #

    def _message_start_frame(self) -> bytes:
        return self.encode_frame(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self._message_id or "msg_translated",
                    "type": "message",
                    "role": "assistant",
                    "model": self._model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def _content_block_start_text_frame(self, index: int) -> bytes:
        return self.encode_frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            },
        )

    def _content_block_start_tool_frame(
        self, index: int, tool_id: str | None, name: str | None,
    ) -> bytes:
        return self.encode_frame(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {},
                },
            },
        )

    def _text_delta_frame(self, index: int, text: str) -> bytes:
        return self.encode_frame(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    def _input_json_delta_frame(self, index: int, partial_json: str) -> bytes:
        return self.encode_frame(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": partial_json},
            },
        )

    def _content_block_stop_frame(self, index: int) -> bytes:
        return self.encode_frame(
            "content_block_stop",
            {"type": "content_block_stop", "index": index},
        )

    def _message_delta_frame(self) -> bytes:
        return self.encode_frame(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": map_finish_reason(self._captured_finish_reason),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 0},
            },
        )

    def _message_stop_frame(self) -> bytes:
        return self.encode_frame("message_stop", {"type": "message_stop"})

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _join(frames: list[bytes]) -> bytes | None:
        """Join emitted frames with the SSE blank-line separator.

        The base :meth:`feed` appends the trailing ``\\n\\n``, so frames are
        joined with a single ``\\n\\n`` here. Returns ``None`` (drop) when this
        upstream frame produced no Anthropic events.
        """
        if not frames:
            return None
        return b"\n\n".join(frames)


# Backwards/forwards-compat alias: the contract and __init__ docstring refer to
# the "translate SSE StatefulSSEFilter subclass" generically.
TranslateSSEFilter = TranslatingSSEFilter
