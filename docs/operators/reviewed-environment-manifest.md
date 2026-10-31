# Reviewed environment manifests

Environment readiness requires an independently accepted, evidence-backed manifest,
passing required checks in a clean consumer context, and valid frozen cache
provenance. Bootstrap prose, an installer-written script, an old success flag or an
agent's approval cannot waive execution evidence.

## Configure the discovery and review model

This implementation uses the existing conducting brain's `openai_api` JSON-chat
seam for discovery and coverage review. Configure a model supporting strict JSON Schema
responses (the default), or explicitly select native function calls with
`conducting.structured_transport: tool_call`, and supply its credentials to the daemon.
Function mode requires exactly one advertised call per response; local manifest and
coverage validation remain mandatory. For endpoints with broken constrained decoding,
`structured_transport: json_object` requests canonical JSON with a JSON-object syntax guard, without the phase-schema grammar or
native tools. The same local validation and bounded repairs apply; malformed output
cannot establish readiness. Unsupported transports hold without automatic fallback. There is no separate
reviewer model-role configuration: the reviewer receives a fresh conversation using
the same configured chat callable.

For example, merge the following into a deployment's existing config, replacing
the model placeholder with the model served by that endpoint:

```yaml
conducting:
  backend: openai_api
  model: "<your JSON-capable model>"
  api_key_env: OPENAI_API_KEY
  max_tokens: 32768
  structured_transport: json_schema  # tool_call or json_object are explicit compatibility choices
  # base_url: "https://your-compatible-endpoint/v1"

env_cache:
  manifest_max_reads: 256
  manifest_max_file_bytes: 262144
  manifest_max_source_bytes: 4194304
  manifest_max_inventory_entries: 100000
  manifest_phase_timeout_seconds: 120
  manifest_attempt_timeout_seconds: 600
  manifest_max_model_calls: 128
  manifest_max_total_reads: 512
  manifest_max_schema_repairs: 2
  manifest_max_coverage_revisions: 2
  manifest_max_scope_revisions: 8
  manifest_max_execution_repairs: 2
  manifest_max_review_context_bytes: 1048576
```

Set the named API-key variable in the daemon environment, using the deployment's
normal secret mechanism. For a checkout, create `.env` from `.env.example` and use
`make run`, which loads it. Do not put literal credentials in manifests or checked-in
configuration. `api_key_env` and `base_url` also support an operator-configured
OpenAI-compatible endpoint; this does not choose a project's toolchain.

Run `python -m coordinare config validate` with the intended environment loaded
before restarting. Configuration validation does not prove model reachability or
JSON support. Conducting backends other than the available `openai_api` JSON-chat
seam, missing JSON-chat configuration or credentials, malformed replies and timed-out
requests hold environment discovery. They do not enable a legacy parser fallback.

## Understand the contract

The daemon inventories the repository at an immutable source commit. Discovery
reads arbitrary paths and records exact content SHA256 values. The agent proposes
only requirements, commands, activation, services and an optional empty-environment
justification. The daemon constructs provenance fields from its actual read ledger,
pinned source, inventory and runtime capabilities; model-supplied provenance fields
are rejected. The unchanged full persisted contract includes:

- Requirements with repository source citations and required executable checks.
- Explicit installation commands, arguments, working directories, environment
  references, prerequisites, scope and timeouts.
- Declarative activation paths and environment updates rooted at `{cache}`,
  `{repo}` or `{scratch}`.
- Explicit service initialization, start, health and stop commands, with writable
  runtime state in scratch outside the read-only installed cache.

Core execution does not branch on language, framework, library, dependency filename
or runtime-manager layout. Existing service declarations are additional evidence
that must become reviewable generic lifecycle actions; automatic service-kind
recipes cannot satisfy this gate. Unsupported platform or privilege requirements
hold readiness. A project needing no extra dependencies still needs a documented
justification and a required baseline usage check.

Review starts without the discovery transcript. The reviewer independently reads
all manifest inputs and can read additional files from the same inventory. It
returns `accept`, `revise` or `indeterminate`, with a rationale and citations.
The reviewer proposes only its verdict, rationale and personally inspected source
paths. The daemon binds that assessment to the exact displayed manifest and checks
actual independent reads and hashes. Only acceptance of the exact candidate permits installation.
Any edit to commands, requirements, requiredness, activation, services or source
identity changes the digest and requires new acceptance.

