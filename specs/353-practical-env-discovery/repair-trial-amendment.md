# Amendment: repair trials on the failed generation's installed disk

Status: approved by the maintainer on 2026-11-11. This narrows two earlier rulings:
- "A repaired generation must never mount or reuse the failed generation" (repair-amendment.md)
- "Do not reuse a failed installation cache", under which probes get an empty cache (executable-preflight-plan.md)

The narrowing covers diagnostic trials only.

## Why

Dettmore e2e-81 spent its four execution repairs on mistakes in its service health and check commands:
- an unknown client call
- an unquoted SQL string
- a missing password
- an unset forwarded variable

Each repair cost a full fresh execution. Most of that time was install steps that had already passed: about 10 to 16 minutes of a 15 to 31 minute execution (a Ruby build, gems, an image pull).

## Ruling

1. **Trial disk.** An installer run can stop with every install passed, a confirmed clean teardown, and a failed service or check. Such a run seals its guest disk as `trial.qcow2` and binds it with a `TrialReceipt`.
   - A `TrialReceipt` is a separate type. It is never a `runtime_artifacts` receipt.
   - A failed report still carries no `runtime_artifacts`, so a trial disk can never become consumer, readiness or provenance evidence.
2. **Trial base.** The failed generation's cache directory, accepted manifest, source identity and trial receipt become the repair's trial base. The cache directory is kept until the repaired generation's execution starts, and is then removed as before.
3. **Trial run.** During an execution repair with a trial base, the repair author may request a trial instead of a declaration. The trial:
   - names complete replacement services and the checks to run;
   - runs in the clean consumer's isolation: cache read-only, outer network none, fresh local credentials;
   - boots `trial.qcow2` through an overlay that is discarded afterwards;
   - runs the base manifest with only the author's services replaced and only the named checks. Installs, activation and runtime endpoints cannot change in a trial.
4. **Results.** Trial results reach the author as untrusted observations, bounded and redacted like execution feedback. They never count as evidence. A repaired manifest still needs:
   - a fresh independent review;
   - a fresh cache generation that reruns every install;
   - a full execution;
   - the clean consumer.
5. **Bounds.** Each repair may run at most `MAX_TRIALS_PER_REPAIR` trials. Each trial is capped by the remaining attempt deadline and by the runtime's own session limit. A trial that cannot run (no base, a refused identity, an executor failure) is reported to the author as unavailable, and the repair continues normally.
6. **Unchanged.** All of these stay as they were:
   - executed checks remain fixed;
   - retirement rules;
   - repair counters;
   - per-execution approval;
   - fresh-generation execution and clean-consumer verification.

## Scope of this change

- The trial disk is sealed on the private-runtime installer path that the validation harness uses.
- The trial executor is wired in the validation harness. Production gets trials once its installer path seals trial disks and supplies an executor. Until then no trial base exists there, and repairs behave exactly as before.
