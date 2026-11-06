"""A streamed model reply that stops sending is retried after minutes, not after the whole request allowance.

Dettmore e2e-61 waited the full 3,600 s read timeout twice for a stream the gateway had dropped. A live reply streams
tokens (reasoning included), so only a dropped one goes STREAM_IDLE_SECONDS without a byte.
"""
from __future__ import annotations

import httpx

from coordinare.services.conducting import STREAM_IDLE_SECONDS, OpenAiApiBackend


class LongRequestBackend(OpenAiApiBackend):
    _TIMEOUT = 3600.0


def test_a_streamed_call_gives_up_after_the_idle_limit_but_keeps_its_other_limits():
    timeout = LongRequestBackend('key')._client_timeout('stream')
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == STREAM_IDLE_SECONDS == 600.0
    assert timeout.connect == timeout.write == timeout.pool == 3600.0


def test_an_unstreamed_call_and_a_short_allowance_are_unchanged():
    assert LongRequestBackend('key')._client_timeout('strict') == 3600.0
    # The default 180 s allowance is already shorter than the idle limit.
    assert OpenAiApiBackend('key')._client_timeout('stream').read == 180.0
