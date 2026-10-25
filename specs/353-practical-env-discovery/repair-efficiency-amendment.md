# Repair efficiency amendment

Status: the maintainer asked on 2026-10-24 to fix every suggestion from the post-v40 repair analysis, after Dettmore v40.

## Problem

An execution repair starts almost from scratch. In v40 the only failing command was `install-helm-gh`, yet the repair
took 95 minutes and 51 model calls:

- Every check group re-authored a full part. The saved parts never matched because their key hashes the whole brief.
- Every part was reviewed again on every revision round.
- Each author prompt carried the full previous manifest and all 147 inputs, about 206K input tokens per call
  (44K in the first generation).
- The freeze covers only commands that actually ran. v40's first generation stopped at preflight, so nothing was
  frozen and 8 of 9 install commands were rewritten.
- Checks grew from 48 to 82; 34 of the new checks duplicated existing ones, 33 with identical argv.
- Relocating the cache retired every probe observation, so each repair needed a fresh probe batch and approval.
- The rewrite introduced new failures: a connectivity check gating every install, `dockerd: not found`, a missing
  `/usr/lib/postgresql/17` and a missing activation path.

v39's repair changed only two install commands and took 21 minutes with 11 calls, so a narrow repair is achievable.

## Decisions

The rows below are numbered as in the analysis.

### 2. Freeze, diff guard and duplicate content

- **Wider freeze.** On an execution repair, every record outside the failure closure (defined in row 1) is frozen,
  whether or not it ran. An author's change to a frozen record is a correctable admission error that names the ids.
- **Diff guard.** The guard compares the candidate against the accepted manifest that failed. Any changed or removed id
  outside the closure is rejected with a named code. Added records are allowed; row 1's review judges them.
- **Duplicate content.** Two checks, or two install commands, with the same argv, cwd, env and scope are rejected at
  admission. This is a named, correctable error listing both ids, and it applies on every path, not only repairs.

### 1. Delta repair (with 4, 5, 6 and 7)

- **Triage (daemon, no model).**
  - The roots are results that `fail` or are `indeterminate`.
  - `not_run` results whose reason is a failed prerequisite or preflight are consequences, not roots.
  - The closure is the roots, their `depends_on` ancestors and dependents, and the criteria and requirements linked to
    any of them.
  - Each root is classified as command, activation or path, service, capability, or network gate.
  - The triage is persisted on the repair journal before any author call, so a restart resumes it.
- **Patch-only authoring (rows 5 and 7).**
  - Only the check groups (and the global part) that own a closure record are re-authored.
  - Each touched author returns a patch of changed and added records, never the unchanged ones.
  - Its prompt carries the closure records, the bounded failure evidence and the input evidence for those records. It
    does not carry the full manifest or every input.
  - Untouched parts are carried over unchanged. Saved-part keys use the triage digest, so a restart reuses finished
    parts.
- **Apply and admit.** Patches apply to the failed accepted manifest. The row 2 guards, obligation preservation and
  every existing admission rule run on the result.
- **Scoped independent review (row 6).**
  - A fresh reviewer, with no author conversation, sees:
    - the diff;
    - the touched criteria and requirement links;
    - open findings;
    - the rejected execution evidence.
  - It must give a ruling for every touched criterion, every changed or added record, and every criterion whose
    mapping lost a check.
  - An unchanged record keeps its prior standing only when its digest and the digests of all its cited inputs match the
    reviewed manifest.
  - Review revision rounds re-judge only the parts whose content changed since the previous round.
- **Probe reuse (row 4).**
  - Observations stay current when the only capability change is `cache_mount_path` and no probe command references
    the cache path.
  - A root classified as capability still forces a fresh batch.
- **Escalation.** If a delta repair's patch cannot be admitted within the schema budget, or the review rejects it, the
  attempt falls back to the existing full repair while budget remains. The full repair stays as the backstop.
- **Default.** Delta repair is the default. A config switch turns it off, falling back to the full repair.
- **Unchanged.** Execution itself does not change: it still uses a fresh cache generation, one manifest digest for
  installation and the clean consumer, and a per-execution approval.

### 3. Static admission rules

These causes repeat across runs. Each becomes a named, correctable admission error during discovery, so it costs one
schema round instead of an execution repair:

- **Connectivity gate.** An install command that only tests reachability (for example a HEAD request, a download to
  `/dev/null`, `ping` or `nc -z`) must not be a `depends_on` prerequisite of another command.
