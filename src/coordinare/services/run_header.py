"""The run header: the inputs a run was given, as the first line of its transcript (spec 130).

The daemon writes one header per run, at dispatch, after the performer accepts the job.
It is seq 0 and is not a performer record. The performer numbers its records from 1, so
the header never shifts that numbering. The writer redacts it like every record.

Two builders feed it:

* :func:`build_run_inputs` runs in the dispatch node. It resolves what only the daemon
  knows: the persona version the run is given, and the routing rule that picked the
  role's mode and model.
* :func:`build_run_header` runs in the HTTP performer service after acceptance. It reads
  the card context the job was sent with, and the performer endpoint.

Neither builder raises. A value the daemon cannot know is null with a reason, never
guessed. The rendered task prompt is not in the header: each performer backend builds it
inside the performer container, and most backends record it as the run's first ``user``
record. The header says so and points there.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services.persona_history import (
    persona_history_root,
    persona_text_sha256,
    reference_for_dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = structlog.get_logger(__name__)

RUN_HEADER_TYPE = "run_header"
RUN_HEADER_VERSION = 1

#: Appended to a persona text by the dispatch node (the language directive). The header
#: removes it again to recover the persona text the persona history versions.
PERSONA_LANGUAGE_SUFFIX = (
    "\n\nWrite all user-facing progress updates, summaries, findings, and explanations "
    "in English. Preserve code, paths, quoted source text, and machine-readable "
    "protocol fields exactly; do not translate those values."
)

#: Bounds on what the header carries, in characters. A truncated field says so.
CARD_TEXT_LIMIT_CHARS = 32_768
CRITERIA_LIMIT = 50
CRITERION_LIMIT_CHARS = 500
PERSONA_TEXT_LIMIT_CHARS = 32_768
FEEDBACK_ITEM_LIMIT_CHARS = 8_192
FEEDBACK_TOTAL_CHARS = 65_536
FEEDBACK_DEPTH = 4
FEEDBACK_LIST_LIMIT = 20
FEEDBACK_KEY_LIMIT = 50
#: The writer writes ASCII JSON, where a non-ASCII character costs up to six bytes. The
#: character bounds above therefore stay well inside this byte bound.
RUN_HEADER_MAX_BYTES = 1024 * 1024

_PROMPT_REASON = (
    "the task prompt is built inside the performer by the backend, not by the daemon, "
    "so the daemon cannot record its exact text"
)
_PROMPT_SEE = (
    "the run's first user record (type user) carries the rendered prompt for backends "
    "that emit one; the capability record lists what a backend provides"
)
_ROUTING_TABLE_REASON = (
    "the self-hosted routing table is read inside the performer container, so the daemon "
    "cannot name the rule that matched"
)


def _cut(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut to ``limit`` characters, with a marker that says so."""
    if len(text) <= limit:
        return text, False
    marker = f"\n[run header field truncated: {len(text)} characters, first {limit} kept]"
    return text[:limit] + marker, True