Daemon metadata carries the accepted manifest, assessment and digest to the
performer. Installation, activation and clean checks use that accepted contract.
Trusted verification records each check's actual exit status and outcome; output
from a child process is data and cannot forge a passing trusted result. Required
checks must all pass for the applicable consumer scope. Failed prerequisites,
missing commands/results, timeouts and indeterminate outcomes hold readiness.
Optional results remain visible and cannot downgrade required requirements.

A foreground service remains supervised through health checks and required checks.
Its start result is finalized after teardown using the supervisor’s completion
evidence. Requested termination during the declared stop phase is distinct from
an earlier service failure; missing or failed cleanup evidence holds readiness.

## Limits and cache invalidation

Each discovery and review phase defaults to 256 reads, 256 KiB per file, 4 MiB of
source text and 120 seconds. One attempt, including runtime identity resolution,
inventory fetching, installation, clean verification and executable repairs, defaults to 600 seconds, 128 model calls, 512 total reads and at
most two repair rounds. The configured outer `bootstrap_max_seconds` also applies;
zero there never removes discovery's own bound. `manifest_attempt_timeout_seconds`
can be raised to at most 3600 seconds, or to at most 14400 seconds (four hours) when
`manifest_shard_context_tokens` enables sharded discovery. Sharded discovery defaults to
14400 seconds, 1,024 model calls and 8,192 total reads unless those are set explicitly;
raise `bootstrap_max_seconds` to match, or it caps the attempt. Phase changes and fresh
reviews cannot reset the aggregate counters.

The pinned inventory carries each path's size and mode, plus a symbolic link's
recorded target, from the verified execution-source bundle. Files larger than the
per-file read limit and symbolic links are listed but never read or followed:
asking for one returns `too_large` (with its size) or `link` (with its recorded
target) instead of holding the attempt, and a source gap cannot rest on such a file
alone. Files already read earlier in the same attempt remain readable evidence.
A source scope holds at most 512 criteria and a finding ledger at most 128 findings;
a repository needing more holds with `scope_size_limit` rather than dropping
obligations.

The complete inventory defaults to 100,000 paths and remains daemon-side. Agents
navigate sorted directory pages of at most 200 entries / 32 KiB and request batches
of at most eight text files. All inspected discovery inputs retain their exact
hashes; the inventory digest also binds unread path additions/deletions. The initial
prompt contains a bounded root listing, not the full inventory.

Malformed proposals receive trusted validation codes and field locations, with no
candidate values in diagnostic events. A coverage `revise` verdict can request a
repair within the same two-round budget. Every changed manifest receives a new
independent review conversation. Indeterminate review, source/identity drift,
unavailable evidence, exhausted budgets and dropped required obligations hold.
Repair never waives execution or invents source evidence.

A complete rejected execution report with an actual failed required action can
request a repair only after the installer and verifier are confirmed stopped.
Missing, wholly unrun, malformed or indeterminate evidence, and uncertain cleanup,
hold the attempt. A failed prerequisite may leave dependent checks unrun in the
rejected report; all required checks must run successfully in the repaired attempt.
Required requirement, check, service and lifecycle IDs, scopes and requiredness
cannot be removed or narrowed.

Executable repair shares the same two-repair, call, read and deadline budgets as
schema and coverage repair. It creates a new empty cache generation, starts fresh
discovery with bounded redacted failure feedback, obtains another independent
review and reruns installation and every required check. Repository and scratch
roots are disposable and may have different absolute paths in each execution;
only cache artifacts persist. Installer-only source references must not survive.

The optional persisted attempt journal retains original UTC expiry, consumed
reservations, generation and rejected evidence. Reservations are saved before
model calls and source reads; inability to save holds external work. Restart cannot
renew the deadline or reuse rejected contents. Interrupted execution holds until
cleanup is established. Feedback is bounded at 16 KiB and 512 characters per check;
sanitized rejected reports retain a 256 KiB bound, with at most three rejected
entries and a 16 MiB journal. Raw command output is never a readiness authority.

