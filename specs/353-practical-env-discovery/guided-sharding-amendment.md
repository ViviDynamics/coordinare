# Guided sharding: read what the environment depends on, not everything

Issue #353; PR #354. Status: written proposal for maintainer review. Amends
[sharded-discovery-amendment.md](sharded-discovery-amendment.md).

## Why

The first sharded attempt (`source-evidence-cov8-internal-glm53flash-elm60m-sharded-v1`, head 5da8e08f) showed
that exhaustive sharding does not fit a real repository. Pinned Elm has 6,592 readable files (66 MB): control-plane
18 MB, docs 9 MB, finance-platform 8.5 MB, console-e2e 7 MB, and more. Splitting by size gives 276 shards.

- Each shard author used about 8 GLM calls and several minutes. Authoring alone would need about 2,200 calls and
  about two hours at 12 concurrent agents, over both the ruled 1,024 calls and the 3,600-second product maximum.
- The attempt actually held after 1,957 s and 125 calls, with 3 of 276 shards finished. One agent spent its two
  corrections adding `"cursor": 0` to `read_files` (`unsupported_envelope`).

Most of those bytes are application source, fixtures and prose that do not change what the development and test
environment needs. A person bootstrapping Elm reads the root guide, the Makefile, the dependency declarations,
CI workflows and the setup scripts those point to. They do not read every handler. Sharding should divide that
reading, not the whole repository.

## Goals

- Every source-defined environment obligation is still found, cited and independently reviewed.
- Calls scale with the parts of the repository that define the environment, not with total bytes.
- Elm and Dettmore fit the ruled budget: 1,024 calls, 8,192 reads, 3,600 s.

## Non-goals and anti-goals

- No daemon-coded filename, language or framework rules. Which paths matter is a model judgment from listings
  and references, made by independent agents and reviewable like any other judgment.
- Unread areas are never silently treated as irrelevant. An exclusion is a cited judgment that an independent
  reviewer can contest.
- No change to the finding ledger, decision protocol, journal invariants, executable stages or readiness gates.

## Decisions (proposed)

### 1. Listing triage, from paths and sizes only

The daemon splits the pinned inventory listing (paths, sizes, modes; no contents) into listing shards that fit an
agent's share. One triage agent per listing shard returns, for each directory or file it was given, one of:

- `environment`: likely to define or exercise the development/test environment, so it must be read.
- `unrelated`: an area the agent judges not to define environment obligations, with a short rationale.
- `unsure`.

Triage costs about one call per listing shard (Elm's listing is about 400 KB, so a few agents).

### 2. Guided authoring from the spine outward

Authoring starts from the spine and every `environment` or `unsure` area. Shard authors are created only for those
areas, still bounded by size and read allowance, and they follow references and dependency declarations outward
through the existing cross-reference routing. A referenced path is authored even if triage marked its area
`unrelated`.

### 3. Exclusions are reviewable

The scope records the triage judgment for every area that was not authored: area, rationale, and triage agent.
Reviewers receive those exclusions alongside the criteria. An independent triage (a second listing-only pass by
reviewer agents, with no author material) compares its own judgment. Any area the reviewer marks `environment` or
`unsure` that was excluded becomes a blocking finding with an `unread_source` request, handled by the existing
investigation and revision path. Acceptance therefore still requires an independent reviewer to agree that every
unread area is outside the environment.

### 4. Review in proportion

Shard reviewers run only for authored shards. The merge reviewer also judges the exclusion list. Unchanged,
accepted shards carry forward as they do today.

### 5. Small protocol tolerance

`read_files` and `read_file` ignore a stray `cursor` key instead of rejecting the reply. That key only belongs to
`list_files`, so ignoring it changes no meaning. Every other unknown key is still rejected.

## Expected cost on Elm (estimate)

Triage: about 4 calls. Spine plus `environment` and `unsure` areas: Makefile, scripts, CI workflows, each service's
dependency declarations and test entry points, perhaps 20 to 40 shards at about 8 calls each, so 160 to 320 calls.
Review: a similar count plus the independent triage. That totals about 400 to 700 calls, inside 1,024. The 3,600 s
deadline is the tighter constraint and has to be measured on the next run.

## Left to the builder

- Listing shard format and size accounting.
- How triage verdicts map onto planned shards (a directory verdict covers its descendants unless a child has its own verdict).
- Exclusion record format inside the scope contract, within its existing byte bound.

## Delivery

Maintainer approval, a short plan, native implementation, independent review, preflight and CI. Then pinned Elm,
then Dettmore, on GLM with the ruled budget.
