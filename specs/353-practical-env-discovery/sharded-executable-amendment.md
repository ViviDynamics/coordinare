# Sharded executable construction and review

Issue #353; PR #354. Status: approved by the maintainer on 2026-10-22 (full amendment). Companion to
[sharded-discovery-amendment.md](sharded-discovery-amendment.md).

## Intent

Sharded source discovery lets a large repository reach an accepted source scope,
but executable manifest construction and executable coverage review still load
every scope input into one conversation (`_discover_attempt` preloads
`scope.all_inputs`). On a large repository that stage would hit the same context
limit. This amendment applies the same divide-and-conquer approach to it, without
changing what readiness means.

## Goals

- No executable author or reviewer conversation needs more than its own share of
  the scope and source.
- The final manifest is still one immutable document with one digest carried
  through installation and the offline clean consumer.
- Every required criterion still maps to required checks; every required check and
  service lifecycle step must still run and pass.

## Non-goals and anti-goals

- No change to the manifest schema's meaning, the probe protocol, the installer,
  the clean consumer, readiness, or the decision protocol.
- No language or framework rules; grouping follows the source shards.
- A merged manifest must never silently drop a check, a criterion mapping, or a
  service lifecycle step, and conflicting install steps must never be resolved by
  picking one arbitrarily.

## Decisions (proposed)

### Evidence on demand, not preloaded

Executable agents receive the scope contract's criteria and their citation
metadata, not file contents. They read the specific files they need through the
existing source tools, charged to the attempt budget, so a conversation holds only
what its work requires.

### Two kinds of executable work

- **Environment author (one, global):** proposes requirements, installation,
  activation, runtime endpoints and services, working from the shared spine,
  dependency declaration files it chooses to read, the probe observations, and
  a bounded digest of every shard's criteria (identifiers, descriptions and
  usability text, no file contents).
- **Check authors (per source shard, parallel):** propose required checks and the
  criterion-to-check mapping for that shard's criteria, given the global
  environment proposal's identifiers and activation, reading only files they need.

Probes stay one global, cited plan, since they observe the shared execution image.

### Deterministic merge

The daemon assembles one manifest: the global environment part plus the union of
shard checks and criterion mappings. Identifier collisions, a check referencing an
unknown requirement or service, or two shards defining the same identifier
differently are rejected back to the responsible authors as repairable errors,
never resolved by choosing one. The merged manifest passes the existing admission
validation unchanged, including complete required-criterion mapping.

### Independent executable review, divided the same way

Fresh reviewers review the global environment part and each shard's checks
against the scope contract, reading files themselves with no access to author
conversations. A merge-level reviewer checks cross-cutting coverage (every required
criterion mapped, services and activation consistent). Findings and decisions use
the existing executable review protocol and one finding ledger, one review
sequence per round.

### Budget and repairs

Same single attempt budget as sharded source discovery. Execution repairs remain 2
and apply to the whole manifest; a failed check routes repair work to the author
that owns it (global or shard), with the original failure evidence.

## Left to the builder

- The criteria digest format and its byte bound.
- How identifiers are namespaced or validated across shards.
- Persistence of executable shard state within the enlarged journal bound.

## Delivery

Maintainer approval, then a plan extending the sharded discovery plan with tasks
for evidence-on-demand, the global environment author, shard check authors, the
merge, divided executable review, repair routing and recovery, then implementation,
independent review, preflight, CI and the pinned Elm and Dettmore runs.
