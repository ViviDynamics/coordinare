# Amendment: complete pinned source for environment execution

**Feature branch**: `353-practical-env-discovery`
**Issue / PR**: #353 / #354
**Status**: Draft for written-spec approval
**Date**: 2026-10-17

## Intent and authorization

The user requested: “Good find! Let’s patch coordinare and run the simulation
again.” The approved direction is to give probes and clean verification the
complete pinned repository while keeping model review evidence bounded. Elm stays
the primary test case, with the same model and 60-minute experimental limits.
Other repositories remain paused until Elm passes.

This amendment extends [spec.md](spec.md) and
[executable-preflight-amendment.md](executable-preflight-amendment.md). It preserves
independent coverage review, deterministic required checks, accepted manifest
identity, bounded repair, and fail-closed readiness. It introduces no language,
framework, or library detection and makes no constitutional change.

## Reproduction and root cause

The Elm simulation on Coordinare `01315ab6` completed in 1,200.472 seconds. Its
final installer passed using accepted manifest
`0d09cf512352910edca3dab593d35906c7cfb5d2d15e0831bd62137f42790e17`.
Seven of nine clean checks passed. The Go build and scaffold checks failed because
`control-plane` was absent from the clean workspace. The actual pinned Elm commit,
`bc224a78db6f669d56863599d653f14e7605de0b`, contains that directory.

`verification_sources()` in
`src/coordinare/services/environment_verification.py` fetches only
`manifest.inputs`. `source_setup()` materializes those review inputs as the entire
repository. `run_owned_probe()` and `execute_environment_probe()` make the same
assumption. The installer, in contrast, receives the complete pinned checkout.

Review evidence is intentionally a small subset of a repository. Treating it as
an executable checkout removes application source, package manifests not read by
the model, assets, binary files, executable modes and symlink semantics. Increasing
model context or requesting more review files cannot correct that boundary.

The pinned Elm tree contains 6,616 blobs totaling 77,426,246 bytes; its largest blob
is 1,172,711 bytes. It cannot fit the existing review-file transport's 256 KiB
per-file and 16 MiB serialized-document limits. These are execution-source needs,
not reasons to weaken review-evidence limits.

A second defect affects the harness summary: after a later installer passes,
`installer_evidence` can still contain an earlier generation's failed report.
The final attempt's own installer artifact is correct. This amendment fixes the
summary's generation attribution without rewriting any prior evidence.

## Approach and alternatives

Use a complete repository tree and binary archive obtained through the repository
provider at the immutable commit, then validate and transfer that execution source
separately from model evidence. The daemon uses API reads and bounded temporary
storage; it does not clone, invoke git, execute repository hooks or run project
commands. Existing Docker and Kubernetes adapters consume the same source format.

Fetching every file individually preserves content but would require thousands of
API calls for Elm. Treating every executable input as a model-review input would
exceed evidence budgets and still lose binary contents and file modes. A pinned
archive with complete tree verification avoids both problems.

## Source contract

1. Keep `EnvironmentManifest.inputs` as the exact hashed evidence that discovery
   and the independent reviewer inspected. Do not add the whole repository to
   conversations, increase evidence limits, or infer execution files from command
   strings, imports, languages or filename catalogs.
2. Resolve the full tracked tree at `source_commit`, including path, Git blob
   object identity and mode. Reject mutable refs, truncated trees, duplicate or
   conflicting paths, unsupported entry types, and unsupported object formats.
   Include binary blobs and executable files. Submodules and LFS pointers that
   require external content produce an explicit unsupported-source hold; do not
   represent them as successfully materialized application content.
3. Obtain a binary archive at that same immutable commit. Check every entry
   against the pinned tree using the appropriate Git blob object hash. Validate
   the complete file set and modes; never trust only an archive filename, HTTP
   status or model-supplied source identity. An archive that omits tracked blobs
   may be completed by pinned blob API reads under the same limits and deadline.
   If completion fails, hold instead of running a partial checkout.
4. Create a canonical execution-source identity over commit, tracked entry
   metadata and verified contents. Bind transport bytes to a SHA-256 digest and
   declared byte count. Preserve this identity in probe/installation/verification
   provenance alongside the unchanged accepted manifest digest and image/platform.
   A provider retry or archive compression change must not change the canonical
   repository content identity.
5. Independently verify every accepted review-input hash against the execution
   source before running commands. For an admitted repository symlink, use the
   same bounded in-tree resolution as evidence fetching. A mismatch holds even
   when the complete archive otherwise validates.
6. The installer's actual checkout must be at the same commit with matching
   tracked content identity before project commands run. It may then receive
   project-local installation changes, as today. The clean consumer starts from
   fresh pinned source and the produced cache; it never copies installer source
   mutations as a substitute for an install action or missing cache content.

## Materialization and transport

Use one shared validator/materializer for probes and clean consumers. Restore into
a newly created directory owned by the adapter. Preserve regular-file bytes and
the executable bit. Reject absolute paths, traversal, special files, hard links,
duplicate paths, file/directory conflicts and unexpected entries before commands
run. Do not use unrestricted archive extraction.

Admit only relative symlinks whose normalized targets remain inside the repository.
Validate links before creating them, detect cycles during bounded resolution, and
never write through a symlink. Reject external or escaping links. Strip ownership,
setuid/setgid and unrelated archive permission bits. Repository contents, including
hooks or archive metadata, are data until an explicitly approved command executes.

Docker probes retain read-only source/cache, disabled network, resource bounds and
per-command container cleanup. Docker verification restores the full source inside
its isolated container before running checks with networking disabled and a
read-only cache. Do not mount a developer's working checkout or Docker socket.

