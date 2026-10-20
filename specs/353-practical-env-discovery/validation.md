# Reviewed environment repair validation

## Readiness decision

This report records earlier implementation validation; its head-specific CI results below are historical. The latest [validation results](validation-results.md) retain a required dashboard latency CI failure and **real environment acceptance remains unmet**. Keep PR #354 and issue #353 open. A model verdict or green Coordinare CI cannot waive missing clean-consumer execution for sample and Elm.

## What was verified

The declaration-only model boundary leaves provenance construction to the daemon. Focused tests cover independent source reads, immutable runtime identity, required obligation preservation, durable shared budgets, restart, new cache generations, redacted rejected evidence and cleanup refusal. A real shared-runner regression installs a source-path-dependent artifact, observes clean-consumer failure, obtains a portable repaired contract and fresh review, reruns required checks and freezes the new cache while preserving rejected evidence.

Fresh independent adversarial review of the amendment found two Important production gaps: best-effort snapshot persistence and dispatch outside the deadline before polling. Both were reproduced and fixed with regressions. The related 178-test set passed. CI subsequently exposed operator wording and redacted error-writer requirements; both were reproduced and fixed, with 72 focused tests passing and those guards added to the private focused preflight map. Lint, strict types and the committed lint ratchet remain unchanged.

All 27 CI checks passed on implementation head `2729753161d911ad23273cc765a5390d0e12f279`: [CI run](https://github.com/ViviDynamics/coordinare/actions/runs/39064661419). A later documentation commit does not imply that this run tested that new head.

## Pinned live profiles

These are direct production-service entrypoint tests using immutable candidate images containing the checkout's shared runner. They do not claim a full live daemon or Kubernetes deployment test. Model credentials remained outside project containers. No handcrafted success manifests or operator scope waivers were used.

The default model profile used `spark/glm-5.3-flash`, 32,768 maximum output tokens, the default reasoning setting, a 120-second phase budget and a 600-second aggregate budget. The separate longer profile used 8,192 maximum output tokens, low reasoning, 600 seconds per phase and 1,200 seconds overall. All existing source snapshots and prior failed results were preserved.

| Profile / implementation revision | Targets | Result | Installation / clean consumer | Ready |
| --- | --- | --- | --- | --- |
| Default / `95dc689a` | Pinned sample and Elm | Both discovery phase deadlines | Neither ran | 0/2 |
| Default / `2c162b76` | Other 27 baseline eligible repositories | All 27 discovery phase deadlines | Neither ran | 0/27 |
| Longer, low reasoning / `95dc689a` | Pinned sample | Review assessment rejected for unread source citations; 11 calls, 90.17 seconds | Neither ran | No |
| Longer, low reasoning / `95dc689a` | Pinned Elm | Discovery deadline after provider read timeouts; 7 reserved calls, 600.28 seconds | Neither ran | No |

The sample is pinned at `64dbb0b613884a024691a8e3eb1724ea4a1fc787`; Elm at `bc224a78db6f669d56863599d653f14e7605de0b`. The unchanged baseline selection contains 29 targets, excluding the same four Rails-primary and three archived repositories. Full per-repository reports, exact image identities, model transcripts and source material remain private.

All 29 default-profile targets held at discovery deadlines across 173 reserved model calls, with zero coverage acceptance, installer, clean-consumer or readiness results. All snapshots are unchanged. All 20 completed profile cleanup ledgers, including historical profiles, were independently checked: every owned image reference and registry is absent. No active validator is retained.

## Remaining limitation

Real discovery still does not reliably produce an evidence-complete, independently accepted contract within the configured bounds. The observed failures are discovery/model protocol and latency failures; no new real installer or clean-consumer result exists to demonstrate executable repair. The synthetic execution regression proves the mechanism, but cannot replace sample and Elm passing every required clean check. No merge or bootstrap success is claimed.

## Implementation decisions and tradeoffs

- One aggregate deadline is the minimum of the manifest-attempt limit, positive outer bootstrap limit and 3,600 seconds; it includes execution, with separate phase deadlines. Up to three rejected generations retain evidence but no active executor. Repair requires a valid full-scope failed report and confirmed cleanup; unknown infrastructure holds. New source identity starts a separate attempt; drift inside an attempt holds. This can hold a slow or unavailable project instead of renewing its budget.
- A rejected report with an actual failed action may include causally skipped dependent checks. Wholly unrun, unknown or malformed evidence cannot request repair. Required obligations remain and every required action must rerun successfully. Feedback is capped at 16 KiB. Treating skipped dependents incorrectly would block legitimate repair; admitting them as success would violate readiness, so they remain rejected evidence only.
- Cleanup drains a started runtime creation before confirming absence, even after the work deadline. This prevents a late Pod/container from appearing after an absence check. Slow infrastructure can delay cleanup; uncertain cleanup cannot authorize another execution, repair or readiness.

## Subsequent 30-minute Elm experiment setup

The user requested a separate 1,800-second Elm experiment. Initial launches failed during temporary image publication, before discovery began: Docker's filesystem was full. A read-only audit matched 22 dangling registry volumes to this validation's exact repository/registry ledgers; those proven-owned volumes were removed. Earlier cleanup evidence covered image references and registry containers, and did not establish anonymous-volume removal.

Registry cleanup now removes the owned container's anonymous volumes with `docker rm -f -v`. A resource-lifecycle regression failed before the fix, and all 17 focused harness/image tests pass afterward. This setup failure consumed no Elm discovery budget. The new experiment retains the earlier low-reasoning model settings, raises phase and aggregate budgets to 1,800 seconds, and also raises the private test instance's HTTP request timeout from 180 to 1,800 seconds. Production defaults remain unchanged. The experiment completed after **635.95 seconds (10 minutes 36 seconds), without timing out**, on `e73089a7`. It made nine model calls and reached two fresh independent reviews. The first requested two missing test lanes; discovery revised the proposal. The second flagged a tool-version conflict and read only four of the eight mandatory manifest inputs (plus one additional file), so the daemon rejected the assessment with `review must independently read every manifest input`. The shared two-repair budget had already been consumed. No independently accepted manifest, installer, clean-consumer or readiness result followed.

The longer timeout allowed discovery and coverage review to progress, but did not establish a usable environment. The pinned Elm snapshot is unchanged. All 25 completed profile image/container cleanup ledgers are confirmed absent, and the latest registry's recorded anonymous volume was independently confirmed absent after the cleanup fix. All 27 CI checks passed on `e73089a7`; this later result-documentation update is separate from that tested code head. Independent review and required clean execution remain mandatory.

## Complete-evidence amendment: final repeated repository matrix

The user approved the written complete-review-evidence specification and implementation plan, then requested implementation and the same repository set again. The implementation independently prepares mandatory reviewer evidence, separates durable schema/coverage/execution correction allowances (two each), versions the journal, and retains the original aggregate limits. Explicit operator regeneration now archives bounded prior evidence and starts an empty cache sibling only after establishing executor cleanup when needed. Cancellation cannot bypass cleanup by changing source identity, and schema correction retains valid required scopes and lifecycle obligations.

The new matrix ran at **`f35c8e8d`**, against exactly the same **29 unique pinned repository snapshots** as the preceding default matrix, with unchanged model, reasoning/output and timeout settings. One shared owned candidate-image registry served all four profiles. **`c484ab39`** subsequently changed only dashboard reset-response decomposition; discovery and execution logic is identical. All **27 CI checks passed on `c484ab39`**, independently confirmed against the PR head.

| Profile | Settings | Observed outcome |
| --- | --- | --- |
| Same 29 default repositories | GLM-5.3 Flash, 32,768 output-token allowance, no effort override; phase 120 seconds, aggregate 600 seconds | **29 held**, all `phase_deadline` during discovery, approximately 120 seconds each. **158 JSON-chat calls**, zero independent review invocations. |
| Separate Elm experiment | Same model, 8,192 output-token allowance, low reasoning; phase/aggregate/private HTTP timeout 1,800 seconds | Held after **714.45 seconds (11m54s)** with `attempt_schema_limit`: six JSON-chat calls, 14 source reads, two schema corrections, zero coverage revisions or executable repairs. No independent review invocation. |

Elm's first complete proposal omitted the required `manifest` envelope. Its next proposal supplied the envelope but failed activation idempotence. The third fixed activation; binding the full contract then rejected clean checks depending on install-only action IDs (`clean_dependency_install_only`). Revalidating the saved final proposal against the actual pinned input hashes and runtime identity reproduced that final error. The schema allowance was exhausted, so no further correction was requested. This was **not a timeout**. Earlier failed proposals and transcripts remain preserved privately; no budget was renewed.

Across these new profiles, **no manifest received independent coverage acceptance, no installer or clean consumer ran, and no environment became ready**. No executable-scope approvals were issued. Consequently the matrix does not demonstrate that mandatory-evidence delivery or executable repair works on a real accepted consumer contract. Those paths have deterministic regression coverage, including a scripted Elm failure sequence and actual local runner repair, but that evidence cannot replace real repository success. Model protocol compliance and discovery latency still prevent live acceptance under these settings.

Every snapshot's integrity check passed. All four profile processes completed; their exit status was one because results were held. The parent orchestration's zero exit status meant the batches finished and cleanup ran, not that environment validation succeeded. All 26 historical/current image-reference and registry cleanup ledgers were independently reconciled with actual Docker absence checks. The new shared registry's recorded anonymous volume was separately confirmed absent. No global prune or unrelated-resource removal was performed.

The amendment's fresh independent full-diff review found three Important issues: cancellation/source-change cleanup bypass, operator reset retaining an unusable journal, and required scope/lifecycle weakening during schema correction. Each was reproduced and fixed in one pass with failing-then-passing regressions. Strict mypy, unchanged lint/debt checks, focused preflight and the broad CI suite pass. CI exposed the existing dashboard function-size guard after the endpoint change; extracting reset-response handling fixed it, with 77 related tests passing. The guard now runs in focused preflight; no threshold or baseline was weakened.

One Minor review finding remains deferred: on review-context overflow, diagnostics can show zero or the previous measured byte count and omit the configured cap. The context admission guard still refuses the oversized request. Provider input-token capacity is unavailable through the current backend, so the explicit byte cap is not a guarantee of provider context fit; provider failures hold without being misclassified as schema corrections. Actual Kubernetes deployment cleanup and live daemon/outer-loop operation were not established by this field matrix.

**SC-004 remains unmet.** Sample and Elm must both pass mandatory independent coverage and every required clean-consumer check before merge. PR #354 and issue #353 remain open; this report records completed test attempts, not a successful bootstrap.


## Complete execution-source correction

The Elm profile on `01315ab6` ended held after 1,200.472 seconds. Its final
installation passed; seven of nine clean checks passed. The build and scaffold
checks failed because the verifier had copied review files rather than the full
repository. This was not a timeout. The original profile and rejected generations
remain unchanged.

The approved source amendment and implementation plan separate bounded model
review evidence from complete binary execution source. The focused regression
suite covers unreviewed source directories, binary bytes, executable permissions,
safe symlinks, content/commit drift, archive/transport limits, cancelled acquisition,
source ownership, preserved budgets, current-generation result attribution and
ordinary-name consumer activation. Docker and Kubernetes use the same validator.

A source-only test of pinned Elm commit
`bc224a78db6f669d56863599d653f14e7605de0b` restored 6,616 tracked files totaling
77,426,246 bytes, including the missing `control-plane` directory. Restored source
matched the pinned checkout's canonical identity in 8.366 seconds. Independently
fetching the real GitHub tree/archive produced the same source digest in 9.202
seconds; the normalized binary transport was 29,661,409 bytes. Source digest:
`6ba372143332703eb4275ae39e01b166c30428960d94b636c1f74652bc4a042d`.
These source-only tests ran no repository commands and do not establish readiness.

The full Elm simulation on `4ec5d59a` completed held after 441.79 seconds,
inside the approved 3,600-second phase/aggregate/provider/installer allowance.
It used the same GLM-5.3 Flash model, low reasoning and 32,768 output-token limit.
Eight capability probes ran against the complete source with matching identity
and confirmed cleanup. The fresh reviewer received all 14 mandatory inputs and
returned `revise`, identifying incomplete functional checks and a tool-version
conflict. Its citations also included three non-file labels: `manifest.checks`,
`manifest.requirements` and a probe-results label. Citation validation rejected
that assessment before discovery could receive its revision feedback. Six model
calls and one schema correction were consumed; both coverage revisions and one
schema correction remained. No installer or clean consumer ran. This was not a
timeout. Snapshot integrity and absence of the owned image references, registry,
registry volume and recorded source directory were independently verified.

Offline replay reproduced the exact citation failure: formal assessment-schema
validation happened inside the correction loop, but file-citation binding happened
after it. The user approved a narrow fix. Invalid citation references now use the
existing schema correction allowance and require a new reviewer response. A
valid revision reaches discovery and its next candidate receives fresh independent
review. Source drift, missing evidence, budget exhaustion and every required
execution check retain their guards. Six regression cases pass; an offline replay
of the saved Elm response reaches valid `revise` using a diagnostic corrected
reply, without model calls, commands, approvals or manifest acceptance.

The source amendment's fresh full-diff review found one Critical containment bug
and four Important Kubernetes/recovery/provider-error bugs. All five were
reproduced and corrected. Actual Docker fixture installation and fresh consumer
execution pass; final-head CI and the next real Elm outcome remain pending.
SC-004 and other live-success tasks stay unchecked. Other repositories remain
paused until Elm's independently reviewed installation, usable activation and
every required clean check actually pass.

## Persistent review findings correction (2026-10-18)

The `review-citations-elm60m-low` profile at `4e71edd2` held after 735.15 seconds
with 2864.967 seconds remaining. Three valid independent reviews alternated over
the unresolved consumer capability finding; two coverage revisions were exhausted.
No installer or clean consumer ran. The pinned snapshot was unchanged and every
owned source path, image/registry and volume was independently confirmed absent.

The user approved the persistent-findings specification and implementation plan.
New regressions establish explicit per-finding adjudication, independent evidence,
source/candidate binding, durable ledger and budgets, legacy review invalidation,
and refusal before installer commands. The affected root suite passed 607 cases
with 14 existing platform skips; performer checks passed 282 cases and workflow
checks passed 26. These are protocol/fixture results, not real Elm readiness.
Final-head CI, independent whole-diff review and a new pinned Elm-only simulation
remain pending. No live-success claim or SC-004 completion is made.

The fresh whole-branch review found two Important restart issues: a revision was
checkpointed before its allowance was charged, and a resumed finding ledger lost
prior required candidate obligations. Both were reproduced against the real
journal with a simulated process death. The single fix pass now checkpoints the
charge/hold with the review and retains the latest validated candidate before
review. Rename/scope/required-status regressions reject weakened resumed checks.
The affected suite passed 1003 tests with 14 existing platform skips and four
existing structlog warnings. Strict typing passed for335daemon modules and the
unchanged lint ratchet holds. Live Elm and final-head CI remain pending.
