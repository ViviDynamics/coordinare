# Data model

The accepted EnvironmentManifest describes requirements, commands, activation and service
lifecycle. CoverageAssessment binds `manifest_digest`, `source_commit`,
`verdict: accept|revise|indeterminate`, `rationale` and cited paths (`sources`).
The daemon stores these outside performer-writable mounts in its existing snapshot.

Execution outcomes bind check ID, manifest digest, context, exit status, duration and
bounded redacted output. Readiness needs accepted coverage and all required checks passed
plus existing frozen cache provenance. No output, skipped execution or unknown state is
success. Post-install cache hashes and results reference the contract digest; they are
not part of that digest.

Source inventory is fetched at an immutable commit. New head invalidates discovery;
unchanged head reuses acceptance. Contract changes require review. Old snapshots load
with absent contract defaults and trigger bounded reconstruction rather than readiness.
