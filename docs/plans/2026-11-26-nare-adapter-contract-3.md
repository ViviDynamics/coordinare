# nare adapter: run against nare contract 3 under the name nare

Issue #360. Part of #381 (nare as the default harness for every persona).

## Scope

In: the `nare` performer backend runs against the released nare (2026.11.109,
contract 3), is accepted by config validation, is advertised by the capability
probe, is installed in the performer base image, and gets its provider rail and
key from the role's endpoint.

Out: making nare the default for any role (#382), and env bootstrap's internal
conversation loop (#376).

## Assumptions

- `driver` never ran against a released nare, so it is removed rather than kept as an alias.
- nare is installed in its own virtualenv in the base image, not added to the performer's
  lock: its `anthropic>=1.6` would move the performer's runtime `anthropic` from 0.104 to 2.x.
- The end-to-end test installs the same pinned wheel as the image, checked against the same
  SHA-256, instead of adding a dev dependency for the same reason.
- No `--stream`: without it the final reply arrives as one `output` event, and streamed text
  deltas are not marked apart from status progress in contract 3.

## Tasks

- [x] 1. Rename `driver` to `nare` everywhere it is registered: registration tests.
- [x] 2. Contract 3 argv, prompts and persona by file, resume repeats run flags: `test_nare_backend.py`.
- [x] 3. Result `text`/`output`, `usage.total`, cost-event `detail`, tool result events, tool-call cap: `test_nare_contract.py`.
- [x] 4. JSON-only closing for verdict roles: `test_verdict_roles_end_with_the_json_only_instruction`.
- [x] 5. `_cancel_reader` awaited before a feedback resume: `test_relay_feedback_cancels_the_old_reader`.
- [x] 6. Daemon side: `endpoint_kind`, `_inject_nare_secrets`, validation, effort, preset entry, proxy seam: `test_360_nare_backend_wiring.py`, `TestJobPayload`.
- [x] 7. Base image installs the pinned release; entrypoint and slim image know `nare`.
- [x] 8. The real binary end to end on nare's fake provider: `test_nare_real_binary.py`.