Manifest and assessment each retain a 4 MiB serialization cap; a scope definition is
bounded at 1 MiB, its contract at 2 MiB and a finding ledger at 512 KiB. Model review
inputs remain bounded text evidence; executable source travels separately as the
complete pinned binary repository bundle, bounded at 128 MiB compressed transport
and 512 MiB expanded contents including metadata. The bundle preserves file modes,
links and binary files. Docker transports its framed header and archive through
trusted stdin, outside environment variables and command arguments.

Kubernetes creates immutable, Pod-owned Secret payloads outside Pod environment
and command text. A separate metadata Secret plus up to 256 data Secrets carries
at most 257 Secrets in total; each data Secret holds up to eight 64 KiB decoded
chunks. Each API object remains below 900 KiB. Trusted init code verifies framing,
archive/source identity, exact paths and reviewed file hashes before restoring
private scratch source. The verification container does not mount source Secrets.
Failed creation, cancellation and uncertain cleanup hold verification. Restart
reconciliation checks ownership and removes interrupted verifier Pods and their
source payloads; Pod ownership also enables Kubernetes garbage collection.
Verification/provenance scripts still use bounded environment chunks, separate
from complete execution source payloads.

Acceptance binds the immutable image, platform, cache mount path and installation/
verification network policies through `capabilities_digest`. These actual execution
constraints are supplied to both agents. Old contracts lacking capability or inventory
binding require rediscovery. Source commit, inventory, capabilities, policy and
rendered activation changes invalidate the corresponding accepted/frozen evidence.
Diagnostics record stage, elapsed time, calls, reads, context bytes, inspected bytes,
repairs and trusted held codes. Provider usage and finish reasons are recorded when
available; absent metadata is explicitly marked unavailable. A timeout alone is not
proof of token exhaustion. Existing outer retry/repair budgets still apply.

## Upgrade and recover

Clean verification requires an immutable `image@sha256:<64 lowercase hex digits>`
reference on both Docker and Kubernetes. Docker resolves the configured consumer
image to a locally inspected immutable reference; pin consumer
endpoint overrides consistently. Multiple distinct consumer images require separate
environment targets and hold this shared cache. The clean Docker verifier runs
with `--network none`; installation may have separate network access, but verification cannot rely
on it.

For Kubernetes, set `kubernetes_verifier_image` to an immutable reference (or pin
its `performer_image` fallback), and set `kubernetes_verifier_platform` explicitly
to `linux/x86_64` or `linux/aarch64`. Verifier node selectors enforce that target;
the daemon host architecture is never used to infer it. When a symphony configures
environment caching,
every endpoint with a consumer role must use that exact image reference; a
different consumer image is rejected during configuration loading. The bootstrap endpoint must also use that target image/platform. Earlier
configurations with a distinct bootstrap-only image remain parseable, but capability
resolution holds until installation and clean verification use the same target.
The installer runtime status is checked before project execution; missing or
different image/platform metadata refuses dispatch. Configure and validate a NetworkPolicy that
isolates verifier pods labeled `coordinare.role: env-verifier`, using a CNI that
actually enforces the policy. Only after validating deployment enforcement, add:

```yaml
env_cache:
  kubernetes_verifier_network_isolated: true
```

This value defaults to `false` and is an operator attestation of externally
enforced isolation. It does not install a policy or make a non-enforcing CNI
secure. Kubernetes clean verification holds when the attestation is absent.
These execution-policy gates do not waive independent review or required checks.
The consumer must use the exact verified image and runtime platform; an OCI index
digest alone cannot authorize a different architecture. Reviewed cache jobs require
ephemeral per-job containers. Persistent performers hold because cache mounts and
whole-job cleanup cannot be established for an existing container.

