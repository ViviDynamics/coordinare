"""The config assistant: proposes configuration changes, and cannot make them.

An operator configuring coordinare for the first time has to work out which of a
few dozen YAML fields they need before anything runs. The dashboard already
renders every one of them as a typed, described setting (spec 081). This turns
that surface into a conversation.

**It cannot write.** Not "is careful about writing" -- there is no write path in
this module, and a test asserts that over its AST. Applying a proposal is a human
clicking Apply in the dashboard, through the same validate-then-atomic-write path
as any other config edit. That is deliberate: coordinare's config holds the GitHub
token's env-var name, the model endpoints, and which personas may act. A wrong
write is an outage or a leak, and the dashboard is unauthenticated by design at
launch (spec 144), so an assistant that could write would be a way to change
coordinare's behaviour by talking to it.

**One structured response per turn, not a tool-calling loop.** Issue #202 asked
for an agent toolbelt. Spec 124 built one -- OpenWiki's DeepAgents loop -- and
found tool-calling unreliable across every self-hosted model available
(``qwen3.6:35b``, ``glm-4.7-flash``, ``gpt-oss:120b``), while cloud models are not
permitted for this role. It shipped a single-shot structured contract instead,
which worked. This feature carries the same self-hosted requirement, so it would
hit the same wall. What survives is the useful half: the descriptors *are* a
schema, used as the model's context and as the shape of its answer rather than as
callable tools.

**Configuration secrets do not reach the model.** The masked view the config UI
already renders is the only view built here -- ``config_descriptors`` masks at a
choke point, so there is no second implementation to drift.

The qualifier is load-bearing. What an operator *types* is sent verbatim, because
it has to be: this is a chat, and filtering arbitrary prose for things that might
be secrets would be both unreliable and a way to mangle legitimate questions. So
the guarantee is "coordinare does not send your stored secrets", not "nothing
secret can ever reach the endpoint", and the panel says so where an operator is
about to type. Stating it narrowly is the point -- a broader claim would invite
exactly the pasted-token it cannot prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog

from coordinare.config_descriptors import (
    GLOBAL_EDITABLE_FIELDS,
    SECRET_MASK,
    ConfigSnapshot,
    build_snapshot,
    scalar_field_names,
)

logger = structlog.get_logger(__name__)

#: Rough characters-per-token. Only used to keep a prompt from running away; the
#: exact ratio does not matter because the budget is a guard rail, not an
#: accounting system.
_CHARS_PER_TOKEN = 4

#: A value that is *only* an environment reference, with nothing beside it.
#: ``config_descriptors.is_env_placeholder`` asks whether a value *contains* one,
#: which is the right question for display -- but it would accept
#: ``"${TOKEN} ghp_realsecret"``, letting a literal ride into config.yaml alongside
#: the reference it was supposed to be replaced by.
_ONLY_ENV_REF = re.compile(r"^\s*\$\{[A-Za-z_][A-Za-z0-9_]*\}\s*$")


@dataclass(frozen=True, slots=True)
class Proposal:
    """A configuration change the operator may choose to apply. Inert until they do."""

    section: str
    values: dict[str, Any]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange: what to show the operator, and what they may apply.

    ``error`` and ``proposal`` are mutually exclusive by construction in
    :func:`run_turn`. A partial proposal is never returned -- a model that
    produced nonsense yields a plain failure, because an operator offered a
    half-parsed change would be the worst of both.
    """

    reply: str = ""
    proposal: Proposal | None = None
    error: str | None = None


_SYSTEM_RULES = """\
You help an operator configure coordinare. You cannot change anything yourself:
you propose, and a human decides.

Answer with ONE JSON object and nothing else:

  {"reply": "<what to tell the operator>",
   "proposal": null}

or, when a configuration change would help:

  {"reply": "<why this change>",
   "proposal": {"section": "<section id>",
                "values": {"<field>": <value>},
                "reason": "<one sentence>"}}

Rules:
- Only use section ids and field names that appear in the configuration below.
  If you need something that is not there, say so in `reply` instead of inventing it.
- Secret fields are shown as a mask. Never propose a literal secret value; propose
  the name of an environment variable, written as ${NAME}.
- Propose one section at a time.
- If you were not shown a section you need, say which one.
"""


