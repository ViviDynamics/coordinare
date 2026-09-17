"""090 US2 (L2) — reason-sensitive failure signatures.

Layer 2 of spec 090 (Baseline Repair Autonomy) classifies each failing HEAD
check against a merge-base baseline.  Classification keys on a **failure
signature** ``(name, conclusion, normalized-reason)`` so that anti-masking
(FR-009) holds: a check that fails for a *different reason* than the baseline
must classify INTRODUCED, never INHERITED.

``normalize_reason`` strips only *volatile drift* — values that legitimately
change run-to-run for the same root cause (timestamps, durations, run/job IDs,
hashes, UUIDs, line/column numbers, hash-bearing paths).  Everything else
(error types, status codes, bare numbers, exit codes) is preserved, because it
distinguishes one failure reason from another.  The function is pure and
deterministic: it reads no clock, RNG, or environment, so the same inputs
always produce the same signature (FR-007).

The hash is truncated to 16 hex chars, matching the
``compute_ci_gate_signature`` precedent.  Truncation admits (astronomically
rare) collisions; the consumer ``ci_gate.compare_signatures`` detects a
collision at compare time and routes to UNKNOWN rather than INHERITED.
"""
from __future__ import annotations

import hashlib
import re

# ---------------------------------------------------------------------------
# Drift regexes — compiled once at module load.  All operate on the *already
# lowercased* reason string, so hex/uuid patterns use lowercase ``a-f`` without
# IGNORECASE.  Ordering matters and is enforced by ``_DRIFT_SUBSTITUTIONS``
# below (UUID and hash-paths before bare hex; timestamps before line/col).
# ---------------------------------------------------------------------------

# 8-4-4-4-12 canonical UUID.
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
)
# ISO-8601: date required, time/zone optional.
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?)?",
)
# A path token whose body embeds a long hex run (cache keys, content-addressed
# artifacts).  Collapsed whole so we never leave dangling slashes behind a bare
# ``<hex>`` substitution — hence this runs *before* the bare-hex rule.
_HASH_PATH_RE = re.compile(r"\S*/\S*[0-9a-f]{8,}\S*")
# A standalone 16+ char hex run (SHA / abbreviated commit / content hash).
_HEX_RE = re.compile(r"\b[0-9a-f]{16,}\b")
# Durations: ``1m3s`` and ``42ms`` / ``1.2s`` / ``5m`` / ``2h`` forms.
_DURATION_RE = re.compile(r"\b(?:\d+m\d+s|\d+(?:\.\d+)?(?:ms|s|m|h))\b")
# Run / job / build / attempt / pid / thread identifiers (require a prefix word
# so bare numbers — status codes, exit codes — are NOT treated as drift).
_ID_RE = re.compile(r"\b(?:run|job|build|attempt|pid|thread)[\s#:_-]*\d+\b")
# Trailing line / column suffixes: ``:123`` or ``:123:7`` (leading colon
# required so HTTP status codes and bare integers survive).
_LINE_COL_RE = re.compile(r":\d+(?::\d+)?\b")

# (pattern, replacement) applied in order.  See the per-rule comments above for
# why UUID/hash-path precede bare hex and why line/col runs last.
_DRIFT_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_UUID_RE, "<uuid>"),
    (_ISO_TS_RE, "<ts>"),
    (_HASH_PATH_RE, "<path>"),
    (_HEX_RE, "<hex>"),
    (_DURATION_RE, "<dur>"),
    (_ID_RE, "<id>"),
    (_LINE_COL_RE, ":<n>"),
)

_WHITESPACE_RE = re.compile(r"\s+")

# ASCII unit separator — joins the signed payload fields so a value containing
# a literal delimiter char cannot forge a different (name, conclusion, reason)
# triple.
_FIELD_SEP = "\x1f"


def _select_source(title: str | None, summary: str | None) -> str:
    """Reason source: title primary, summary fallback, ``""`` when neither has
    non-whitespace content."""
    if title is not None and title.strip():
        return title
    if summary is not None and summary.strip():
        return summary
    return ""


def normalize_reason(title: str | None, summary: str | None) -> str:
    """Return a drift-stripped, lowercased, whitespace-collapsed reason string.

    Pure and deterministic — no clock, RNG, or environment reads (FR-007).
    """
    reason = _select_source(title, summary).lower()
    for pattern, replacement in _DRIFT_SUBSTITUTIONS:
        reason = pattern.sub(replacement, reason)
    return _WHITESPACE_RE.sub(" ", reason).strip()


def make_failure_signature(
    name: str,
    conclusion: str,
    title: str | None,
    summary: str | None,
) -> tuple[str, str]:
    """Return ``(signature_hash, normalized_reason)`` for a failing check.

    The hash is the first 16 hex chars of ``sha256`` over
    ``name \\x1f conclusion \\x1f normalized_reason`` (FR-007).  Both the
    16-char hash and the normalized reason are returned so callers can persist
    the human-readable reason alongside the comparison key.
    """
    normalized = normalize_reason(title, summary)
    payload = f"{name}{_FIELD_SEP}{conclusion}{_FIELD_SEP}{normalized}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return digest, normalized


__all__ = ["make_failure_signature", "normalize_reason"]
