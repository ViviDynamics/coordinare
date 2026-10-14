"""The run header: the inputs a run was given, as the first line of its transcript (spec 130).

The daemon writes one header per run, at dispatch, after the performer accepts the job.
It is seq 0 and is not a performer record. The performer numbers its records from 1, so
the header never shifts that numbering. The writer redacts it like every record.

Two builders feed it:

* :func:`build_run_inputs_bounded` runs in the dispatch node. It resolves what only the
  daemon knows: the persona version the run is given, and the routing rule that picked the
  role's mode and model. The persona history touches the disk, so it runs on a worker
  thread under a bound, and the event loop never waits on it.
* :func:`build_run_header` runs in the HTTP performer service after acceptance. It reads
  the card context the job was sent with, and the performer endpoint.

Neither builder raises. A value the daemon cannot know is null with a reason, never
guessed. The rendered task prompt is not in the header: each performer backend builds it
inside the performer container, and most backends record it as the run's first ``user``
record. The header says so and points there.

Env var names are recorded under keys that carry no credential stem (``env_var_names``),
so the transcript redactor leaves them as the names they are. The redactor would replace
the value of a key such as ``api_key_env`` with ``[REDACTED]``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.lib.transcript_redaction import redact_text
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

#: The header's encoded size bound. It is measured on the bytes the writer puts on disk
#: (ASCII JSON, where a non-ASCII character costs up to six bytes). A header over the bound
#: has its fields cut further, in steps of these divisors, until it fits.
RUN_HEADER_MAX_BYTES = 1024 * 1024
_SHRINK_DIVISORS = (1, 2, 4, 8, 16, 32, 64, 128)

#: How long a dispatch waits for the persona history. Past it, the header says the version
#: was not recorded in time and the dispatch goes on.
PERSONA_RECORD_WAIT_SECONDS = 2.0

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


def _ascii_bytes(header: dict[str, Any]) -> int:
    """The header's size as the writer puts it on disk: one ASCII JSON line."""
    encoded = json.dumps(header, ensure_ascii=True, separators=(",", ":"), default=str)
    return len(encoded.encode("ascii")) + 1


def _bound_value(value: Any, depth: int, item_limit: int) -> Any:
    """A JSON-safe copy of ``value`` with every string, list and mapping bounded."""
    if isinstance(value, str):
        return _cut(value, item_limit)[0]
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= FEEDBACK_DEPTH:
        return _cut(str(value), item_limit)[0]
    if isinstance(value, dict):
        return {
            str(key): _bound_value(item, depth + 1, item_limit)
            for key, item in list(value.items())[:FEEDBACK_KEY_LIMIT]
        }
    if isinstance(value, list | tuple):
        return [
            _bound_value(item, depth + 1, item_limit) for item in list(value)[:FEEDBACK_LIST_LIMIT]
        ]
    return _cut(str(value), item_limit)[0]


def _feedback_block(raw: object, divisor: int) -> dict[str, Any]:
    """The relayed feedback, bounded item by item and in total."""
    if not isinstance(raw, list):
        return {"items": [], "omitted": 0}
    item_limit = FEEDBACK_ITEM_LIMIT_CHARS // divisor
    total_limit = FEEDBACK_TOTAL_CHARS // divisor
    items: list[Any] = []
    omitted = 0
    used = 0
    for entry in raw:
        if not isinstance(entry, dict):
            omitted += 1
            continue
        bounded = _bound_value(entry, 0, item_limit)
        size = len(json.dumps(bounded, default=str))
        if used + size > total_limit:
            omitted += 1
            continue
        used += size
        items.append(bounded)
    return {"items": items, "omitted": omitted}


def _card_block(card_context: dict[str, Any], divisor: int) -> dict[str, Any]:
    description, cut_description = _cut(
        str(card_context.get("description") or ""), CARD_TEXT_LIMIT_CHARS // divisor,
    )
    raw_criteria = card_context.get("acceptance_criteria")
    criteria_source = raw_criteria if isinstance(raw_criteria, list) else []
    criteria: list[str] = []
    cut_criteria = len(criteria_source) > CRITERIA_LIMIT
    for criterion in criteria_source[:CRITERIA_LIMIT]:
        text, was_cut = _cut(str(criterion), CRITERION_LIMIT_CHARS // divisor)
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


def _persona_block(persona: object, card_context: dict[str, Any], divisor: int) -> dict[str, Any]:
    """The persona version, and the persona text the performer was sent, redacted.

    The text is redacted before it is cut, so a cut never leaves half of a secret. The
    hash names exactly the text the header carries, so it can be reproduced from the line.
    """
    record: dict[str, Any] = persona if isinstance(persona, dict) else {}
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
        "sent_text_truncated": False,
    }
    sent = card_context.get("persona_instructions")
    if isinstance(sent, str):
        carried, cut = _cut(redact_text(sent), PERSONA_TEXT_LIMIT_CHARS // divisor)
        block["sent_text"] = carried
        block["sent_text_sha256"] = persona_text_sha256(carried)
        block["sent_text_truncated"] = cut
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
        "env_var_names": {
            "api": _str_or_none(card_context.get("api_key_env")),
            "auth": _str_or_none(card_context.get("auth_token_env")),
        },
    }


