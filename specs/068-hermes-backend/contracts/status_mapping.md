# Contract: Hermes → Shared Status Mapping

**Feature**: 068-hermes-backend
**Source vocabulary**: `BackendStatus.state ∈ {working, done, error}` from
`agent/performer/src/performer/backends/base.py`

Coordinare MUST observe only the shared vocabulary (FR-009). The adapter
is the sole translation point.

---

## Translation table

| Observed condition | Resulting `BackendStatus.state` | `reason` field |
|---|---|---|
| `_proc` alive, no terminal trigger fired | `working` | — |
| `_proc` exited code 0, output JSON file exists and parses to non-empty document | `done` | — |
| `_proc` exited code 0, output JSON file missing OR empty OR not parseable | `error` | `malformed_output` |
| `_proc` exited non-zero | `error` | `subprocess_exit:<code>` |
| `_proc` killed via `stop()` (operator/coordinare request) | `error` | `stopped` |
| `AGENT_TIMEOUT` elapsed and coordinare invoked `stop()` on this adapter | `error` | `timeout` |
| Required env var missing at `start()` | `error` | `missing_env:<NAME>` |

## Invariants

- **No loops on bad output** (FR-010): malformed/empty output ALWAYS
  maps to a terminal `error`. The adapter MUST NOT retry, MUST NOT
  re-invoke `hermes chat` on its own.
- **No Hermes-internal leaks**: Strings like `"chat_paused"`,
  `"awaiting_clarify"`, or any other Hermes-internal lifecycle state
  MUST NOT appear in `state`. They MAY appear in `reason` only if
  prefixed with `hermes:` to make the translation gap visible.
- **Terminal is sticky**: Once `state` becomes `done` or `error`,
  subsequent `get_status()` calls return the same value until the
  adapter is discarded.
- **Cleanup parity**: Every transition into `done` or `error` MUST be
  accompanied by exactly one `_finalize()` invocation (see
  `backend_adapter.md`).
