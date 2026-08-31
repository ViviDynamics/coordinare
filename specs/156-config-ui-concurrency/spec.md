# Feature Specification: The config UI stops overwriting concurrent edits

**Feature Branch**: `156-config-ui-concurrency`
**Created**: 2026-08-30
**Issue**: #237 (follow-up to spec 155 / #202)

## Context

`config_write_service` has had optimistic concurrency since spec 081 — `compute_content_hash`,
`guard_concurrency`, `ConcurrencyConflictError` — and the catalog and routing endpoints use it: a
write built against a stale version of `config.yaml` is refused with a 409.

`PUT /api/config/global` never did. Spec 155 gave it an optional `expected_hash` so the config
assistant's writes would be guarded, and deliberately left the older Global Config page alone —
changing a surface that spec was not asked to touch would have been how an unrelated regression
arrives. This finishes it.

Two operators with the dashboard open, or one operator in two tabs, can currently overwrite each
other's global-config edits silently, while the same collision on a catalog entry is correctly
refused.

## The obstacle worth naming

The Global Config page cannot send a hash today because it has no way to obtain one. It loads
from `GET /api/config/global`, which returns values and nothing else; the hash lives in
`GET /api/config/all`, a much heavier call used by the other config page. And the response body
of the values endpoint is pinned by an existing test to an exact key set, so the hash cannot
simply be added to it.

## Clarifications

- Q: Add the hash to the `GET /api/config/global` body? → A: No. An existing test asserts that
  response's exact key set, and it is right to: that endpoint is a values contract.
- Q: Make the page call `/api/config/all` for the hash? → A: No. It would fetch every section's
  descriptors to read one string.
- Q: Then how? → A: an `ETag` on the values response. It is what the header is for, it adds
  nothing to the body, and it breaks nothing.
- Q: Should the endpoint *require* a hash rather than accept one? → A: Left open deliberately;
  see "Open decision" below. This spec makes every first-party caller send one.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A concurrent edit is refused, not silently lost (Priority: P1)

Two operators edit global config. The second save is refused, and the operator is told to reload
rather than discovering later that their colleague's change vanished.

**Why this priority**: it is the defect.

**Independent Test**: load the page, change the file underneath it, save, and see a refusal with
the file intact.

**Acceptance Scenarios**:

1. **Given** the page was loaded before another edit landed, **When** the operator saves,
   **Then** the write is refused and the other edit survives.
2. **Given** no concurrent edit, **When** the operator saves, **Then** it succeeds as before.
3. **Given** a refusal, **When** the operator reads the message, **Then** it says the
   configuration changed and to reload — not a raw status code.

---

### User Story 2 - The page can obtain the hash cheaply (Priority: P1)

**Why this priority**: US1 is not implementable without it, and the obvious routes are barred —
one by an existing contract test, the other by cost.

**Independent Test**: `GET /api/config/global` carries an `ETag`, and its body is unchanged.

**Acceptance Scenarios**:

1. **Given** a config file, **When** the values are fetched, **Then** the response carries an
   `ETag` matching the file's content hash.
2. **Given** that response, **When** its body is compared to before, **Then** it is unchanged.

---

### Edge Cases

- **No config file.** The endpoint already reports that; it must not now fail differently.
- **A caller that sends no hash.** Still accepted, and now visible in the logs rather than
  silent — see the open decision.
- **A hash for the wrong file.** Refused, like any other mismatch.

## Requirements *(mandatory)*

- **FR-001**: `GET /api/config/global` MUST carry the config file's content hash as an `ETag`.
- **FR-002**: That response's body MUST be unchanged.
- **FR-003**: The Global Config page MUST send the hash it loaded with when saving.
- **FR-004**: A save built against a stale hash MUST be refused, leaving the file untouched.
- **FR-005**: The refusal MUST be explained in the page in plain words.
- **FR-006**: A save with no hash MUST still be accepted, and MUST be logged as unguarded.
- **FR-007**: No existing endpoint behaviour may change for callers that send no hash.

## Success Criteria *(mandatory)*

- **SC-001**: A stale save from the Global Config page is refused and the concurrent edit
  survives.
- **SC-002**: The values response body is byte-identical to before.
- **SC-003**: An unguarded write appears in the logs, so the remaining gap is measurable rather
  than invisible.
- **SC-004**: No existing test needs its assertions weakened.

## Open decision (for the operator, not for this spec to settle)

Whether `PUT /api/config/global` should **require** a hash. After this spec, every first-party
caller sends one, so requiring it would close the gap completely — at the cost of breaking any
operator automation that posts config, which would begin failing with a 428. Six call sites in
the test suite write without one today, which is a fair proxy for how much else would need
updating. Pre-launch is the cheapest moment to make that change; it is also a contract change,
which is why it is named here rather than made quietly.

## Out of Scope

- Requiring the hash (above).
- The catalog and routing endpoints, which already guard correctly.
- Authentication (spec 143 / #197).

## Review remediation (adversarial `Workflow`, 3 confirmed / 3 refuted)

- The `ETag` was a bare `sha256:...`. RFC 7232 §2.3 wants a DQUOTE-enclosed opaque tag; the
  bare form works for our own string comparison and is still a malformed header — the kind
  of thing that works until something between the server and the browser starts caring. Now
  quoted, and the endpoint accepts either form, because a client reading the version from
  the header sends it back with quotes while one holding it from `new_hash` sends it bare.
  Without that tolerance, quoting the header correctly would have made every save 409.
- The test for it stripped quotes before comparing, which is a no-op on an unquoted value —
  so it passed whether or not the header was well-formed. It accommodated a mismatch instead
  of catching one. It now asserts the format.
- The third finding reported the repeat-save fix as uncommitted. It was stale: the review
  snapshotted `HEAD` a moment before that commit landed. Recorded rather than dismissed,
  because "the reviewer is looking at a different tree than you are" is worth recognising
  quickly next time.