Linux commands run under a dedicated subreaper that kills and reaps detached
descendants before reporting completion. Other operating systems retain a process
group adapter for development; production container verification uses Linux.
Curated test credentials use transient Docker verifier stdin or a Kubernetes
Secret reference. The Kubernetes Pod is created first, then its API-issued UID owns
the short-lived Secret; Pod garbage collection covers daemon crashes. Removal must
be confirmed before readiness. Update the shipped namespace Role to grant Secret
create/get/delete (no list, update, patch or cluster grant); use a dedicated namespace
for this permission boundary. Encoded credential envelopes retain a 384 KiB aggregate
cap and use 64 KiB fragments in both transports. Kubernetes Pod specs contain only
Secret names, keys and fragment counts. Values are restored and transport fragments
removed after the trusted interpreter starts. Manifests use named
references; literal credential URLs are refused.

Upgrade daemon, performer and trusted verifier images together; they must share
the environment contract capability. State schema 40 adds the accepted manifest
and assessment. Restored older caches have no accepted pair and hold consumers
until reviewed reconstruction and clean verification complete. Existing bytes are
not evidence of acceptance and do not need to be deleted before replacement.

When an environment is held:

1. Read the symphony's bootstrap status and held reason. Identify whether the
   problem is configuration, discovery evidence, review, command execution,
   provenance, transport infrastructure or an exhausted budget.
2. For missing model configuration, fix the conducting JSON-chat configuration and
   daemon credentials. For omissions or conflicting instructions, fix repository
   evidence or the declared requirement; discovery must reassess the new snapshot.
3. For required check failures, repair the installed environment or the documented
   requirement. A failed check cannot be disabled through a review verdict.
4. For image/capability mismatches, roll matching daemon, performer and verifier
   images with immutable SHA256 references. For Kubernetes isolation refusal,
   configure and validate the enforcing CNI/NetworkPolicy before setting the
   attestation. For provenance refusal, stop competing writers and rebuild through
   the normal bootstrap path.
5. After correcting the cause, use **Force bootstrap now** on the dashboard or
   `POST /api/symphonies/{name}/env-bootstrap`. This resets the existing bootstrap
   budget; the 15-minute manual-reset cooldown still applies. Paused symphonies
   must resume first. A retry must obtain matching acceptance and verification.

Docker and Kubernetes retain the existing safe writes, writer-quiescence,
read-only consumer mounts, frozen provenance and trusted result-collection rules.
See [environment-cache onboarding](../onboarding/06-env-cache.md) for the verifier
identity, Kubernetes image settings and pod bounds. Validate deployment-specific
permissions, registry access and network policy operationally. This guide does
not report live deployment validation.


### Opt-in repository validation

Run `python -m scripts.validate_environment_repositories --help` from a development
checkout. Supply existing clean, pinned snapshots (`<repo>/source` and an
`inventory.json` containing `commit`), an unused private output directory, model
configuration and either an immutable candidate `--image` or immutable
`--base-image`. The latter overlays the checkout's shared runner into a temporary
image. Both paths verify the image's actual Python file hashes before validation.
For example:

```sh
.venv/bin/python -m scripts.validate_environment_repositories sample elm \
  --snapshots /private/snapshots --output /private/validation/new-profile \
  --base-image registry.example/performer@sha256:<digest> \
  --model <model-id> --base-url <provider-url> --api-key-env <key-env-name> \
  --execute
```

Discovery uses the production service, including fresh independent coverage review.
With `--execute`, an accepted proposal waits for command-scope inspection. Inspect
`attempt-000/accepted-manifest.json` and its pinned source scripts, then write
`execution-approved.json` into that attempt's private directory. Each repaired
candidate uses a separate `attempt-NNN` directory and requires its own exact-digest
scope marker. Attempts and rejected evidence remain separately archived:

```json
{"scope":"environment_validation","approved":true,"manifest_digest":"<digest>","source_commit":"<commit>","image_ref":"<immutable-image>"}
```

This local marker authorizes isolated validation commands only. It cannot waive
missing or failed execution evidence. Deployment, release, hardware operations and
ambiguous destructive commands require a separate scope decision. Model credentials
are kept outside project containers. Private installer checkouts disable host Git
configuration and hooks, including host filter drivers. Private transcripts are optional and must not
be published. Reports distinguish discovery, review, installer, clean consumer and
frozen provenance; discovery alone exits unsuccessfully. These are direct service
entrypoint tests, not a live daemon or outer retry-loop test. Runtime cache contents
and temporary images are removed by default; readiness in a report is historical
for that validation attempt, not an installed deployment.


