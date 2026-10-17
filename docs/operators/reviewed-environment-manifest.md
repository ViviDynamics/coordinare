# Reviewed environment manifests

Environment readiness requires an independently accepted, evidence-backed manifest,
passing required checks in a clean consumer context, and valid frozen cache
provenance. Bootstrap prose, an installer-written script, an old success flag or an
agent's approval cannot waive execution evidence.

## Configure the discovery and review model

This implementation uses the existing conducting brain's `openai_api` JSON-chat
seam for discovery and coverage review. Configure a model that supports JSON-object
chat responses and supply its credentials to the daemon. There is no separate
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
  # base_url: "https://your-compatible-endpoint/v1"

env_cache:
  manifest_max_reads: 256
  manifest_max_file_bytes: 262144
  manifest_max_source_bytes: 4194304
  manifest_max_inventory_entries: 100000
  manifest_phase_timeout_seconds: 120
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
reads arbitrary paths and records exact content SHA256 values. Its output includes:

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
The daemon validates source identity, manifest digest, input hashes and actual
reviewer reads. Only acceptance of the exact candidate permits installation.
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
source text and 120 seconds. The complete inventory defaults to a 100,000-entry cap,
controlled by `manifest_max_inventory_entries`;
serialized inventory also has a source-byte admission bound. Manifest and assessment
each have a 256 KiB serialization cap. Reads, model turns and replies are bounded;
exhaustion is an explicit hold, never truncated acceptance. The YAML above names
the operator-configurable inspection limits. Separately, the accepted manifest's
source inputs must fit the fixed 256 KiB aggregate clean-verifier transfer budget,
measured as UTF-8 JSON including paths and escaping, before base64 encoding. Discovery
holds before accepting a contract that exceeds this shared Docker/Kubernetes bound;
increasing inspection limits does not increase it. Review may read additional context
within the inspection limits without adding it to the verifier transfer. Docker
materializes source through verifier stdin; Kubernetes splits source, verification
script and provenance configuration into 64 KiB environment chunks and reconstructs
them inside the trusted Python process. This keeps each argument and environment
value below Linux's 128 KiB per-string limit without raising aggregate payload caps. Increase
only the relevant inspection bound after checking the held reason and repository size.

A changed source head invalidates discovery, including newly added files and nested
projects whose old inputs were untouched. An unchanged source identity does not
invoke discovery/review again when accepted readiness is still applicable. Content
changes in the installed cache invalidate provenance independently of source
changes. Existing outer bootstrap attempt/deadline limits and workflow repair
budgets continue to apply. Individual commands default to a 180-second timeout.

## Upgrade and recover

Clean verification requires an immutable `image@sha256:<64 lowercase hex digits>`
reference on both Docker and Kubernetes. Mutable tags hold readiness. Docker uses
the selected bootstrap performer's configured image, so pin that actual image,
including any endpoint override. The clean Docker verifier runs with `--network
none`; installation may have separate network access, but verification cannot rely
on it.

For Kubernetes, set `kubernetes_verifier_image` to an immutable reference (or pin
its `performer_image` fallback). When a symphony configures environment caching,
every endpoint with a consumer role must use that exact image reference; a
different consumer image is rejected during configuration loading. An endpoint
whose only role is `env_bootstrap` may use a different image because the clean
verifier runs separately after bootstrap. Configure and validate a NetworkPolicy that
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
