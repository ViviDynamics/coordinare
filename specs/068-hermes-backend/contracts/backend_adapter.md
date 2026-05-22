# Contract: `HermesBackend` ↔ `BackendAdapter` Protocol

**Feature**: 068-hermes-backend
**Source**: `agent/performer/src/performer/backends/base.py` — `BackendAdapter` Protocol

`HermesBackend` MUST conform to the existing Protocol with no method
signature changes. The contract below restates each Protocol method and
specifies Hermes-specific obligations layered on top.

---

## `async def start(stand, score, *, model=None, effort=None, temperature=None, max_tokens=None) -> None`

**Inputs**
- `stand: Stand` — performer stand context (cwd, repo, branch, secrets)
- `score: Score` — role/persona/card payload built by the performer from
  coordinare's `PerformerEndpoint` request
- Optional model/effort/temperature/max_tokens overrides

**Obligations**
1. Create job-scoped `HERMES_HOME` via `tempfile.mkdtemp(prefix="hermes-job-")`.
2. Write `hermes.config.yaml` inside that directory with
   `disabled_toolsets: [gateway, messaging, cron, clarify, user_memory]`.
3. Build the prompt from `score` using the shared helpers (same shape as
   `junie`/`opencode`/`codex`): persona → card → AC → clarifications →
   queued relay feedback → architecture-plan ref → role output requirements.
4. Validate that `HERMES_PROVIDER`, `HERMES_API_KEY`, `HERMES_MODEL` are
   present; if any is missing, transition to `error` with a clear reason
   and finalize.
5. Spawn `hermes chat -q "<prompt>" --quiet -z --toolsets
   terminal,file,search,browser,todo --provider $HERMES_PROVIDER
   --model ${model or $HERMES_MODEL}` (plus `--base-url $HERMES_BASE_URL`
   when set) as an `asyncio.subprocess.Process` with `HERMES_HOME` set to
   the temp dir.
6. Set `_status` to `working` and return.

**Postconditions**
- `_proc` is a running subprocess OR `_status` is already terminal `error`.
- `_profile_dir` exists on disk and is owned by this adapter instance.

**Forbidden side effects**
- MUST NOT read or write anything under the operator's `~/.hermes`.
- MUST NOT send any message via Hermes' gateway / messaging tools.
- MUST NOT schedule any cron / background task via Hermes.

---

## `def get_status(self) -> BackendStatus`

**Obligations**
- Return one of `working`, `done`, `error`. No Hermes-internal state value
  may leak (FR-009).
- Polling MUST NOT block; the method is sync and must reflect the latest
  observed subprocess exit code without awaiting it.

**State-derivation rule**
- `_proc` alive → `working`
- `_proc` exited with code 0, output file present and parses to non-empty
  JSON → `done`
- Any other exit (non-zero, missing file, empty file, parse failure) →
  `error`
- After `stop()` or timeout-driven `stop()` → `error`

---

## `def drain_events(self) -> list[BackendEvent]`

**Obligations**
- Return and clear `_events`. Same semantics as peer adapters.
- Event payloads MAY include Hermes log excerpts but MUST NOT include
  the raw API key or any value of `HERMES_API_KEY`.

---

## `async def relay_feedback(self, feedback: str) -> None`

**Obligations**
- Append `feedback` to `_feedback_queue`.
- MUST NOT spawn a subprocess, MUST NOT write to the live `_proc`'s
  stdin, MUST NOT alter `_status`.
- The next re-invocation (driven by the performer wrapper, same pattern
  as `claude_code`/`junie`) drains the queue into the new prompt.

---

## `async def stop(self) -> None`

**Obligations**
1. Set `_stop_requested = True`.
2. If `_proc` is running: `_proc.terminate()`, await up to a short grace
   period (≤2s), then `_proc.kill()` if still alive.
3. Invoke `_finalize()` (idempotent) to remove `_profile_dir` and clear
   `_proc`.
4. Ensure `_status` is `error` (with `reason="stopped"` for observability).

**Postconditions**
- No leaked subprocess.
- No leaked profile directory.
- Idempotent: calling `stop()` twice is safe.

---

## Cross-cutting: `_finalize()`

Not part of the Protocol, but a contract internal to the adapter:

- MUST be invoked on every terminal path: normal `done`, malformed-output
  `error`, subprocess crash, `stop()`, and `AGENT_TIMEOUT` expiry
  (FR-011).
- MUST `shutil.rmtree(self._profile_dir, ignore_errors=False)` — failures
  are logged but do not block status transition.
- MUST be safe to call multiple times.
