"""The observer core (425): wake triggers, lightweight-model verdicts.

The monitoring loop already carries the evidence a person would use — merged
performer events, tool-use counts, token deltas, repetition signatures. The
observer is the lightweight judge woken when mechanical triggers fire. Triggers
are wake conditions, not judges: they bias toward waking, never against. The
verdict vocabulary is fixed; this issue implements the schema, the call, and the
continue path only — the other actions belong to later issues.

The persona carries no stack knowledge (#364) and the prompt is built from
summaries, never raw telemetry. A dead or malformed observer is no action:
monitoring behaves exactly as it did before the observer existed.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from coordinare.services.retune import retune_prompt_fragment

logger = structlog.get_logger(__name__)

OBSERVER_TRIGGERS = frozenset({
    "quiet_window",
    "repetition_signature",
    "token_burn_anomaly",
    "turn_boundary",
    "watchdog_trip",
})

OBSERVER_VERDICTS = frozenset({
    "continue",
    "correction",
    "kill",
    "retune",
    "escalate",
})

_REASON_CAP = 300
_PROMPT_TEXT_CAP = 400
_PROMPT_RECENT_ITEMS = 5
_PROMPT_BUDGET = 8000
_CORRECTION_BODY_CAP = 800


def correction_signature(text: str) -> str:
    """A 12-hex digest of the correction text, normalised for comparison.

    Whitespace runs and casing are not steering changes: rewording the same
    directive collapses, any real change of content replaces.
    """
    normalized = " ".join((text or "").split()).casefold()
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def correction_body(text: str, evidence: dict[str, Any] | None) -> str:
    """The human-feedback text that rides the next turn's payload.

    The prompt never carries raw telemetry (425), so the evidence summary is
    the scalar evidence fields only — type and length preserve the shape.
    """
    parts = [f"Observer correction: {(text or '').strip()}"]
    if evidence:
        pairs = sorted(
            f"{key}={value}"
            for key, value in evidence.items()
            if isinstance(value, (str, int, float, bool))
        )
        if pairs:
            parts.append(f"Evidence summary: {', '.join(pairs)}")
    body = "\n".join(parts)
    if len(body) > _CORRECTION_BODY_CAP:
        body = body[: _CORRECTION_BODY_CAP - 1] + "…"
    return body


def record_correction(
    pending: dict[str, Any] | None,
    text: str,
    evidence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Fold a correction verdict into the pending correction.

    Returns the new pending correction when the directive is new or changed
    (replacing whatever was pending), and ``None`` when the same correction
    repeats — collapsing it, never re-sending an identical text.
    """
    signature = correction_signature(text)
    if pending and pending.get("signature") == signature:
        return None
    return {"signature": signature, "body": correction_body(text, evidence)}


@dataclass(frozen=True)
class ObserverVerdict:
    verdict: str
    reason: str
    # 428: the optional structured retune request, {knob: requested_value}.
    # Absent (None) unless the verdict carries a usable "retune" block; the
    # retune phase clamps and bounds-checks it before anything is applied.
    retune: dict[str, Any] | None = None


@dataclass(frozen=True)
class TriggerSnapshot:
    quiet_age_s: float
    quiet_window_s: float
    repetition_count: int
    repetition_threshold: int
    tokens_delta: int
    token_burn_min_tokens: int
    production_moved: bool
    watchdog_pending: bool


class _ObserverPromptBackend(Protocol):
    async def prompt(self, text: str, response_format: str | None = None) -> Any: ...