def _section_digest(section: Any) -> str:
    """One section rendered for the model: field names, types, and masked values."""
    lines = [f"## section: {section.id} — {section.title}"]
    if section.description:
        lines.append(section.description)
    for setting in section.settings:
        bits = [f"- {setting.key} ({setting.type})"]
        if setting.secret:
            bits.append("[secret: set via ${ENV_VAR} only]")
        if setting.enum:
            bits.append(f"one of: {', '.join(map(str, setting.enum))}")
        if setting.current_value is not None:
            bits.append(f"= {setting.current_value!r}")
        if setting.help:
            bits.append(f"— {setting.help}")
        lines.append(" ".join(bits))
    for item in getattr(section, "items", []) or []:
        lines.append(f"- item {item.id} ({item.kind})")
    return "\n".join(lines)


def _snapshot_of(config: Any) -> ConfigSnapshot:
    """The same masked snapshot the config UI renders.

    Built through ``config_descriptors`` rather than by reading the config object,
    because that is where masking happens. Reading fields directly here would be
    the second implementation that eventually leaks.
    """
    return build_snapshot(config)


def build_context(config: Any, *, focus: set[str] | None = None, budget: int = 6000) -> str:
    """Render the configuration for the model, masked and bounded.

    *budget* is in tokens, approximated. Sections are added whole until the budget
    is spent, and **anything left out is named**. Silent truncation is worse than a
    smaller context: a model that cannot see a section will invent one confidently,
    whereas a model told "you were not shown X" can say so.

    *focus* pulls named sections to the front, so a conversation about personas
    keeps the personas section even when the config is large.
    """
    snapshot = _snapshot_of(config)
    sections = list(snapshot.sections)
    if focus:
        sections.sort(key=lambda s: (s.id not in focus, s.id))

    limit = budget * _CHARS_PER_TOKEN
    index = "Sections that exist: " + ", ".join(s.id for s in snapshot.sections)
    parts: list[str] = [index]
    used = len(index)
    omitted: list[str] = []

    for section in sections:
        digest = _section_digest(section)
        if used + len(digest) > limit:
            omitted.append(section.id)
            continue
        parts.append(digest)
        used += len(digest)

    if omitted:
        parts.append(
            "NOTE: these sections were omitted for length and you have NOT been shown "
            f"their fields: {', '.join(omitted)}. If you need one, say so.",
        )
    return "\n\n".join(parts)


def _known_fields(config: Any, section_id: str) -> set[str] | None:
    """Editable field names in *section_id*, or ``None`` when it does not exist.

    Deliberately the *same* set the write endpoint enforces
    (``scalar_field_names``), not one derived from the rendered descriptors. If
    these two disagreed, a proposal could validate here and be refused at Apply --
    turning the button into where errors surface, which is exactly what validating
    early is meant to prevent.

    Names are bare (``github_token``), matching the write endpoint's body. The
    descriptors key the same fields as ``global.github_token``; that dotted form
    is a display concern and does not cross this boundary.
    """
    if section_id == "global":
        # The endpoint's own allowlist, not the section's 55 scalar fields. Most of
        # those cannot be written while coordinare is running, and validating against
        # them would let a proposal pass here and be refused at Apply.
        return set(GLOBAL_EDITABLE_FIELDS)

    known = scalar_field_names(section_id)
    if known:
        return set(known)
    # A section with no scalar fields is either a collection (personas, endpoints)
    # or does not exist. Collections have their own CRUD endpoints and are not
    # proposable here, so both are "not a section you may propose against".
    snapshot = _snapshot_of(config)
    if any(section.id == section_id for section in snapshot.sections):
        return set()
    return None


