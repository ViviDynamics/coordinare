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

## Limits and cache invalidation

Each discovery and review phase defaults to 256 reads, 256 KiB per file, 4 MiB of
source text and 120 seconds. The complete inventory is capped at 100,000 entries;
serialized inventory also has a source-byte admission bound. Manifest and assessment
each have a 256 KiB serialization cap. Reads, model turns and replies are bounded;
exhaustion is an explicit hold, never truncated acceptance. The YAML above names
the operator-configurable inspection limits. Increase only the relevant bound after
checking the held reason and repository size.

A changed source head invalidates discovery, including newly added files and nested
projects whose old inputs were untouched. An unchanged source identity does not
invoke discovery/review again when accepted readiness is still applicable. Content
changes in the installed cache invalidate provenance independently of source
changes. Existing outer bootstrap attempt/deadline limits and workflow repair
budgets continue to apply. Individual commands default to a 180-second timeout.

## Upgrade and recover

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
   images. For provenance refusal, stop competing writers and rebuild through the
   normal bootstrap path.
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
