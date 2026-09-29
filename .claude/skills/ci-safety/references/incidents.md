# Incidents behind the CI safety rules

Each entry: what happened, what it cost, which rule it produced. Dates are when the
rule was written.

## Rerunning a stale run cancelled live CI (2026-08-17)
A superseded run was re-run. It took the concurrency slot and cancelled the run for
the current head. Thirty-two checks read as failed at once. Cost: an afternoon reading
red that meant nothing. Rule: §1 and §2.

## Three retries reported, one happened (2026-08-17)
`gh run rerun` was issued three times against an in-progress run; it exits 0 and does
nothing in that state. The report said "retried three times". Rule: §4 and the
`verified-rerun` script.

## The flake that was a bug (2026-08-18)
A console end-to-end failure matched the flake list and was retried all night. It was
a real defect: clicks were dropped when a repaint detached their target. Rule: §3,
second occurrence is never a flake.

## Retrying against a stale base (2026-08-18)
Two PRs re-failed the same lane for hours. `main` already had the fix. Rule: §5.

## Watcher exited "unknown" (2026-08-18)
A poll loop hit a transient API error and exited declaring unknown. The PR sat
unattended. Rule: §6.

## Nine hours waiting for a tidier moment (2026-08-18)
A green PR waited on a parent whose only red was an unrelated flake. Rule: merge-pr's
"merge on the first genuine green".