def _secret_fields(config: Any, section_id: str) -> set[str]:
    """Bare names of the secret-bearing fields of *section_id*."""
    prefix = f"{section_id}."
    secrets: set[str] = set()
    for section in _snapshot_of(config).sections:
        if section.id != section_id:
            continue
        for setting in section.settings:
            if not setting.secret:
                continue
            key = setting.key
            secrets.add(key.removeprefix(prefix))
    return secrets


def _parse(raw: Any) -> tuple[Turn | None, str | None]:
    """Strictly turn a model response into a :class:`Turn`, or say why not.

    Deliberately unforgiving. Every lenient branch here is a way for a malformed
    response to become a change an operator is invited to apply.
    """
    if not isinstance(raw, dict):
        return None, "the model did not return a JSON object"

    reply = raw.get("reply", "")
    if not isinstance(reply, str):
        return None, "the model's reply was not text"

    proposal_raw = raw.get("proposal")
    if proposal_raw is None:
        if not reply:
            return None, "the model returned neither a reply nor a proposal"
        return Turn(reply=reply), None

    if not isinstance(proposal_raw, dict):
        return None, "the model's proposal was not an object"

    section = proposal_raw.get("section")
    values = proposal_raw.get("values")
    if not isinstance(section, str) or not section:
        return None, "the proposal did not name a section"
    if not isinstance(values, dict) or not values:
        return None, "the proposal did not contain any values"

    reason = proposal_raw.get("reason")
    return (
        Turn(
            reply=reply,
            proposal=Proposal(
                section=section,
                values=values,
                reason=reason if isinstance(reason, str) else "",
            ),
        ),
        None,
    )


def validate_proposal(config: Any, proposal: Proposal) -> str | None:
    """Return why *proposal* is unacceptable, or ``None`` if it may be offered.

    Checked before the operator ever sees it, so nothing unreal reaches the apply
    path. A model naming a field that does not exist is common enough that letting
    it through would make Apply the place errors surface.
    """
    known = _known_fields(config, proposal.section)
    if known is None:
        return f"no such configuration section: {proposal.section!r}"
    if not known:
        return (
            f"{proposal.section} is a collection, not a set of settings; it is edited "
            "through its own controls rather than proposed as a block"
        )

    unknown = sorted(k for k in proposal.values if k not in known)
    if unknown:
        return f"no such field in {proposal.section}: {', '.join(repr(u) for u in unknown)}"

    secrets = _secret_fields(config, proposal.section)
    for key, value in proposal.values.items():
        if key not in secrets:
            continue
        # A secret may only be pointed at an environment variable. Accepting a
        # literal would put the value in config.yaml, and the model that produced
        # it has already seen only a mask -- so a "literal" here is either the
        # operator's secret echoed back or an invention.
        if not isinstance(value, str) or not _ONLY_ENV_REF.match(value):
            return (
                f"{key} is a secret and can only be set to an environment variable "
                "reference like ${MY_TOKEN}, and nothing else besides it"
            )
        if SECRET_MASK in value:
            return f"{key} was set to the display mask rather than a value"

    return _reject_invalid_values(config, proposal)


def _reject_invalid_values(config: Any, proposal: Proposal) -> str | None:
    """Would this proposal actually produce a valid configuration?

    Field names existing is not enough. A model can offer ``log_level: "verbose"``
    or ``max_concurrent_cards: "a few"`` — the names are real, the values are not.
    Without this the operator reads a sensible-looking diff, clicks Apply, and gets
    a 400 from the write endpoint: the button becomes where errors surface, which
    is what validating beforehand is for (FR-004).

    Validated by asking the real config model, not by reimplementing its rules, so
    ranges, patterns and enums are all covered and cannot drift from the schema.
    """
    holder = getattr(config, "global_config", config)
    try:
        current = holder.model_dump()
    except Exception:  # pragma: no cover - not a pydantic model
        return None

    candidate = {**current, **proposal.values}
    try:
        type(holder)(**candidate)
    except Exception as exc:
        return f"that would not be a valid configuration: {_first_problem(exc)}"
    return None


