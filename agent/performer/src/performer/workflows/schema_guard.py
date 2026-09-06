"""Per-step schema validation with exactly one reprompt (spec 164 FR-010).

Measured during design: an observation step emitted element kinds outside the
closed enum it was handed.  Accepting that silently degrades the step's output
without any signal, so a violation earns one corrective reprompt naming the
offending fields, and then fails loudly.

One reprompt, not a loop: a model that has missed the schema twice is not going
to converge on the third try, and the gateway serves every role.
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from performer.workflows.base import SchemaViolation, WorkflowMetrics

log = structlog.get_logger(__name__)

M = TypeVar("M", bound=BaseModel)


def extract_json(text: str) -> str | None:
    """Return the outermost JSON object in *text*, or None.

    Models wrap payloads in commentary or code fences.  That is a presentation
    quirk, not a schema violation, so it is stripped before validation.
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def render_schema(schema: type[BaseModel]) -> str:
    """A compact description of *schema* for the model.

    Found by the first live eval run: the model returned a semantically correct
    plan with invented field names, because nothing in the request described the
    expected shape.  Validating against a schema the model was never shown is a
    guessing game, not a contract.

    JSON Schema rather than prose: it is unambiguous about required fields and
    closed enums, which is exactly where the live failure landed (`id` and
    `kind` omitted, `steps` returned as prose strings instead of objects).
    """
    return json.dumps(schema.model_json_schema(), separators=(",", ":"))


def schema_instruction(schema: type[BaseModel]) -> str:
    return (
        "Reply with ONLY a JSON object matching this JSON Schema exactly. "
        "Every required field must be present, and every enum value must come "
        "from the listed options.\n"
        f"{render_schema(schema)}"
    )


_EXCERPT = 300


def _excerpt(text: str) -> str:
    """A length-capped, repr-safe slice of a model response for diagnostics.

    Round-two review: a SchemaViolation said only "no JSON object after
    reprompt". Whether the model returned nothing, prose, or a near-miss IS the
    diagnosis, and it was discarded. Capped because spec 083 FR-011 forbids
    logging model text wholesale: an excerpt is diagnostic, a transcript is a
    leak.
    """
    text = (text or "").strip()
    if not text:
        return "<empty>"
    return repr(text[:_EXCERPT] + ("…" if len(text) > _EXCERPT else ""))


def _describe(exc: ValidationError) -> str:
    """A short, model-readable account of what failed validation."""
    parts = []
    for err in exc.errors()[:6]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


async def validate_with_reprompt(
    call: Callable[[str | None], Awaitable[str]],
    schema: type[M],
    metrics: WorkflowMetrics,
) -> M:
    """Call, validate against *schema*, reprompt once on violation.

    *call* receives ``None`` on the first attempt and a correction string on the
    reprompt, so the step can append it to its own persona.
    """
    raw = await call(None)
    payload = extract_json(raw)
    first_problem: str

    if payload is None:
        first_problem = "response contained no JSON object"
    else:
        try:
            return schema.model_validate(json.loads(payload))
        except (ValidationError, json.JSONDecodeError) as exc:
            first_problem = (
                _describe(exc) if isinstance(exc, ValidationError) else f"invalid JSON: {exc}"
            )

    metrics.schema_reprompts += 1
    log.warning("schema_guard.reprompt", problem=first_problem[:200], first=_excerpt(raw))
    correction = (
        f"Your previous response did not match the required schema ({first_problem}).\n"
        f"{schema_instruction(schema)}"
    )
    retried_raw = await call(correction)
    retried_payload = extract_json(retried_raw)
    if retried_payload is None:
        log.debug("schema_guard.no_json_after_reprompt", first=_excerpt(raw), retried=_excerpt(retried_raw))
        raise SchemaViolation(
            f"no JSON object after reprompt (first problem: {first_problem}); "
            f"first response {_excerpt(raw)}; retried response {_excerpt(retried_raw)}"
        )
    try:
        return schema.model_validate(json.loads(retried_payload))
    except (ValidationError, json.JSONDecodeError) as exc:
        log.debug("schema_guard.still_invalid_after_reprompt", retried=_excerpt(retried_raw))
        raise SchemaViolation(
            f"schema still unmet after one reprompt: {exc} "
            f"(first problem: {first_problem}); first response {_excerpt(raw)}; "
            f"retried response {_excerpt(retried_raw)}"
        ) from exc
