# Reviewed environment repair validation

## Readiness decision

The implementation has passing CI and focused regressions, but **real environment acceptance remains unmet**. Keep PR #354 and issue #353 open. A model verdict or green Coordinare CI cannot waive missing clean-consumer execution for sample and Elm.

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

Registry cleanup now removes the owned container's anonymous volumes with `docker rm -f -v`. A resource-lifecycle regression failed before the fix, and all 17 focused harness/image tests pass afterward. This setup failure consumed no Elm discovery budget. The new experiment retains the earlier low-reasoning model settings, raises phase and aggregate budgets to 1,800 seconds, and also raises the private test instance's HTTP request timeout from 180 to 1,800 seconds. Production defaults remain unchanged. Its outcome is pending and cannot waive independent review or required clean execution.