def _first_problem(exc: Exception) -> str:
    """The field and reason from a pydantic error, without the noise.

    Pydantic formats as: a count line, then the field, then the reason, then a
    docs URL. The operator needs the middle two — "log_level: String should match
    pattern ..." tells them what to ask for next, where "1 validation error" does
    not.
    """
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    body = [ln for ln in lines[1:] if not ln.startswith("For further information")]
    if len(body) >= 2:
        reason = body[1].split(" [type=")[0]
        return f"{body[0]}: {reason}"
    return body[0] if body else str(exc)


def opening_guidance(config: Any) -> str:
    """What to say first, given how configured this install already is.

    A chat box with a blinking cursor is the same problem as the YAML it replaces:
    an operator who does not know what coordinare needs still does not know. So the
    panel opens on the first thing that is actually missing.

    Ordered by what blocks what. There is no point discussing personas with someone
    who has no board to read work from, and no point discussing a board with someone
    whose model endpoint is not reachable -- the first failure they hit would be the
    one nobody mentioned.
    """
    holder = getattr(config, "global_config", config)
    endpoints = getattr(holder, "endpoints", None) or []
    org = str(getattr(holder, "github_org", "") or "")

    if not endpoints:
        # Careful not to offer what cannot be delivered: endpoints are a *collection*,
        # and collections are created through their own controls, not proposed as a
        # block of settings. Promising a proposal here would fail only after the
        # operator had answered -- the worst moment to discover it.
        return (
            "Nothing is pointing at a model yet, and that is the first thing to fix. "
            "Add one under Config → endpoints; ask me what any of its fields mean, or "
            "which shape fits what you are running (Ollama, vLLM, an OpenAI-compatible "
            "gateway) and I will talk you through it."
        )
    if not org:
        return (
            "There is no GitHub organisation configured, so coordinare has nowhere to "
            "read work from. Which org or user owns the repository you want it to "
            "work on?"
        )
    # There is deliberately no "you have no symphony" branch. A configuration with
    # an empty `symphonies` list fails validation, so coordinare never starts, so the
    # assistant is never running to say it. Checked rather than assumed -- an
    # unreachable branch would have implied a state an operator could be in.
    return (
        "Ask me about any setting and I will explain it, or describe what you want to "
        "change and I will propose it. You decide whether to apply anything."
    )


async def run_turn(
    *,
    message: str,
    history: list[dict[str, str]],
    config: Any,
    backend: Any,
    budget: int = 6000,
) -> Turn:
    """One exchange with the assistant: exactly one model call.

    Returns a :class:`Turn` carrying either a reply, a validated proposal, or an
    error. It does not raise: a broken backend is something to tell the operator
    about, not a reason for the dashboard to return a 500.
    """
    if backend is None:
        return Turn(error="no model backend is configured for the assistant")

    context = build_context(config, budget=budget)
    conversation = "\n".join(
        f"{turn.get('role', 'operator')}: {turn.get('text', '')}" for turn in history
    )
    prompt = (
        f"{_SYSTEM_RULES}\n\n"
        f"Current configuration:\n{context}\n\n"
        f"{('Conversation so far:' + chr(10) + conversation + chr(10) + chr(10)) if conversation else ''}"
        f"operator: {message}"
    )

    try:
        raw = await backend.prompt(prompt, response_format="json")
    except Exception as exc:
        logger.warning("config_assistant.backend_error", error=str(exc))
        return Turn(error=f"the assistant's model endpoint failed: {exc}")

    turn, problem = _parse(raw)
    if turn is None:
        logger.info("config_assistant.unparseable", problem=problem)
        return Turn(error=problem or "the model's response could not be understood")

    if turn.proposal is not None:
        rejection = validate_proposal(config, turn.proposal)
        if rejection is not None:
            logger.info("config_assistant.rejected_proposal", reason=rejection)
            return Turn(reply=turn.reply, error=rejection)

    return turn
