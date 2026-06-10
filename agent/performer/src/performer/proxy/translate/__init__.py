"""Pure Anthropic↔OpenAI wire-format translators for the self-hosted backend layer.

This sub-package implements the spec-084 ``translate`` strategy: a
``(claude_code, <model>)`` routing entry can reach an OpenAI-wire upstream
(Ollama-direct gpt-oss) with no LiteLLM in the path. ``claude_code`` speaks the
Anthropic wire protocol (``POST /v1/messages``); self-hosted gpt-oss on Ollama
speaks the OpenAI wire (``/v1/chat/completions``) only.

The translators are pure (no network, no disk, no body logging — FR-011) so they
are unit-testable in isolation against stub bodies/streams:

- :func:`translate_request` — Anthropic ``/v1/messages`` body → OpenAI
  ``/v1/chat/completions`` body (``request.py``).
- :func:`translate_response` — OpenAI chat-completion JSON → Anthropic
  ``/v1/messages`` response object (``response.py``).
- the OpenAI-SSE → Anthropic-SSE ``StatefulSSEFilter`` subclass (``sse.py``).
- :data:`FINISH_REASON_MAP` / :func:`map_finish_reason` — the single
  source of truth shared by the JSON and SSE response paths (``finish_reason.py``).

Symbols are re-exported here as each module lands during implementation.
"""

from __future__ import annotations

from .finish_reason import FINISH_REASON_MAP, map_finish_reason
from .request import translate_request
from .response import translate_response
from .sse import TranslateSSEFilter, TranslatingSSEFilter

__all__ = [
    "FINISH_REASON_MAP",
    "map_finish_reason",
    "translate_request",
    "translate_response",
    "TranslatingSSEFilter",
    "TranslateSSEFilter",
]
