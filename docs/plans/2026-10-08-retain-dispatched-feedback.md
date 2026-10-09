# Retain dispatched feedback across worker replacement

Issue #549

## Scope
In: retain the feedback batch delivered to a foreground performer until successful stage advancement; persist it and replay it to replacements of that stage.
Out: reviews arriving during work (#548), board pause behavior (#547/#552), PR conversation polling (#550), and external PR closure (#551).

## Assumptions
- Accepting a dispatch does not acknowledge completion of its feedback.
- Completed stages must not leak feedback into subsequent roles or other cards.
- Failed dispatches and unsuccessful turns preserve the pending batch.

## Tasks
- [x] 1. Reproduce dispatch → snapshot → restore → replacement payload losing body and inline feedback.
- [x] 2. Persist the in-flight batch with its owning stage, replay it to replacements, and retain newly queued feedback independently.
- [x] 3. Clear the batch on successful stage advancement and card retirement, with legacy snapshot and sibling isolation coverage.
- [ ] 4. Run full preflight, adversarial review, Copilot review, required CI, and merge.

## Final PR-check acknowledgement review fix

Independent review reproduced an original-request loss through real terminal-success S3/S4 phases: final-stage advancement cleared the batch before the closer PR-checks gate could HOLD or BOUNCE. Final-stage acknowledgement now waits for that gate's PASS. HOLD and missing PR fields retain the batch; BOUNCE carries original, queued and CI repair feedback together, including reviewer-to-implementer transitions. The deferred ephemeral implementer path also applies the closer PR-check gate before acknowledgement. Local lint bounces remain retained.

Eleven new red-to-green cases cover final implementer/reviewer HOLD, BOUNCE, PASS and missing PR fields, plus deferred ephemeral HOLD/BOUNCE/PASS. The bounce assertions execute the dispatch payload, and held/bounced/missing-PR batches survive snapshot persistence and restore. Verification: 482 related tests pass (22 issue cases), Ruff and strict mypy (205 modules) pass, and the Ruff debt ratchet holds without baseline/suppression changes. Root owns independent review and the final preflight.
