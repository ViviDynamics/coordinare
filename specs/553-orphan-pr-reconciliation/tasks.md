# Tasks: Orphaned PR reconciliation

- [ ] 1. Config: `OrphanPrConfig` (enabled / completion_reconciliation / sweep, all default false) on symphony-level config, with validation and tests proving default-off yields no behavior and no reconciliation GitHub reads.
- [ ] 2. GitHub surface: `list_open_pull_requests()` + `close_pull_request_with_comment()` on the protocol, real service, and fake; tests assert the fake round-trips fixtures.
- [ ] 3. Attribution: `resolve_pr_to_card` resolution chain (session branch → issue linkage → canonical/legacy prefixes), returning card + basis, `None` when unresolvable; tests cover legacy-prefix PRs and unresolvable skips.
- [ ] 4. Comment assembly: attribution marker, card reference, terminal state, superseding PR link, reason; test pins the exact required fields.
- [ ] 5. Completion-time reconciliation: closing every other open PR of a completed card; tests for the US-1 acceptance scenarios (two-PR card, single-PR card, merge-not-complete no-op).
- [ ] 6. Janitor sweep: per-cycle close of terminal-card PRs; tests for US-2 scenarios (Done-card close, active-card untouched, unresolvable skip, multi-PR same-card, idempotent re-close).
- [ ] 7. Wire-in: merge path calls completion reconciliation when enabled; check_board end-of-cycle calls the sweep when enabled; disabled path exits before any reconciliation read (byte-identical).
- [ ] 8. Decision logs: one `orphan_pr.decision` line per evaluated PR with the FR-005 vocabulary; test asserts the exact outcomes and fields.
- [ ] 9. House sweep: ruff + mypy clean; full unit suite green; preflight before push.
