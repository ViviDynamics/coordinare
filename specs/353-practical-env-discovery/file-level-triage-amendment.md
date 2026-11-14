# File-level triage: read the named files, not the areas around them

Issue #353; PR #354. Status: written proposal for maintainer review. Amends
[guided-sharding-amendment.md](guided-sharding-amendment.md).

## Why

Guided-v5 (head c031ed24) finished both triages cleanly in about 11 minutes with 31 calls, but kept 172 shards
for authoring. Triage marks whole directories `environment` (for example `control-plane/internal/config`, or an
entire service), and an area is excluded only when both sides say `unrelated`. Most of the repository therefore
stays in. At GLM's pace (about 12 shards per 5 minutes) that is about 70 minutes of authoring, beyond the
60-minute attempt cap. The earlier guided runs showed the same pattern at 105 shards.

The verdicts themselves are sensible. The problem is granularity: a directory verdict pulls in every file beneath
it, when the environment is usually defined by a few named files in that directory.

## Decisions (proposed)

### 1. `environment` names files; directories are `unrelated` or `unsure`

A triage verdict of `environment` must name a file path. A directory may be judged `unrelated` (everything
beneath it is skipped unless named elsewhere) or `unsure`. An `environment` verdict on a directory is a correctable
error asking for the specific files.

### 2. Authors read named files first

Authoring targets the files either triage named `environment`, plus the spine, grouped into shards by the existing
planner. `unsure` directories are not authored up front. Their files are listed to the shard authors and the
merge reviewer, who can read and cite them on demand, and they stay out of the exclusion record because they were
not judged unrelated. References are still followed, so any file named by an authored file is read.

### 3. Exclusions stay as approved

A path is recorded as excluded only when both sides cover it with `unrelated`. `unsure` areas are recorded
separately as `unread_unsure` (path, files), so reviewers see exactly what was neither read nor excluded and can
raise `unread_source` findings against it.

## Expected effect on Elm

Triage named roughly 150 to 200 environment items across both sides in guided-v5. As named files they pack into
far fewer shards. Most environment files (manifests, Makefiles, workflows, configs and docs) are small. This is an
estimate to be measured on the next run, probably 15 to 40 shards.

## Non-goals

No language or filename rules in the daemon; no change to exclusion independence, the ledger, or acceptance.

## Delivery

Maintainer approval, then native implementation with an independent review, preflight and CI, then pinned Elm.

## Review fixes (2026-10-21)

- The daemon loads every named file into its shard author's conversation, so no named file can be skipped and no
  model call is spent reading it.
- Inputs read through references, which no authored shard owns, get their own shard reviewers instead of all
  landing on the merge reviewer.
- If the unread records cannot fit even as top-level areas, the attempt holds with `scope_size_limit`; judgments
  are never relabelled or folded into an unnamed area. Group rationales are kept in full.

## Maintainer ruling: sharded attempt ceiling (2026-10-21)

File-level triage v1 named 1,698 files (22.6 MB; 927 files and 14.5 MB from one triage that fell back to naming its
whole listing) and authored 1 of 93 shards before it was stopped. The maintainer approved raising the attempt
ceiling for sharded discovery only to four hours (`MAX_SHARDED_ATTEMPT_SECONDS`). Non-sharded attempts keep
their one-hour maximum, and sharded configuration defaults to four hours unless set.
