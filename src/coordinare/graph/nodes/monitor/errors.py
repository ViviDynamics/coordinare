"""Backend error-reason classifiers for the monitor (435, 119, 138)."""

from __future__ import annotations

import re

from coordinare.graph.nodes.handle_system_error import TRANSIENT_STATUSES

_FORMAT_ERROR_PREFIX = "BACKEND_FORMAT_ERROR:"
_WORKFLOW_PUSH_REJECTION_MARKERS: tuple[str, ...] = (
    "refusing to allow a github app to create or update workflow",
    "lacks `workflows` permission",
)


_TRANSIENT_BACKEND_ERROR_MARKERS = (
    "subprocess_exit",                 # backend CLI crashed / exited non-zero
    "server disconnected",             # container HTTP server died mid-request
    "readiness timeout",               # container slow/failed to come up
    "unreachable",
    "connection reset", "connectionreseterror", "econnreset",
    "connection refused",
    "transport error", "transporterror",
    "container start failed",
    "cannot write to closing transport",
)


# 336: httpx renders a raised status as
# ``Server error '504 Gateway Timeout' for url '...'`` (or ``Client error`` for
# 4xx), and JobRunner prefixes the exception type when the executor lets one
# escape. Anchoring on that shape is deliberate: matching a bare ``504`` matches
# the microsecond field of an ISO timestamp, which is exactly how #336's
# withdrawn "31 occurrences over nine hours" count was manufactured.
_UPSTREAM_STATUS_RE = re.compile(r"(?:client|server) error '(\d{3})[ ']")


def _upstream_status(reason: str) -> int | None:
    """The upstream HTTP status a raised httpx error carries, if any."""
    match = _UPSTREAM_STATUS_RE.search(reason.lower())
    return int(match.group(1)) if match else None


def _is_transient_backend_error(reason: str) -> bool:
    """True for SYSTEM/infrastructure failures (backend CLI crash, container
    death, transport/readiness, a transient upstream HTTP status) that warrant
    an auto-retry rather than parking the card in Blocked. These are NOT content
    verdicts — a flaky CLI, a container hiccup or a gateway timeout shouldn't
    permanently block a card. The system_error path (backoff + budget) still
    blocks after N consecutive failures.

    336: a gateway ``504`` used to take the terminal path, so an architect turn
    that had been generating for half an hour was lost and no retry counter
    moved. Coordinare already classified 504 as transient, but only through
    ``classify_upstream`` on the structured spec-067 envelope; a 504 that
    arrives as a bare reason string never reached it. The status is deferred to
    ``TRANSIENT_STATUSES``, the declared single source of truth, rather than
    matched as upstream text here.
    """
    status = _upstream_status(reason)
    if status is not None and status in TRANSIENT_STATUSES:
        return True
    # A permanent status does NOT short-circuit: this predicate only ever widens
    # what is retryable, so a reason that already matched a marker keeps its
    # existing behaviour even when a 4xx is quoted somewhere inside it.
    low = reason.lower()
    return any(m in low for m in _TRANSIENT_BACKEND_ERROR_MARKERS)


# 119: backend output-format-contract failures. A JSON-only role (tech_writer,
# assessor, ...) on a stochastic local reasoning model (gpt-oss:120b via hermes)
# occasionally emits output with no parseable JSON object — the backend reports
# `malformed_output`. This is NOT a content verdict and NOT deterministic: a
# re-dispatch almost always parses. Route it through the same bounded retry
# (handle_system_error: backoff + budget, blocks after N consecutive fails) that
# spec-098 gave assessor-shape / `BACKEND_FORMAT_ERROR:` failures, instead of
# terminal-blocking the card on the first bad roll. (Truncation — a genuinely
# too-large doc — is the deterministic case; it exhausts the budget and blocks,
# which is the right floor.)
_FORMAT_CONTRACT_ERROR_MARKERS = ("malformed_output",)


def _is_format_contract_error(reason: str) -> bool:
    """True for a backend output-format-contract failure (e.g. ``malformed_output``)
    that warrants a bounded retry rather than an immediate terminal block."""
    low = reason.lower()
    return any(m in low for m in _FORMAT_CONTRACT_ERROR_MARKERS)


def _is_workflow_push_permission_error(reason: str) -> bool:
    """True when git push was rejected because workflow writes are disallowed."""
    lowered = reason.lower()
    return (
        "git push failed" in lowered
        and any(marker in lowered for marker in _WORKFLOW_PUSH_REJECTION_MARKERS)
    )

