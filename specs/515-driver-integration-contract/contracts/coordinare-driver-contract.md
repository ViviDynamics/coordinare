# Coordinare ↔ driver integration contract

Issue #515 · Status: **normative draft** · Coordinare performer protocol as of
spec 076 + 080 · driver machine contract **v1**.

The key words **MUST**, **SHOULD**, and **MAY** are to be interpreted as
described in RFC 2119. Every claim is annotated with the evidence it is built
on; if the code moves, the contract moves with it.

Driver is a standalone agent harness in the Coordinare family. The integration
shape is: coordinare dispatches a card to a performer; the performer's backend
adapter drives `driver run` as a subprocess and translates driver's wire protocol
into coordinare's `BackendStatus` vocabulary. The adapter is the only new
coordinare-side code; driver is unmodified container image
(`ghcr.io/vividynamics/driver`).

Evidence anchors used throughout:

- Coordinare adapter seam: `agent/performer/src/performer/backends/base.py`
  (`BackendAdapter`, `BackendStatus`), registration in
  `agent/performer/src/performer/backends/__init__.py` (`SUPPORTED_BACKENDS`).
- Coordinare protocol: `src/coordinare/protocol.py` and its mirror
  `agent/performer/src/performer/protocol.py` (`PerformerStatusType`,
  `PerformerResponse`, `PerformerMetrics`).
- driver CLI and wire: `driver/src/driver/cli.py`, `driver/docs/contract.md` (v1).
- HTTP job transport: `specs/056-performer-containerization/contracts/`,
  `agent/performer/src/performer/server/routes.py`.
- Dispatch payload: `specs/contracts/dispatch-payload.md`.

---

## 1. Session lifecycle and invocation

**C1.** The adapter MUST start driver as:
`driver run --yes --contract <N> --jsonl [--session <path>] <prompt>` — `--yes`
is mandatory for unattended runs (driver refuses to start without it, exit 2);
`--contract` pins the machine-contract version so a coordinare image built
against contract v1 refuses a newer driver **at start** instead of mid-session
(`driver/docs/contract.md`; `--contract` exits 2 on mismatch).

