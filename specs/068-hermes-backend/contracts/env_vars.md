# Contract: Hermes Environment Variables

**Feature**: 068-hermes-backend

Defines the env-var surface that the operator MUST / MAY set and the
behavior the `HermesBackend` adapter guarantees for each.

---

## Required (operator-provided)

| Var | Purpose | If missing |
|---|---|---|
| `HERMES_PROVIDER` | Hermes provider id (e.g., `openai`, `anthropic`, `custom`) | Adapter sets terminal `error` at `start()` with reason `missing_env:HERMES_PROVIDER` |
| `HERMES_API_KEY` | Credential for the provider | Adapter sets terminal `error` at `start()` with reason `missing_env:HERMES_API_KEY`. Value MUST NOT appear in events or logs. |
| `HERMES_MODEL` | Default model id when coordinare does not specify a `model` override on the job | Adapter sets terminal `error` if neither `HERMES_MODEL` nor a job-level model override is present. |

## Optional (operator-provided)

| Var | Purpose | Default |
|---|---|---|
| `HERMES_BASE_URL` | Provider base URL (self-hosted or proxy endpoints) | Unset — Hermes uses its provider default |

## Adapter-managed (operator value, if any, is ignored)

| Var | Why ignored |
|---|---|
| `HERMES_HOME` | Always overridden by the adapter to the job-scoped `tempfile.mkdtemp` path. Honouring an operator value would directly violate FR-005 (no touching `~/.hermes` or any persistent profile). |

## Forbidden

The adapter MUST NOT set or forward any of the following to the
subprocess (these gate communication surfaces FR-006 forbids):

- `HERMES_GATEWAY_*`
- `HERMES_MESSAGING_*`
- `HERMES_CRON_*`
- `HERMES_USER_MEMORY_PATH` / `HERMES_GLOBAL_MEMORY_*`

If any are present in the parent process environment, the adapter MUST
strip them from the subprocess env (defense in depth — Hermes' own
toolset gating is the primary guard, this is the secondary).

## Observability

- The adapter MUST log, at `info`, the resolved provider/model/base_url
  triple (NOT the API key) and the absolute `HERMES_HOME` path at job
  start.
- The adapter MUST log, at `info`, the cleanup outcome for
  `HERMES_HOME` at finalize.