## Complete reviewer evidence and correction limits

Before each fresh coverage judgment, Coordinare independently fetches every manifest
input at the pinned commit, verifies its hash and delivers its contents to the reviewer.
The reviewer can inspect more files and must still judge executable coverage. Delivery
establishes the supplied bytes; it does not establish that the model understood them.
Source drift and missing evidence hold before judgment. The full serialized review
conversation, including transport instructions, embedded schemas and additional tool results, is limited to 1 MiB by default
(`manifest_max_review_context_bytes`, maximum 16 MiB). Oversized evidence holds without
truncation or automatic partitioning. The JSON-chat backend currently exposes no input
token capacity; the byte cap does not guarantee provider context capacity. Provider
request failures hold and do not consume schema corrections. Output limits remain set
on the model backend.

Scope revisions default to eight. Schema corrections, executable revisions and executable repairs default to two each
(sharded discovery defaults to eight executable revisions). Their independent configurable ranges
are zero to eight, zero to eight, zero to eight and zero to two respectively (`manifest_max_schema_repairs`,
`manifest_max_scope_revisions`, `manifest_max_coverage_revisions`, `manifest_max_execution_repairs`).
`manifest_max_coverage_revisions` counts executable review revisions. A scope revision that is refused holds with
`attempt_scope_revision_limit`; an executable revision that is refused holds with `attempt_coverage_limit`. An execution repair defaults to a delta: only the records the failed
execution implicates are re-authored, as changed and added records, and an unchanged record keeps its standing. If the
delta cannot be admitted or is not accepted by review, the full repair runs while budget remains. Set
`manifest_delta_repair: false` to use the full repair for every execution repair. All categories share the original
aggregate deadline, model-call and source-read limits. A new candidate, new reviewer,
cache generation or daemon restart never renews them. The old `manifest_max_repairs`
configuration is a deprecated alias supplying all three limits; combining it with any
new category setting is rejected. A valid coverage revision can add obligations but
cannot remove, make optional or narrow existing required checks or lifecycle scopes.

Source review can delegate up to eight exact file-read or paginated directory-list
requests per finding. The daemon binds each request to its finding, originating
review, scope, source commit and inventory. Investigation must finish before an
author revision, and responses must cite requested file contents. A listing proves
only its exact page; unavailable content is not pinned absence. Inspection never
resolves a finding or grants readiness. The author can reuse verified same-attempt
bytes within the existing byte/context limits. Independent reviewers fetch every
required input afresh; the original read and repair budgets remain in force.

Current attempt journal version eight/source protocol three stores the bounded
inspection queue and results, category consumption and source-scope checkpoints.
Versions one through five require an explicit clean rebuild after confirming no
executor is running. Versions six and seven can migrate only when they already
contain an accepted source-scope contract and no pending investigation; otherwise
they require an explicit restart. Missing in-memory author content requires charged
reinspection or a hold, never fabricated completion. Migration never grants fresh allowances. Frozen contracts remain
subject to the current identity, coverage and execution acceptance checks. The existing authenticated environment-bootstrap action now archives the old
journal and starts in a new empty cache sibling. Discovery-only holds need no executor
cleanup. After execution, the action must confirm that its owned session stopped;
a missing owner or failed confirmation returns a hold without clearing the journal or
consuming the reset cooldown. At most three old journals and 16 MiB of archive evidence
are retained; exceeding that bound requires explicit archival before another reset.
Interrupted execution remains held across source changes and restarts.
Current diagnostics record the actual stage, category consumed/max counts, mandatory
inputs delivered/required, context bytes and remaining aggregate time. Raw source and
model rationale are excluded from these summaries.


The opt-in repository validation CLI accepts `--max-total-reads` using the existing
`manifest_max_total_reads` product bounds (1–8,192; default 512). Its profile records
the selected allowance, and every actual fetch still spends that attempt's finite
read budget. A larger allowance requires a fresh labelled profile; it does not
resume a held attempt, reset repair counters, replace independent evidence fetches,
or waive any required execution check. Compare results with the changed allowance
identified explicitly.