- **Service executable.** A service's start command must invoke an executable that the probe evidence shows on the
  base image, or that an install command in the manifest produces or names.
- **Activation path.** A PATH (or similar) entry under the cache mount must be created or populated by some install
  command.

Each rule must avoid false positives on the accepted Dettmore manifests from v36 to v41. Any record a rule would flag
there must actually have failed in execution.

### 8. Critical path

The global author and the check-group authors run concurrently whenever a group does not need the new global part's
content. Merge-time checks are unchanged. The concurrency cap stays as configured.

### 9. Prompt caching

- Check whether the model endpoint reports cached prompt tokens, and fix usage parsing if it is dropped.
- Order prompts so the stable prefix (instructions, then source evidence) comes first and per-call content last, so
  any prefix cache the endpoint has can hit.
- No provider-specific cache controls.

### 10. Guest emulation

- **What is true.** The private service guest is `linux/x86_64` on an aarch64 carrier, so installs run under
  cross-architecture TCG emulation.
- **Timing.** Record per-command installer and verifier durations in the execution report and the run events, so the
  cost of emulation is measured.
- **Acceleration.** When the carrier exposes hardware acceleration for the guest's architecture (KVM), use it, with TCG
  as the fallback.
- **Not decided here.** Changing the guest architecture is a separate decision for the maintainer, informed by the
  timing data.

### Not adopted

- **Row 11, skipping scope review on reuse.** This would weaken independent review.
- **Row 12, approval latency.** It is not a material cost.

## Constraints

- No change to the persisted or wire manifest schema, or to its canonical digest.
- Journal or snapshot additions are backward-compatible and default to empty.
- Corrections never echo model text. Ids pass through the existing safe-id filter.
- Budgets are not raised.
- No model-specific behaviour.

## Acceptance

- Focused tests cover each rule and each guard.
- Replay the v40 failure fixture (first-generation manifest and rejected report): the delta repair touches only the
  closure, adds no duplicate checks and reuses the probes.
- The next Dettmore run's repairs, if any, are measured against v40's 95 minutes and 51 calls.

## Independent revision budgets

Maintainer decision, 2026-10-25: allow eight scope revisions and eight executable revisions independently in sharded discovery. Scope reviews that request revision, including sharded reviews, and runtime release reopenings consume `max_scope_revisions` (default eight); executable review revisions and `observe_sources` reopenings consume the existing `max_coverage_revisions` budget. Exhaustion holds with `attempt_scope_revision_limit` or `attempt_coverage_limit` respectively. Diagnostics persist both counters and maxima. Older journals charge their shared count to both budgets and preserve their maximum. Deadlines, calls, reads, execution repair limits and cleanup requirements remain unchanged.

## Activation environment correction encoding

Dettmore v45 exhausted schema corrections when its final `value` was a JSON-encoded activation environment object instead of a plain object. The patch reader accepts one extra JSON layer only at `manifest.activation.env`, when replacing an existing object with an object, as required by that field's schema. Every other path retains literal `value` semantics, including individual environment entries. Malformed JSON, duplicate keys, nonfinite values and other JSON types receive no conversion. The corrected declaration still passes all existing admission rules, and budgets remain unchanged.

## Conversation correction context

Dettmore v45 also exposed shared reply metadata leaking between concurrent authors: a global manifest correction was labelled `shard_checks`, and its field correction path was omitted. Corrections now receive immutable conversation context containing the requested envelope, patch permission and that reply's finish reason. Envelope and finish metadata are captured before awaited operations; parallel authors continue to share the same resource counters and limits. Deterministic overlap tests and saved-feedback replay must pass locally before another live validation run.

## Validation recorder concurrency

Each validation call reserves its transcript and event ID before awaiting a reply. Concurrent completions cannot overwrite each other's transcript; saved records include the call ID and trusted phase. Independent review invocation counters use the orchestration phase rather than inspecting prompt JSON, including divided source and executable reviews. Continuations do not start another invocation. These counters report that review was attempted; they never establish acceptance or readiness.


### Retained source evidence reaches review

Source review plans include every retained input, including evidence discovered during construction or repair after the scope definition was authored. Unread inputs schedule their owning reviewers or bounded reference reviewers, and those reviewers receive the verified contents as mandatory evidence. A completed review therefore covers the newly retained files and does not reopen for the same unreviewed input. Required criteria, finding decisions, source identities and revision budgets remain unchanged.
