"""Backend error-reason classifiers for the monitor (435, 119, 138)."""

from __future__ import annotations

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


def _is_transient_backend_error(reason: str) -> bool:
    """True for SYSTEM/infrastructure failures (backend CLI crash, container
    death, transport/readiness) that warrant an auto-retry rather than parking
    the card in Blocked. These are NOT content verdicts — a flaky CLI or a
    container hiccup shouldn't permanently block a card. The system_error path
    (backoff + budget) still blocks after N consecutive failures."""
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

