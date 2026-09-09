# Plan and analysis

- Add a bounded documentation-input carrier; wire report collection, per-role reset,
  session fanout and snapshot persistence, Score/dispatch contract and prompts.
- Repair side-run dispatch configuration, identity and mode; hash the combined
  inputs, resume polls, resolve completion against canonical state, and serialize
  early/final documentation writers.
- Feed inputs into the existing documenter planning/writing steps, retain inventory
  de-duplication, separate early docs-only writes from final root-pointer refresh,
  and expose completed docs to implementation.
- Verify real Score validation, payload routing, per-role records and hashes,
  restart/fanout replacement, updated inputs, writer exclusion and existing real-Git
  push union tests. Run full suites/lint and independent/Copilot review, then CI/merge.

Analysis before implementation: no conflict with 165's per-reader briefs: findings
use a separate documenting-only carrier, so implementer/QA do not receive each
other's blueprint slices. A documenter's completed output is advisory to the
implementer; the blueprint remains authoritative. No analysis role gains write
capabilities. Snapshot additions are optional and old records have safe defaults.
No separate follow-up issue is needed to close the original integration gaps.