def parse_verdict(answer: Any) -> ObserverVerdict | None:
    """Parse a structured judge answer. Anything malformed is not a verdict.

    The conducting backends already wrap the model's parsed JSON in a "data"
    envelope, so the verdict is read from answer["data"]. If the model added
    its own "data" wrapper anyway, unwrap one level — a correct verdict under
    a double envelope beats a discarded one.
    """
    if not isinstance(answer, dict):
        return None
    data = answer.get("data")
    if isinstance(data, dict) and not isinstance(data.get("verdict"), str):
        nested = data.get("data")
        if isinstance(nested, dict):
            data = nested
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or verdict not in OBSERVER_VERDICTS:
        return None
    reason_raw = data.get("reason", "")
    reason = reason_raw if isinstance(reason_raw, str) else str(reason_raw)
    # Single-line, capped: the verdict log is a structured-log line, and the
    # prompt it summarises carries summaries only (never raw event text), so
    # the reason cannot echo telemetry the model was never shown.
    reason = " ".join(reason[:_REASON_CAP].split())
    # 428: the optional structured retune request. Only scalar values survive
    # (the retune phase clamps scalars); anything nested is model noise and
    # is dropped, an all-noise block parses as absent.
    retune_raw = data.get("retune")
    retune = None
    if isinstance(retune_raw, dict):
        scalars = {
            key: value
            for key, value in retune_raw.items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
        retune = scalars or None
    return ObserverVerdict(verdict=verdict, reason=reason, retune=retune)


def observer_enabled(state: Any) -> bool:
    """Whether the active symphony's observer is switched on."""
    sym_name = state.get("current_symphony")
    sym_cfg = (state.get("symphony_configs") or {}).get(sym_name) if sym_name else None
    observer_cfg = getattr(sym_cfg, "observer", None) if sym_cfg is not None else None
    return bool(observer_cfg is not None and getattr(observer_cfg, "enabled", False))


def evaluate_triggers(
    snapshot: TriggerSnapshot,
    enabled_triggers: frozenset[str] | set[str] = OBSERVER_TRIGGERS,
) -> list[str]:
    """Fire the triggers the evidence supports, from the configurable subset."""
    fired: list[str] = []
    for trigger in sorted(enabled_triggers & OBSERVER_TRIGGERS):
        if trigger == "quiet_window":
            fires = snapshot.quiet_window_s > 0 and snapshot.quiet_age_s >= snapshot.quiet_window_s
        elif trigger == "repetition_signature":
            fires = snapshot.repetition_count >= snapshot.repetition_threshold
        elif trigger == "token_burn_anomaly":
            fires = (
                snapshot.tokens_delta >= snapshot.token_burn_min_tokens
                and not snapshot.production_moved
            )
        elif trigger == "turn_boundary":
            fires = snapshot.production_moved
        elif trigger == "watchdog_trip":
            fires = snapshot.watchdog_pending
        else:
            fires = False
        if fires:
            fired.append(trigger)
    return fired


def persona() -> str:
    """The judge's role. No stack knowledge, no tool names (#364)."""
    return (
        "You are a site foreman watching a job site from a distance. "
        "You cannot see the workers' hands, only the shape of their progress: "
        "how long the site has been quiet, whether the same motion repeats, "
        "whether effort is producing visible results. "
        "You judge whether the work needs a nudge, a stop, or nothing. "
        "You never name tools, languages, or technologies; you describe "
        "behaviour and outcomes only."
    )


def _summarise_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in sorted(evidence.keys()):
        value = evidence[key]
        if isinstance(value, (bool, int, float)):
            summary[key] = value
        elif isinstance(value, str):
            summary[key] = value[:_PROMPT_TEXT_CAP]
    return summary


def build_prompt(
    evidence: dict[str, Any],
    triggers: list[str],
    recent_meta: list[str],
    retune_bounds: dict[str, Any] | None = None,
) -> str:
    """Build the judge prompt from bounded summaries. Never raw telemetry.

    The recent-activity tail carries type/length metadata, not event text:
    performer events are untrusted telemetry (command output, diffs), and
    none of it belongs in a prompt sent to the observer's endpoint.

    428: when retune bounds are configured the retunable knobs are advertised
    so a retune verdict can name values inside them. Without bounds the
    prompt is byte-identical to the pre-428 shape.
    """
    fixed = "\n".join([
        persona(),
        "",
        "You are judging one cycle of an autonomous job. Reply with a verdict.",
        "",
        f"Triggers that woke you: {', '.join(sorted(triggers))}",
        f"Evidence summary: {_summarise_evidence(evidence)}",
        "",
        "Verdicts:",
        "- continue: the work is moving; leave it alone.",
        "- correction: the work drifts; the next instruction should redirect it.",
        "- kill: stop the work; it is wasting effort without results.",
        "- retune: the approach itself needs to change, not just the instruction.",
        "- escalate: a person must decide; the evidence is ambiguous or grave.",
        "",
        "Answer only with JSON of the form "
        '{"verdict": <verb>, "reason": <at most a few sentences>}.',
        "",
        "Recent activity (type/length): ",
    ])
    retune_evidence = retune_prompt_fragment(retune_bounds)
    if retune_evidence:
        # Insert the retune evidence before the recent-activity tail, after
        # the answer schema, and extend the schema so the model knows a
        # retune verdict may carry the structured request.
        fixed = fixed.replace(
            "Recent activity (type/length): ",
            retune_evidence + "\n"
            'A retune verdict MAY add "retune": {<knob>: <value>} naming a '
            "retunable knob and its requested value.\n"
            "Recent activity (type/length): ",
        )
    # Whatever the fixed body left of the budget is what the recent tail gets.
    recent: list[str] = []
    budget = _PROMPT_BUDGET - len(fixed)
    for chunk in reversed(recent_meta):
        if budget - len(chunk) < 0:
            break
        budget -= len(chunk)
        recent.append(chunk)
    recent.reverse()
    return (fixed + str(recent))[:_PROMPT_BUDGET - 1]


@dataclass(frozen=True)
class ObserverQuery:
    """One observer wake: who is being judged, and with what evidence."""

    card_id: str
    stage: str
    triggers: list[str]
    evidence: dict[str, Any]
    prompt: str


async def observe(
    backend: _ObserverPromptBackend | None,
    query: ObserverQuery,
) -> ObserverVerdict | None:
    """Ask the observer. Never raises; a dead observer is no action."""
    if backend is None:
        return None
    try:
        answer = await asyncio.wait_for(
            backend.prompt(query.prompt, response_format="json"),
            timeout=60.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "observer.unavailable",
            card_id=query.card_id,
            performer_stage=query.stage,
            triggers=sorted(query.triggers),
            error=str(exc),
        )
        return None
    verdict = parse_verdict(answer)
    if verdict is None:
        logger.warning(
            "observer.verdict_unreadable",
            card_id=query.card_id,
            performer_stage=query.stage,
            triggers=sorted(query.triggers),
        )
        return None
    logger.info(
        "observer.verdict",
        card_id=query.card_id,
        performer_stage=query.stage,
        triggers=sorted(query.triggers),
        verdict=verdict.verdict,
        reason=verdict.reason,
        evidence=_summarise_evidence(query.evidence),
    )
    return verdict
