# Data Model: Implementer-Side Rebase

## ImplementerRebaseConfig (new, `config.py`)

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | When true, the daemon classifies in-flight PR branches and routes diverged ones to the implementer. When false, no classification runs and dispatch is independent of mergeability. |

`extra="forbid"`. Exposed as `ProjectConfiguration.implementer_rebase`.

## RebaseOutcome (changed, `models/rebase.py`)

Adds `DELEGATED = "delegated"`: divergence detected, handed to the implementer. `CLEAN`, `PERFORMER_RESOLVED`, `BLOCKED` and `FAILED` are no longer produced by the daemon; they stay in the enum so persisted markers and old dashboard payloads still parse.

## last_rebase_attempt (unchanged shape, persisted per card)

`{main_sha: str, head_sha: str, outcome: str}`. Written when a request is issued, with `outcome="delegated"` and `head_sha` the branch head the platform reported. Non-progressing outcomes for `should_attempt_rebase`: `blocked`, `failed`, `delegated`.

## Decision values (`classify_pre_dispatch`)

`proceed` | `unknown` (was `defer`) | `request` (was `rebase`) | `blocked_thrash`.

## Divergence feedback entry (relay_feedback item)

| Key | Meaning |
| --- | --- |
| `kind` | `"branch_divergence"`; the performer's trigger |
| `source` | `"coordinare"` |
| `base_branch` | branch to rebase onto |
| `main_sha` | base revision the daemon saw; must be contained in the rebased history |
| `expected_head_sha` | branch revision the daemon classified; the lease |
| `body` | the agent-facing instruction |

## State transition on a request

`relay_feedback` gains the divergence entry at the front (existing entries kept), `performer_stage = "implementing"`, `phase = "dispatching"`, `agent_dispatch = {}`, `agent_dispatch_at = None`. Identical to what `prepare_conflict_resolution` set, except feedback is merged rather than replaced.

No `state_store.py` change. `CURRENT_SCHEMA_VERSION` is unchanged.
