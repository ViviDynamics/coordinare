"""Shared I/O helpers for performer backends."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

# Timeout argument: fixed seconds, None (no timeout), or a zero-arg callable
# that returns a fresh timeout each chunk-read (used by watchdogs whose budget
# decreases over wall-clock time).
TimeoutSource = float | None | Callable[[], float | None]


async def iter_lines_chunked(
    stream: asyncio.StreamReader,
    *,
    chunk_size: int = 65536,  # 64 KiB — large enough that typical
    # stream-json events fit in one read, small enough to bound the
    # buffer's per-read growth. Independent of any per-line cap: this
    # impl reads bytes (not lines), so oversized events stream through
    # intact (unlike asyncio.StreamReader.readline()'s default limit).
    timeout: TimeoutSource = None,
    on_chunk: Callable[[bytes], None] | None = None,
) -> AsyncIterator[bytes]:
    """Yield newline-terminated lines from a StreamReader via chunked reads.

    Avoids ``asyncio.StreamReader.readline()``'s default 64 KiB per-line limit
    (which raises ``LimitOverrunError`` on overflow). Reads into an internal
    buffer in fixed-size chunks and splits on ``\\n``; trailing partial bytes
    at EOF are yielded as a final line.

    Each yielded value is the line bytes WITHOUT the trailing newline. Empty
    lines (consecutive newlines) ARE yielded — the caller filters if needed.

    Timeouts apply per chunk-read, not per line. A timeout firing raises
    ``asyncio.TimeoutError`` out of the iterator; callers wrap the
    ``async for`` in ``try/except`` if they need to react.

    ``on_chunk`` (optional) is invoked with each raw chunk read from the stream
    BEFORE line-splitting, including any embedded newlines. Use this to mirror
    bytes to a capture file/buffer without re-encoding the lines. Exceptions
    from the callback propagate out of the iterator.
    """
    buffer = bytearray()
    while True:
        t = timeout() if callable(timeout) else timeout
        if t is not None:
            chunk = await asyncio.wait_for(stream.read(chunk_size), timeout=t)
        else:
            chunk = await stream.read(chunk_size)
        if not chunk:
            if buffer:
                yield bytes(buffer)
                buffer.clear()
            return
        if on_chunk is not None:
            on_chunk(chunk)
        buffer.extend(chunk)
        while True:
            nl = buffer.find(b"\n")
            if nl < 0:
                break
            yield bytes(buffer[:nl])
            del buffer[: nl + 1]
