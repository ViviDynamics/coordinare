# Feature Specification: the global config write requires a version

**Feature Branch**: `157-require-config-hash`
**Created**: 2026-08-31
**Follows**: #237 / spec 156, which raised this as an open decision

## Context

Spec 155 gave `PUT /api/config/global` an **optional** `expected_hash`; spec 156 made the
Global Config page send one and logged the writes that still arrived without. Both left the
endpoint accepting unguarded writes, because refusing them is a contract change and nobody
could weigh it without knowing how often they happened.

That question is now answered: every first-party caller sends a version, so what remains is
automation writing config with no protection against overwriting a concurrent edit. Silently
losing someone's change is worse than a loud failure a script can be taught to handle, and
pre-launch is the cheapest moment to make the change.

## Requirements

- **FR-001**: `PUT /api/config/global` MUST refuse a request with no `expected_hash`.
- **FR-002**: The refusal MUST be **428 Precondition Required** — the request is well-formed
  and what is missing is a precondition (RFC 6585 §3), not a malformed body.
- **FR-003**: The refusal MUST say how to obtain the version, not merely that one was
  missing.
- **FR-004**: A refused request MUST NOT modify the file.
- **FR-005**: The Global Config page MUST fail closed when it has no version, explaining what
  to do rather than surfacing a bare 428.
- **FR-006**: Refusals MUST be logged, so an operator can see which automation needs updating.
- **FR-007**: A stale version MUST still be refused with 409, unchanged.

## Success Criteria

- **SC-001**: No caller can write global config without a version.
- **SC-002**: The refusal names `expected_hash` and points at the `ETag`.
- **SC-003**: The page never sends a versionless write.
- **SC-004**: Every test that wrote without a version is updated deliberately, and the ones
  written to pin the *old* contract are inverted with the reason recorded — not deleted.

## The breaking change, stated plainly

Any operator automation that posts to `PUT /api/config/global` begins failing with 428 until
it sends a version. That is the intended effect. Eight call sites in this repository wrote
without one and are updated here, which is a fair proxy for how much else may need it.

Callers that cannot easily hold a version can read one immediately before writing
(`GET /api/config/global` → `ETag`). That reintroduces a race for *that* caller — but a
narrow one it opted into, rather than a silent one applying to everybody.

## Review outcome

Nothing survived refutation. Two of the refuted findings were right about the code and wrong
about the scope, which is worth recording rather than filing away with the rest:

`POST /api/veto` and `DELETE /api/personas/{role}` also write `config.yaml`, and neither takes
a version. That is true, it is not introduced here, and it is not this spec's to fix — but it
is *more* visible after this change, because one endpoint now refuses a versionless write
while its neighbours accept one silently. Filed as **#241**.

The bypass surface was probed directly rather than reasoned about: omitted and null give 428,
empty string and bare quotes give 409 (they are a version that does not match, not an absent
one), and non-string types give 400. No shape modifies the file. That table is a test.
