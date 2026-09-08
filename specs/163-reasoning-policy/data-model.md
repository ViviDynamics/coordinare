# Data Model: Reasoning Policy and Truncation Classification (163)

Nothing here is persisted. The classification is computed per failure; the policy is a
config surface. No `state_store.py` change and no schema version bump.

## FailureShape (existing — reach and inputs change)

`AssessorFailureShape = Literal["empty_answer", "malformed_body", "empty_body", "truncated"]`

Already correct. This feature changes **who it is computed for** and **what it reads**, not
what it means.

| Shape | Meaning | Actionable cause |
| --- | --- | --- |
| `truncated` | ran out of output budget | budget too small for this prompt |
| `empty_answer` | responded, but with no usable answer | model or prompt |
| `empty_body` | nothing came back at all | infrastructure |
| `malformed_body` | came back unparseable | format contract |

**Precedence (FR-006)**: a response that is both truncated and unparseable is `truncated`.
The budget is the actionable cause; fixing the format cannot help a reply that was cut off.

**Inputs, in order (FR-002, FR-003)**:

1. the **structured finish reason**, when the path carries one — authoritative
2. the existing **prose markers**, otherwise — so no backend loses coverage it has today

The second is a fallback, not a replacement. Some backends surface only a human-readable
reason string.

## Derivations built on FailureShape (must NOT widen)

The shape drives two operator-facing behaviours. Widening the shape's reach must not widen
these; each keeps its current scope and gets a regression test.

| Derivation | Where | Current scope | After |
| --- | --- | --- | --- |
| `is_env_blocked` | `handle_system_error` | `stage == "assessing"` and shape is `empty_body` | **unchanged** |
| retry trigger | `monitor_performer` | any non-`None` shape (so: assessing only) | names the shapes it means, rather than "any shape" |

**Why this is a data-model concern and not just an implementation note**: the shape is
currently doing double duty as both a classification and an implicit permission. Separating
them is the substance of US1.

## TruncationOutcome (behavioural rule, FR-008)

A `truncated` shape must not produce an unchanged retry: same prompt and same budget
truncate identically, burning the retry budget to reach the same place.

Permitted responses:

- retry with a **larger output budget**, or
- do not retry, and report that the budget was exhausted (FR-007)

Forbidden: retry with the request unchanged.

## ReasoningPolicy (new, US2 — research R4 resolved)

An optional property **of a model**, not of a role and not global, because the correct
value differs per model (research R2).

| Field | Type | Notes |
| --- | --- | --- |
| policy | optional | Absent by default. Absent MUST produce a byte-identical request to today's (FR-010). |

**Validation (FR-012)**: an unrecognised value is rejected when configuration loads, naming
the offending value — never forwarded to a model that would ignore or misread it.

**Carrier**: the existing 080 catalog. `model_endpoints` already names the model;
`resolve_performer_dispatch_model` already resolves a role into dispatch context. The policy
rides the same path, so it reaches whatever backend the role uses.

## PolicyEvidence (US3)

Per-model, and three-valued on purpose:

| Value | Meaning |
| --- | --- |
| `measured_beneficial` | observed to help this model |
| `measured_harmful` | observed to break this model |
| `not_measured` | unknown |

**Why not a boolean**: "off because measured harmful" and "off because nobody tried" look
identical as a bare absent flag, and the difference is the whole point. `glm-5.3-flash` is
`measured_harmful` — enabling it moves the thinking into `content` and stops the output
parsing (R2). FR-017 pins that so the finding cannot be lost to a later tidy-up.

**Invariant (FR-015)**: the model all roles currently use ships with no policy.