Kubernetes verification transfers binary payload chunks through bounded immutable,
Pod-owned source Secrets, extending the existing projected-volume mechanism.
Neither source bytes nor repository credentials travel in Pod command arguments,
environment strings or logs. The init step verifies transport and source identity
before restoring source. Keep per-object API limits, ownership and cleanup checks.
An adapter that cannot admit the payload holds explicitly; it cannot fall back to
review-only files or another runtime. Kubernetes probe isolation stays unsupported
until separately implemented; this amendment does not add that adapter.

Private GitHub archive/blob acquisition uses the existing repository service's
authentication and safe redirect handling. Do not forward a GitHub token to an
arbitrary redirect host. Project commands receive no archive-fetch credentials.
The private snapshot harness supplies the identical format from pinned Git objects,
not from mutable checkout files, and exercises the production materializer.

## Resource bounds and lifecycle

Execution-source limits are distinct from model evidence budgets. Initial fixed
ceilings are 128 MiB compressed archive/transport, 512 MiB expanded file contents,
64 MiB per blob, 100,000 tracked entries, 256 fallback blob fetches, and eight
concurrent provider reads. Validate byte ceilings while streaming, including
decompression; do not allocate an unbounded body before checking its size.
Exhaustion returns a source-specific hold without executing project commands.

Keep the current 64 KiB transport chunk size and bounded API-object grouping;
derive chunk counts from the execution-source ceiling rather than reusing the
review payload's 256-chunk ceiling. Metadata must itself fit a checked payload,
not an arbitrarily large first Secret. Limit per-object serialized size before
creating resources. Include setup in existing probe/attempt deadlines. The source
transfer must not renew deadlines, repair budgets or model-call/read quotas.

Prepare the validated source once per active pinned target when possible. No
per-file API loop on the normal archive path, no additional model calls and no
repeated full transfer per probe command. Each consumer still receives a fresh
materialization; sharing a mutated workspace is forbidden. A reused artifact must
be revalidated against its digest and target and stay within the same attempt's
ownership/lifetime. Do not introduce a persistent general-purpose source cache.

Record source acquisition/validation/materialization durations, byte counts and
file counts without contents or credentials. Cancellation removes owned temporary
files and source resources, and confirms cleanup before repair. An incomplete
cleanup holds; source transfer never unlocks another execution automatically.

## Acceptance and migration

Old review-only source evidence cannot satisfy new execution-source provenance.
Missing or mismatched source identity requires rebuilding/re-verifying through the
normal gate. Do not relabel older successful reports, reset an active attempt's
budgets, erase rejected manifests, or silently renew an expired attempt. Preserve
the existing explicit restart path after confirmed cleanup.

Fresh independent review remains mandatory. Review instructions must require
declared activation to make installed tools available to actual downstream project
commands; absolute tool paths in checks alone do not establish normal consumer
usability. A missing activation directory must be corrected by installation or a
complete alternative activation, not hidden by removing useful PATH entries.
Check failure, missing source, unknown evidence, image/platform drift and unrun
required checks always reject readiness. No repair-budget or timeout-default change.

The harness publishes only the current attempt's installer and clean evidence in
its final summary. Every record must match that attempt's digest, image/platform
and execution source. Store earlier results under their original attempt paths;
do not overwrite them. A passing installer does not imply passing clean checks.

## Acceptance scenarios

1. A repository has one reviewed build file, application source in another
   directory, a binary asset, an executable script and a safe in-tree symlink.
   Probes and clean verification see all tracked files, correct bytes and modes,
   while the reviewer receives only bounded hashed review evidence.
2. Reproduce Elm's missing-directory failure with a synthetic repository. Its
   required build and scaffold checks pass only with complete source. Removing
   any tracked input, changing the commit, corrupting chunks or providing a
   mismatched review hash rejects execution/readiness.
3. Archive traversal, duplicate members, escaping links, special files, metadata
   overflow, decompression overflow, incomplete provider trees and truncated
   archives hold before commands. Safe files are never written outside the owned
   destination. An unsupported source feature has a specific reason code.
4. Docker probes retain isolation and complete cleanup on timeout/cancellation.
   Docker and Kubernetes clean adapters reconstruct the same source identity.
   Kubernetes source objects stay under API bounds, have correct ownership, and
   are removed after success, failure and partial creation.
5. An installer mutates a source file. The clean consumer receives the original
   pinned file and must obtain installed artifacts only from the approved cache.
   Installed command names resolve using declared activation, and required
   project checks run without installer-only environment state or networking.
6. Two failed generations followed by a passing installer cannot leave the prior
   failed install report in the final current-generation summary. All original
   artifacts and repair consumption remain available and unchanged.
7. Run focused regression tests locally and the broad suite in CI. Repeat only
   Elm at the same pinned commit, model `onprem/glm-5.3-flash`, low effort, 32,768
   max output tokens, and approved 3,600-second phase/aggregate/provider/installer
   experimental limits. Inspect each exact probe/manifest scope before execution.
   Report all stages and failures truthfully. Resume other repositories only
   after independently reviewed, usable Elm activation and every required clean
   check pass; keep PR #354 unmerged until the existing final gates are met.

## Exclusions

No language/framework adapters, repository edits, model substitution, arbitrary
host execution, daemon clone, general deployment, unrelated refactor, constitutional
amendment, automatic scope approval or bypass of failed checks. Submodule/LFS
hydration and a Kubernetes probe adapter are outside this correction and must hold
explicitly when required.