def _str_or_none(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _bound_value(value: Any, depth: int) -> Any:
    """A JSON-safe copy of ``value`` with every string, list and mapping bounded."""
    if isinstance(value, str):
        return _cut(value, FEEDBACK_ITEM_LIMIT_CHARS)[0]
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= FEEDBACK_DEPTH:
        return _cut(str(value), FEEDBACK_ITEM_LIMIT_CHARS)[0]
    if isinstance(value, dict):
        return {
            str(key): _bound_value(item, depth + 1)
            for key, item in list(value.items())[:FEEDBACK_KEY_LIMIT]
        }
    if isinstance(value, list | tuple):
        return [_bound_value(item, depth + 1) for item in list(value)[:FEEDBACK_LIST_LIMIT]]
    return _cut(str(value), FEEDBACK_ITEM_LIMIT_CHARS)[0]


def _feedback_block(raw: object) -> dict[str, Any]:
    """The relayed feedback, bounded item by item and in total."""
    if not isinstance(raw, list):
        return {"items": [], "omitted": 0}
    items: list[Any] = []
    omitted = 0
    used = 0
    for entry in raw:
        if not isinstance(entry, dict):
            omitted += 1
            continue
        bounded = _bound_value(entry, 0)
        size = len(json.dumps(bounded, default=str))
        if used + size > FEEDBACK_TOTAL_CHARS:
            omitted += 1
            continue
        used += size
        items.append(bounded)
    return {"items": items, "omitted": omitted}


def _card_block(card_context: dict[str, Any]) -> dict[str, Any]:
    description, cut_description = _cut(
        str(card_context.get("description") or ""), CARD_TEXT_LIMIT_CHARS,
    )
    raw_criteria = card_context.get("acceptance_criteria")
    criteria_source = raw_criteria if isinstance(raw_criteria, list) else []
    criteria: list[str] = []
    cut_criteria = len(criteria_source) > CRITERIA_LIMIT
    for criterion in criteria_source[:CRITERIA_LIMIT]:
        text, was_cut = _cut(str(criterion), CRITERION_LIMIT_CHARS)
        criteria.append(text)
        cut_criteria = cut_criteria or was_cut
    issue_number = card_context.get("issue_number")
    return {
        "id": _str_or_none(card_context.get("id")),
        "issue_number": issue_number if isinstance(issue_number, int) else None,
        "title": _str_or_none(card_context.get("title")),
        "description": description,
        "acceptance_criteria": criteria,
        "truncated": cut_description or cut_criteria,
    }


def _persona_block(persona: object, card_context: dict[str, Any]) -> dict[str, Any]:
    """The persona version and the exact persona text the performer was sent."""
    record: dict[str, Any] = persona if isinstance(persona, dict) else {}
    sent = card_context.get("persona_instructions")
    sent_text = sent if isinstance(sent, str) else None
    version = record.get("version")
    block: dict[str, Any] = {
        "name": _str_or_none(record.get("name")),
        "version": version if isinstance(version, int) and not isinstance(version, bool) else None,
        "version_sha256": _str_or_none(record.get("version_sha256")),
        "changed_at": _str_or_none(record.get("changed_at")),
        "changed_by": _str_or_none(record.get("changed_by")),
        "source": _str_or_none(record.get("source")),
        "sent_text": None,
        "sent_text_sha256": None,
    }
    if sent_text is not None:
        block["sent_text"] = _cut(sent_text, PERSONA_TEXT_LIMIT_CHARS)[0]
        block["sent_text_sha256"] = persona_text_sha256(sent_text)
    if block["version"] is None:
        block["reason"] = _str_or_none(record.get("reason")) or (
            "no persona version was recorded for this dispatch"
        )
    return block


def _model_block(card_context: dict[str, Any]) -> dict[str, Any]:
    model = _str_or_none(card_context.get("model"))
    if model is None:
        return {"model": None, "reason": "no model was resolved for this role (it has no mode)"}
    return {
        "model": model,
        "reasoning_policy": _str_or_none(card_context.get("reasoning_policy")),
        "base_url": _str_or_none(card_context.get("base_url")),
        "api_key_env": _str_or_none(card_context.get("api_key_env")),
        "auth_token_env": _str_or_none(card_context.get("auth_token_env")),
    }


_UPSTREAM_KEYS = ("name", "model", "wire_format", "base_url", "auth_env", "auth_style", "reasoning_policy")


def _upstream_block(ref: object) -> dict[str, Any] | None:
    if not isinstance(ref, dict):
        return None
    return {key: ref.get(key) for key in _UPSTREAM_KEYS}


def _model_endpoints_block(orchestration: object) -> dict[str, Any]:
    """The tool, thinking and classifier legs. Names and endpoints only, never a key value."""
    if not isinstance(orchestration, dict):
        return {
            "strategy": None,
            "tool": None,
            "thinking": None,
            "classifier": None,
            "reason": "no orchestration block: the mode is single (one model), or the role has no mode",
        }
    return {
        "strategy": _str_or_none(orchestration.get("strategy")),
        "tool": _upstream_block(orchestration.get("tool")),
        "thinking": _upstream_block(orchestration.get("thinking")),
        "classifier": _upstream_block(orchestration.get("classifier")),
    }


def _mode_block(mode: object, card_context: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = mode if isinstance(mode, dict) else {}
    name = _str_or_none(record.get("name"))
    if name is None:
        return {"name": None, "strategy": None, "reason": _str_or_none(record.get("reason")) or "no mode"}
    orchestration = card_context.get("orchestration")
    strategy = orchestration.get("strategy") if isinstance(orchestration, dict) else "single"
    return {"name": name, "strategy": _str_or_none(strategy), "reason": None}


def _routing_block(routing: object) -> dict[str, Any]:
    record: dict[str, Any] = routing if isinstance(routing, dict) else {}
    block: dict[str, Any] = {
        "role": _str_or_none(record.get("role")),
        "backend": _str_or_none(record.get("backend")),
        "mode": _str_or_none(record.get("mode")),
        "rule": _str_or_none(record.get("rule")),
        "reason": _str_or_none(record.get("reason")),
    }
    if block["rule"] is None and block["reason"] is None:
        block["reason"] = "no routing rule was resolved for this dispatch"
    block["performer_routing_table"] = {"rule": None, "reason": _ROUTING_TABLE_REASON}
    return block


def build_run_header(
    *,
    run_id: str,
    card_context: dict[str, Any],
    run_inputs: dict[str, Any] | None,
    performer: Mapping[str, str | None],
    now: datetime | None = None,
) -> dict[str, Any]:
    """The header record for one run. Plain JSON, bounded, and never raises.

    ``performer`` carries ``id``, ``transport`` and ``endpoint`` (the endpoint is None
    when the performer has none). ``run_inputs`` is what :func:`build_run_inputs`
    returned at dispatch. ``None`` means the dispatcher did not supply it, and the
    header says so for every field that came from it.
    """
    inputs: dict[str, Any] = run_inputs if isinstance(run_inputs, dict) else {}
    stamp = (now or datetime.now(UTC)).isoformat()
    missing = "not supplied with this dispatch"
    persona_source: object = inputs.get("persona")
    if run_inputs is None:
        persona_source = {"version": None, "reason": missing}
    routing_source: object = inputs.get("routing")
    if run_inputs is None:
        routing_source = {"rule": None, "reason": missing}
    mode_source: object = inputs.get("mode")
    if run_inputs is None:
        mode_source = {"name": None, "reason": missing}
    return {
        "type": RUN_HEADER_TYPE,
        "seq": 0,
        "header_version": RUN_HEADER_VERSION,
        "ts": stamp,
        "run_id": run_id,
        "prompt": {"rendered": None, "reason": _PROMPT_REASON, "see": _PROMPT_SEE},
        "persona": _persona_block(persona_source, card_context),
        "performer": {
            "id": _str_or_none(performer.get("id")),
            "backend": _str_or_none(card_context.get("backend")),
            "transport": _str_or_none(performer.get("transport")),
            "endpoint": _str_or_none(performer.get("endpoint")),
        },
        "model": _model_block(card_context),
        "model_endpoints": _model_endpoints_block(card_context.get("orchestration")),
        "mode": _mode_block(mode_source, card_context),
        "routing": _routing_block(routing_source),
        "card": _card_block(card_context),
        "feedback": _feedback_block(card_context.get("relay_feedback")),
    }


def build_run_inputs(
    *,
    config: object,
    role: str | None,
    persona_sent: str | None,
    transcript_target: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What the daemon knows about a dispatch and the performer cannot: never raises.

    Records the persona version the dispatch runs with (recording a change first, see
    :func:`persona_history.reference_for_dispatch`), the mode and the routing rule that
    picked the role's model, and the transcript folder the run belongs to.
    """
    try:
        return {
            "transcript_target": dict(transcript_target or {}),
            "persona": _persona_inputs(config, role, persona_sent),
            **_mode_and_routing(config, role),
        }
    except Exception as exc:  # diagnostics never fail a dispatch
        logger.warning("run_header.inputs_failed", exc_type=type(exc).__name__)
        reason = f"run inputs could not be resolved: {type(exc).__name__}"
        return {
            "transcript_target": dict(transcript_target or {}),
            "persona": {"version": None, "reason": reason},
            "mode": {"name": None, "reason": reason},
            "routing": {"role": role, "rule": None, "reason": reason},
        }


def _persona_inputs(config: object, role: str | None, persona_sent: str | None) -> dict[str, Any]:
    if role is None or persona_sent is None:
        return {"name": role, "version": None, "reason": "this stage has no persona text in the dispatch"}
    effective = persona_sent.removesuffix(PERSONA_LANGUAGE_SUFFIX)
    reference = reference_for_dispatch(persona_history_root(config), role, effective)
    return {"name": role, **reference}


def _mode_and_routing(config: object, role: str | None) -> dict[str, Any]:
    performers = getattr(config, "performers", None)
    role_config = performers.resolved_role(role) if performers is not None and role else None
    if role_config is None:
        reason = "the role has no performer configuration"
        return {
            "mode": {"name": None, "reason": reason},
            "routing": {"role": role, "rule": None, "reason": reason},
        }
    mode_name = _str_or_none(role_config.mode)
    rule = (
        f"performers.{role} -> modes.{mode_name}"
        if mode_name
        else f"performers.{role} (no mode: the model comes from the role)"
    )
    return {
        "mode": {"name": mode_name, "reason": None if mode_name else "the role has no mode"},
        "routing": {
            "role": role,
            "backend": _str_or_none(role_config.backend),
            "mode": mode_name,
            "rule": rule,
            "reason": None,
        },
    }
