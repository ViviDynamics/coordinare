# Research: Config Assistant (spec 155, issue #202)

Verified against `main` at 037f8b9 on 2026-08-30.

## R1 — Not an agentic tool-calling loop

**Decision**: one constrained structured response per turn. No tool loop.

The issue specifies "structured tool calls" and an agent toolbelt. That mechanism has already
been tried in this repository and did not work. Spec 124 adopted OpenWiki's DeepAgents
tool-calling loop, and a live POC found it unreliable across **every** self-hosted model
available — `qwen3.6:35b`, `glm-4.7-flash`, `gpt-oss:120b` all flaked on the agentic
read/write/todo loop — while cloud models are not permitted for that role. Spec 124 dropped the
backend entirely and shipped a single-shot structured JSON contract instead, which was reliable.

This spec has the same constraint (FR-010: self-hosted must work), so it would hit the same wall.
Building the loop anyway would be repeating a failure the project has already paid for once.

What survives from the issue's design is the useful half: the descriptors *are* a schema. They
become the **context** the model is given and the **shape** its answer must take, rather than a
set of callable tools. Each turn: masked config + conversation + operator message in, one JSON
object out — a reply, or a reply carrying a proposal.

**Alternatives considered**: a loop with a retry budget (still depends on the capability that was
shown to be missing); requiring a cloud model (violates the project's hard constraint).

## R2 — The model is the conducting backend

**Decision**: reuse `ConductingBackendProtocol`, already built and already in daemon state.

`build_conducting_backend(config)` (`services/conducting.py:603`) is constructed in
`__main__.py:889` and placed in graph state at `:987`; the protocol is
`async def prompt(text, response_format=None) -> dict[str, Any]`
(`services/conducting.py:197`). `classify_issue_comment_ai` already uses it exactly this way,
with `response_format="json"` and a `None`-on-failure contract.

This is the whole of FR-010 for free: the backend is whatever the operator configured, self-hosted
included, so "no cloud requirement" needs no new configuration surface. The dashboard receives
`daemon` and can reach it.

**Alternative considered**: a separate assistant-specific endpoint, as the issue suggests. It
would add a config surface, a second failure mode and a second thing to document, to reach a
capability the existing backend already provides.

## R3 — "Never applies", enforced structurally

**Decision**: the assistant module imports nothing that can write, and a test asserts it.

A prompt instruction is not a guarantee; it is a request to a system whose whole failure mode is
not following instructions. The assistant builds context and parses a response. Writing lives
where it already lives — the dashboard's config endpoints, reached by a human clicking Apply.

The test asserts over the module's AST that it neither imports nor calls the write service, which
fails on the change that would introduce the capability rather than on an observation that it was
not used.

## R4 — Masking

**Decision**: reuse the config UI's own masking, do not write a second one.

`config_descriptors.py` already has `SECRET_MASK` and
`serialize_value(raw, *, secret) -> (value, was_masked)` (`:49`, `:128`), plus
`is_env_placeholder` for the `${VAR}` indirection. The descriptors carry which fields are secret,
so the masked view the UI renders is the view the model gets.

A second masking implementation would be a second thing to get wrong, and the two would drift.

FR-009 (a proposal may set an env-var *name* but never a literal secret) is enforced on the way
back in: a proposed value for a secret-carrying field is accepted only if it is an env
placeholder.

## R5 — The concurrency guard the issue assumes is not on this path

**Discovered, not assumed.** The issue says applying uses "the SHA-256 concurrency guard so a
stale proposal can't clobber concurrent edits". That guard exists —
`config_write_service.guard_concurrency` / `ConcurrencyConflictError` (`:108`, `:85`) — and the
catalog and routing endpoints use it. **`PUT /api/config/global` does not.** It validates unknown
fields and the resulting config, then writes, with no hash check anywhere in the handler
(`dashboard.py:4864-4935`).

That is the endpoint a config proposal most often targets, so FR-007 cannot be met by "reuse what
is there".

**Decision**: give `PUT /api/config/global` an **optional** `expected_hash`, enforced with the
existing `guard_concurrency` when present and returning 409 on conflict. Optional, so no existing
caller or test changes behaviour (SC-008); the assistant's Apply always sends one, so every
assistant-driven write is guarded.

**Deliberately not done here**: making the existing config UI send a hash too. It is a real gap
and a strictly better end state, but it changes the behaviour of a surface this spec was not
asked to touch, and doing it quietly inside an assistant feature is how unrelated regressions
arrive. Filed separately.

## R6 — Off by default

**Decision**: a config flag, and a backend must exist. Both.

The flag alone would let an operator enable it into a 500; the backend alone would turn the
feature on for every existing deployment that has a conducting backend, which is all of them.
With it off, the dashboard serves exactly what it serves today (FR-014, SC-005), which is
asserted rather than assumed.

## R7 — Bounded context, and saying what was left out

**Decision**: send the section list always, and full detail only for sections in play; state any
omission in the prompt itself.

A large config would otherwise either blow the context window or be silently truncated, and
silent truncation makes the model confidently wrong about fields it was never shown. Telling it
what it cannot see is what lets it say "I can't see that section" instead of inventing one.