**C2.** Provider/model configuration MUST travel by flags (`--provider
anthropic|openai`, `--model`, `--base-url`, `--temperature`, `--max-tokens`,
`--effort`, `--system`), never by argv-embedded secrets. API keys MUST arrive
in the environment (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`); argv is
world-readable (`driver/src/driver/cli.py` — there is no `--api-key` flag by
design). Coordinare's secret-injection path
(`http_performer_service._build_job_payload`, keys on `card_context["backend"]`)
MUST provide a driver branch that injects only the env key(s).

**C3.** Each card (or resume turn) is one `driver run` process. The adapter MUST
pass `--session <path>` under the job's writable state and MUST resume with
`--resume <path>` for follow-up turns (clarifications, feedback), because
coordinare's relay model is resume-based (§4) and the session file is written
atomically per turn, so SIGTERM-then-resume recovery works
(`driver/src/driver/cli.py` `_load_or_new`).

**C4.** Exit codes are outcomes, not failures: `done` → 0, `blocked` → 0,
`error` → 1, never-started → 2 (no stdout at all). The adapter MUST treat
exit 2 as an **adapter/configuration error** (surface as
`state=error` immediately, do not retry the turn), exit 1 as a turn error, and
exit 0 as consumable regardless of `status` (`docs/contract.md`).

## 2. Wire protocol → `BackendStatus` mapping

**C5.** driver's stdout is one JSON object per line; event `type`s are
`progress | tool_use | thinking | cost | error | output` — chosen to match
coordinare's `BackendEventType` verbatim (`agent/performer/src/performer/models.py`
lines 40–46). The adapter MUST map `BackendEvent(**line)` directly, with no
per-backend event-type table. An unrecognized event type MUST be recorded as
`progress`-equivalent and logged, never dropped silently and never fatal: the
contract version preflight (C1) is the versioning gate, not the event stream.

**C6.** The final non-event `result` line carries
`session_id, status, questions, usage, stop_reason, turns, contract, driver, output, error`.
The adapter MUST map onto `BackendStatus`:

| driver result | `BackendStatus` | downstream coordinare status |
| --- | --- | --- |
| `status="done"` | `state="done"` | per workflow (e.g. `pr_opened`, `approved`) |
| `status="blocked"` + non-empty `questions` | `state="blocked"`, `questions` | `blocked` (questions reach the board, §4) |
| `status="error"` | `state="error"`, `error_reason` | `error` |
| `stop_reason="max_tokens"` | `state="done"`, `stop_reason` set | `token_limit` (`main.py` maps it; `monitor._phase_token_limit` posts advice) |
| `stop_reason="schema_violation"` | `state="error"`, `error_reason` | `error` |

The adapter MUST NOT invent new values inside `PerformerStatusType`
(`agent/performer/src/performer/protocol.py:18–64`): a status missing from the
Literal breaks the job poll loop. `PerformerResponse.questions` enforces
`minLength=1` when blocked (`src/coordinare/protocol.py:402–407`) — an empty
questions list on `blocked` is a contract violation, not a valid state.

**C7.** `usage` (from the `result` line and `cost` events) maps to
`PerformerMetrics.tokens_processed`. Token accounting is best-effort across
coordinare backends today (`monitor_performer` treats unreported as delta 0,
emitting `monitor_performer.tokens_processed_unreported`); a driver adapter that
reports `usage` SHOULD therefore meet the spec-034 bar and MUST NOT report
synthetic zeros when the provider gave nothing — omit the field instead.

**C8.** The `output` field on the result line is driver's final textual answer.
The adapter MUST carry it as `BackendStatus.output` so the workflow's summary
can use it; the assessor's question cap (2, `workflows/assessor/gate.py`)
applies to coordinare-side synthesis, not to driver's own `questions` list, which
the adapter MUST pass through unmodified.

## 3. What "just clicks into place" — coordinare-side expectations of driver

This section is the field registry for the adapter (work item 1 of #515):

1. **Backend registration.** One `SUPPORTED_BACKENDS` entry
   (`backends/__init__.py`): `"driver": ("performer.backends.driver",
   "DriverBackend")`, plus a `card_context["backend"] == "driver"` branch in
   `http_performer_service._build_job_payload` for secret/env injection (C2).
2. **Adapter surface.** Exactly `BackendAdapter`:
   `start(stand, score, *, model, effort, temperature, max_tokens)`,
   `get_status()`, `drain_events()`, `relay_feedback(feedback)`, `stop()`
   (`backends/base.py:26–57`). No other coordinare seam may know driver exists.
3. **Dispatch payload fidelity.** `dispatch_card` passes `card_context`
   through verbatim as job `metadata` (`http_performer_service.py:939–956`);
   the adapter's `Score` MUST declare every field it reads
   (`specs/contracts/dispatch-payload.md` — undeclared keys are dropped
   silently). The adapter MAY read `score.role`, `score.persona`,
   `score.branch`, `score.prior_clarifications` like other backends.
4. **Prompt/persona.** `--system` receives the persona text; `--root` confines
   driver's file tools to the workspace (`--root` is confinement, not a jail —
   the sandbox boundary remains coordinare's container).
5. **Proxy topology.** driver speaks `--provider openai` over Chat Completions,
   so it is **proxy-eligible** for dual-model orchestration (spec 080) via the
   provider-base-URL seam: the adapter MUST set the provider base-URL env
   (`DRIVER_BASE_URL`, per `proxy/launch.py`'s one-env-var contract) when
   orchestration is active. Driver MUST also work `strategy: single` with no
   proxy (direct provider).
6. **Terminal contract.** The adapter inherits `main.py`'s
   `BackendStatus → PerformerResponse` translation and the HTTP job transport
   (`GET /jobs/{id}/stream` SSE included); it contributes nothing new to the
   wire — that is the point of the closed `PerformerStatusType` vocabulary.

## 4. The ask channel when the driver is a meta harness (work item 2)

Today's round-trip is human-terminated: performer emits
`state=blocked, questions=[...]` → coordinare moves the card BLOCKED and posts
the questions as an issue/board comment (`handle_blocked.py`, 24h dedup) → a
**human** comments the answer → `check_board` picks the comment up, records
`{questions, answer}` in `card_clarifications`, and the next dispatch carries
`clarifications` → the adapter resumes driver with them
(`check_board.py` ~1430–1464, `dispatch-payload.md` lines 77–78). Two facts
constrain a meta harness:

- **Bot-author filter.** `check_board` skips comment authors ending in
  `[bot]` or `vivi-coordinare`. A driver that answers by posting issue comments
  as a bot is invisible to coordinare, by design.
- **No in-flight feedback wire channel.** `relay_feedback` over HTTP buffers a
  warning ("the wire protocol has no in-flight feedback channel",
  `http_performer_service.py:726–735`); feedback reaches a session only on
  the next dispatch.

**Resolution — the meta harness answers in the session, not on the board:**

**A1.** When the driver of driver is a meta harness (coordinare driving driver as a
performer, or qa-harness driving driver for QA), an `ask` tool call is answered by
**resuming the session**: `driver run --resume <session> --yes "<answers>"`,
which reopens the session, clears `questions`, and returns `status` to
working (`driver/src/driver/cli.py`, `tools.py:94–101,173–189`). The resume
answer MUST be supplied by the driver's policy, which for coordinare is the
human round-trip above (the driver is the meta harness, but the *authority*
for an ask is still the human card owner); for qa-harness the authority is its own
verdict policy, per its spec.

**A2.** Coordinare's relay seam is the bridge: the adapter's
`relay_feedback(feedback)` MUST translate into a resume of the live session
when one exists (in-flight answer), falling back to recording
`card_context["relay_feedback"]` for the next dispatch when the process has
exited. The contract does not require a new wire RPC — but an adapter MAY
implement in-flight relay by SIGTERM-safe resume (C3, atomic session file).
The buffered-warning behavior for the HTTP path is unchanged until the wire
protocol grows a feedback channel (deferred, §7).

**A3.** Board attribution stays honest. A programmatic answer that reaches the
board MUST NOT impersonate a human: the relay payload's `author_login`
convention (`src/coordinare/protocol.py:323–326`, e.g. `human:jdoe` vs. the
driver's own identity) MUST be used verbatim, and meta-harness answers MUST
NOT be crafted to pass the bot filter — the filter exists so that only the
authority-of-record shapes the card. In practice: coordinare→driver asks resolve
through the human on the board (A1 first clause), never by a bot comment
forged as human.

**A4.** Ask determinism. The `blocked → questions → answer → resumed` state
machine MUST be visible in coordinare's structured log
(`dispatch_performer.*`, `monitor_performer.*` events already carry the
transitions) and the questions on the result line MUST be the same strings
coordinare posts to the board — no paraphrase, no truncation beyond the
existing 200/80-char activity-log limits at the UI layer only.

## 5. Compaction and context-window visibility (work item 3)

Current state, stated plainly: **driver slice 1 has no compaction and no
context-window reporting.** Context overflow surfaces only as
`stop_reason="max_tokens"`. Coordinare cannot observe window size today for
any backend except via per-backend env overrides (`HERMES_CONTEXT_WINDOW`,
`OPENCLAW_CONTEXT_WINDOW`) that never reach coordinare's state, and only
`prime_agent` reports a compaction event. This is the largest determinism gap
in the integration.

**P1 (normative, on driver slice 2).** Driver MUST make context accounting
observable to the caller:

- The `result` line MUST carry `usage` with at least
  `{input_tokens, output_tokens, context_tokens, context_limit}` (the last
  two absent in v1 — the breaking addition is the reason driver's
  `docs/contract.md` versioning policy exists; bump the contract version and
  the `--contract` preflight catches it at C1).
- Compaction, when it exists, MUST emit a `progress` event
  `{type: "progress", text: "compaction_start"|"compaction_end", detail: {...}}`
  so the driver can distinguish "context was rewritten" from "context is
  intact" — mirroring the existing `compaction_end` convention
  (`backends/prime_agent.py:402–406`). An aborted compaction MUST surface as
  a terminal `error` event with the provider's reason, never as silent
  truncation.
- The session file MUST record window size and each compaction boundary so a
  resumed session is reconstructible and the driver can assert determinism.

**P2 (normative, on the adapter).** The adapter MUST translate `usage` into
`PerformerMetrics.tokens_processed` (C7) and MUST expose
`context_tokens/context_limit` via `BackendStatus.progress` detail so
`monitor_performer` can carry it into `state`. Until P1 ships, the adapter
MUST set `stop_reason="max_tokens"` through (→ coordinare `token_limit`, which
posts advice as open questions) — that is the honest, deterministic v1
behavior: the budget is exhausted, the run stops, a human sees why.

**P3 (determinism guarantee).** With P1/P2 in place, the calling harness can
answer for any turn: how much context was live (`context_tokens` vs
`context_limit`), whether it was rewritten (compaction events in stream and
session), and what it cost. The coordinare-side acceptance test is a contract
test in `tests/contract/` (the seam used by `test_dispatch_payload.py` /
`test_agent_protocol.py`): feed a recorded driver JSONL transcript, assert the
`BackendStatus` mapping of §2 byte-for-byte, including a `max_tokens` run and
a `blocked` run.

## 6. Versioning and conformance

- Driver's machine contract is versioned (`driver contract` prints it;
  `--contract N` refuses a mismatch at start). The adapter MUST pass the
  version it is written against; a driver upgrade that changes §2's vocabulary
  is a new contract version and a new adapter review, not a runtime surprise.
- Conformance tests live in `tests/contract/` (coordinare side) and driver's
  benchmark suite (`driver/docs/superpowers/*-benchmark-*.md`). The dispatch
  payload field registry (`specs/contracts/dispatch-payload.md`) is enforced
  by `tests/contract/test_dispatch_payload.py`; adapter PRs MUST keep it
  green.

## 7. Deferred (filed separately)

- In-flight feedback as a wire RPC (§A2 half): needs a protocol change in
  `src/coordinare/protocol.py` and the HTTP contract; deferred until a driver
  actually needs it beyond resume-based answers.
- The driver adapter itself (`SUPPORTED_BACKENDS` entry + adapter class +
  contract tests) — the first implementation of this contract.
- Cost-in-dollars accounting (driver slice 2) beyond token counts.
