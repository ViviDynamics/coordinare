"""496 — rewrite the claude CLI's hardcoded small-model background calls.

The claude CLI has at least one background call path that requests its
hardcoded small model (``claude-haiku-*``) without consulting
``ANTHROPIC_SMALL_FAST_MODEL``. When that traffic traverses a performer proxy
(the 073 ``ClaudeCodeShim``), the request body passes through verbatim and the
upstream — e.g. a litellm proxy key restricted to an allowed-model list —
refuses it with a 403, producing one ERROR per turn.

The decision is a pure function: given the requested model name and the
deployment's configured models, decide the rewrite target (or ``None`` to leave
the request alone). Only the CLI's small-model family (``claude-…haiku``) is
rewritten; every other model name — including non-claude and non-haiku claude
models — passes through untouched, because the CLI's main model path is already
covered by ``ANTHROPIC_MODEL`` / the ``--model`` flag.

With neither ``ANTHROPIC_SMALL_FAST_MODEL`` nor ``ANTHROPIC_MODEL`` set, the
decision leaves every request alone, so deployments without the env vars keep
the byte-identical baseline.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

# claude-haiku-4-5-20251001, claude-3-5-haiku-20241022, … — the CLI's hardcoded
# background small-model family. Requires the ``claude-`` prefix and a ``haiku``
# segment; anything else (claude-sonnet, claude-opus, third-party names) is not
# the CLI's small model.
_SMALL_MODEL_FAMILY_RE = re.compile(r"^claude-.*haiku")

_SMALL_MODEL_ENV = "ANTHROPIC_SMALL_FAST_MODEL"
_PRIMARY_MODEL_ENV = "ANTHROPIC_MODEL"


def resolve_small_model_rewrite(
    requested_model: str | None, env: Mapping[str, str]
) -> str | None:
    """Return the model a small-model-family request must be rewritten to.

    ``None`` means leave the request alone. Resolution order:
    ``ANTHROPIC_SMALL_FAST_MODEL`` → ``ANTHROPIC_MODEL`` → no rewrite. A
    requested model outside the CLI's small-model family (or equal to the
    rewrite target) is never rewritten.
    """
    if not requested_model or not _SMALL_MODEL_FAMILY_RE.match(requested_model):
        return None
    for env_name in (_SMALL_MODEL_ENV, _PRIMARY_MODEL_ENV):
        candidate = (env.get(env_name) or "").strip()
        if candidate:
            return candidate if candidate != requested_model else None
    return None


def rewrite_request_model(raw: bytes, env: Mapping[str, str]) -> bytes:
    """Rewrite the ``model`` field of a CLI request body when the decision says so.

    Fail-open: a non-JSON or non-mapping body is returned verbatim (the upstream
    will reject it on its own), and a body whose model is not in the CLI's
    small-model family is byte-identical to the input.
    """
    try:
        body: Any = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return raw
    if not isinstance(body, dict):
        return raw
    rewritten = resolve_small_model_rewrite(body.get("model"), env)
    if rewritten is None:
        return raw
    return json.dumps({**body, "model": rewritten}, separators=(",", ":")).encode("utf-8")