_UPSTREAM_KEYS = ("name", "model", "wire_format", "base_url", "auth_style", "reasoning_policy")


def _upstream_block(ref: object) -> dict[str, Any] | None:
    if not isinstance(ref, dict):
        return None
    block: dict[str, Any] = {key: ref.get(key) for key in _UPSTREAM_KEYS}
    block["env_var_names"] = {"auth": _str_or_none(ref.get("auth_env"))}
    return block


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


def _assemble(
    *,
    identity: Mapping[str, str],
    card_context: dict[str, Any],
    inputs: dict[str, Any] | None,
    performer: Mapping[str, str | None],
    divisor: int,
) -> dict[str, Any]:
    """The header at one level of truncation. ``divisor`` 1 is the full bounds.

    ``inputs`` is None when the dispatcher supplied no run inputs; every field that would
    have come from them then says so.
    """
    missing = "not supplied with this dispatch"
    if inputs is None:
        persona_source: object = {"version": None, "reason": missing}
        routing_source: object = {"rule": None, "reason": missing}
        mode_source: object = {"name": None, "reason": missing}
    else:
        persona_source = inputs.get("persona")
        routing_source = inputs.get("routing")
        mode_source = inputs.get("mode")
    return {
        "type": RUN_HEADER_TYPE,
        "seq": 0,
        "header_version": RUN_HEADER_VERSION,
        "ts": identity["ts"],
        "run_id": identity["run_id"],
        "prompt": {"rendered": None, "reason": _PROMPT_REASON, "see": _PROMPT_SEE},
        "persona": _persona_block(persona_source, card_context, divisor),
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
        "card": _card_block(card_context, divisor),
        "feedback": _feedback_block(card_context.get("relay_feedback"), divisor),
    }


def build_run_header(
    *,
    run_id: str,
    card_context: dict[str, Any],
    run_inputs: dict[str, Any] | None,
    performer: Mapping[str, str | None],
    now: datetime | None = None,
) -> dict[str, Any]:
    """The header record for one run. Plain JSON, bounded on its encoded bytes, never raises.

    ``performer`` carries ``id``, ``transport`` and ``endpoint`` (the endpoint is None
    when the performer has none). ``run_inputs`` is what :func:`build_run_inputs` returned
    at dispatch. ``None`` means the dispatcher did not supply it, and the header says so
    for every field that came from it.
    """
    inputs = run_inputs if isinstance(run_inputs, dict) else None
    identity = {"run_id": run_id, "ts": (now or datetime.now(UTC)).isoformat()}
    header: dict[str, Any] = {}
    for divisor in _SHRINK_DIVISORS:
        header = _assemble(
            identity=identity,
            card_context=card_context,
            inputs=inputs,
            performer=performer,
            divisor=divisor,
        )
        if _ascii_bytes(header) <= RUN_HEADER_MAX_BYTES:
            break
    return header


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
    picked the role's model, and the transcript folder the run belongs to. This is the
    blocking form. Dispatch calls :func:`build_run_inputs_bounded`.
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


async def build_run_inputs_bounded(
    *,
    config: object,
    role: str | None,
    persona_sent: str | None,
    transcript_target: dict[str, str] | None = None,
) -> dict[str, Any]:
    """:func:`build_run_inputs` off the event loop, under ``PERSONA_RECORD_WAIT_SECONDS``.

    The persona history reads and may write (with an fsync) a file, so it can be slow on
    a slow disk. Past the bound, or on any error, the persona version is reported as not
    recorded, with the reason, and the dispatch goes on. Never raises.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                build_run_inputs,
                config=config,
                role=role,
                persona_sent=persona_sent,
                transcript_target=transcript_target,
            ),
            timeout=PERSONA_RECORD_WAIT_SECONDS,
        )
    except TimeoutError:
        reason = (
            f"persona history did not answer within {PERSONA_RECORD_WAIT_SECONDS:g} s, "
            "so this persona version was not recorded in time"
        )
        logger.warning("run_header.persona_deferred", role=role, wait_seconds=PERSONA_RECORD_WAIT_SECONDS)
    except Exception as exc:  # diagnostics never fail a dispatch
        reason = f"persona history failed ({type(exc).__name__}), so this persona version was not recorded"
        logger.warning("run_header.persona_failed", role=role, exc_type=type(exc).__name__)
    inputs = build_run_inputs(config=config, role=role, persona_sent=None, transcript_target=transcript_target)
    inputs["persona"] = {"name": role, "version": None, "reason": reason}
    return inputs


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
