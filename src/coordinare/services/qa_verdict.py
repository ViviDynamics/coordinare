"""120 (US1): coordinare-authoritative QA evidence-integrity classification.

A QA performer's self-reported ``qa_passed`` is not trusted blindly. A pass that
verified **zero** acceptance criteria (``criteria_checked > 0`` while
``criteria_passed == 0``), or that lacks the visual evidence a UI change
requires, is *unsubstantiated* and must not advance the card — regardless of the
performer build that produced it.

This module is a **pure decision function** (no I/O, no logging of values) so it
is exhaustively unit-testable and reused by the monitor. The routing it returns
maps onto the orchestrator's existing terminal handlers:

* ``"advance"`` — substantiated pass (or a genuine no-criteria scope); proceed.
* ``"hold"``    — unsubstantiated **and** an environment signal is present; route
  to the existing ``qa_env_blocked`` HOLD path (cache repair + notify).
* ``"bounce"``  — unsubstantiated with no environment signal; treat as a failure
  and relay to the implementer.

See ``specs/120-qa-evidence-integrity/contracts/qa-report.md`` for the decision
table this implements.
"""

from __future__ import annotations

from typing import Literal

QaRoute = Literal["advance", "hold", "bounce"]


def _as_int(value: object) -> int:
    """Coerce a report count to int, defaulting to 0 on any non-numeric value."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _url_is_verifiable(path_or_url: object) -> bool:
    """True when path_or_url is a local path or a real qa-assets CDN URL.

    The performer upload mechanism (cdn_upload.py) always produces URLs of the
    form ``https://github.com/{org}/{repo}/raw/qa-assets/...``.  A
    ``user-attachments`` URL is GitHub's drag-and-drop private CDN — it is never
    produced by our upload code and indicates a model hallucination; treat it as
    absent evidence.
    """
    s = str(path_or_url) if path_or_url else ""
    if not s:
        return False
    if not s.startswith("http://") and not s.startswith("https://"):
        return True  # local file path — performer hasn't uploaded yet
    return "/raw/qa-assets/" in s


def _visual_evidence_present(report: dict) -> bool:
    """True when at least one visual_evidence item carries a verifiable path_or_url."""
    items = report.get("visual_evidence")
    if not isinstance(items, list):
        return False
    return any(isinstance(ev, dict) and _url_is_verifiable(ev.get("path_or_url")) for ev in items)


def _has_env_signal(report: dict, env_cache_health_failed: bool) -> bool:
    """True when the run carries an environment-failure signal.

    Either the QA report's honest ``environment_error`` ("couldn't verify") or
    the performer's ``env_cache_health_failed`` flag routes an unsubstantiated
    pass to HOLD (repair the environment and re-run) rather than to a bounce.
    """
    return bool(env_cache_health_failed) or bool(report.get("environment_error"))


def qa_unsubstantiated_reason(report: dict | None) -> str | None:
    """Return a names/counts-only reason when a ``qa_passed`` is unsubstantiated.

    Returns ``None`` when the pass is substantiated (or there was genuinely no
    criteria scope to verify, ``criteria_checked == 0``). The returned string
    NEVER contains secret values — only category names and counts (FR-007/019).
    """
    report = report or {}
    checked = _as_int(report.get("criteria_checked"))
    passed = _as_int(report.get("criteria_passed"))
    if checked > 0 and passed == 0:
        return f"zero_criteria_passed (0 of {checked})"
    if bool(report.get("visual_validation_required")) and not _visual_evidence_present(report):
        return "missing_visual_evidence"
    return None


def classify_qa_verdict(
    status: str,
    report: dict | None,
    env_cache_health_failed: bool = False,
) -> QaRoute:
    """Decide advance/hold/bounce for a QA terminal verdict.

    Only ``qa_passed`` is gated here — every other marker (``qa_failed``,
    ``qa_env_blocked``, errors) is handled by its own dedicated path, so this
    returns ``"advance"`` for them (a no-op for the caller's terminal-success
    branch). A substantiated ``qa_passed`` returns ``"advance"``; an
    unsubstantiated one returns ``"hold"`` (env signal present) or ``"bounce"``.
    """
    if status != "qa_passed":
        return "advance"
    report = report or {}
    if qa_unsubstantiated_reason(report) is None:
        return "advance"
    return "hold" if _has_env_signal(report, env_cache_health_failed) else "bounce"
