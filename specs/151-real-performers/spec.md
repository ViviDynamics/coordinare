# Feature Specification: Board-Simulation Benchmark — Real Performers (Spec 151)

**Feature Branch**: `151-real-performers`
**Follows**: Spec 134 (`134-board-sim-benchmark`) — this is a direct follow-up that closes 134's deferred acceptance criterion (real performers, not the stub).
**Created**: 2026-08-06
**Status**: Draft
**Input**: User description: "Follow-up to spec 134: make the board-simulation benchmark drive cards through the full lifecycle with REAL performers (real model dispatch), against a faked GitHub — closing the gap that 134 deferred because a real performer opens its own PR over the network."

## Overview

Spec 134 delivered the evaluation substrate and verified it end-to-end with a
**stubbed** performer (sanctioned by 134's own test criterion). This spec closes
134's deferred acceptance criterion 3: **drive every card to a terminal state with
real performers** — real model dispatch through the existing `/jobs` path — while
still faking GitHub, so the run's outcome quality is real.

**Why it was deferred (the finding, from 134's `real-performer-followup.md`):** a
real performer does not touch the coordinare's in-process `FakeGitHubService`. It
talks to GitHub over the network *itself* — it clones/pushes an **HTTPS** remote and
**opens its own PR** via a REST `POST …/pulls`, then polls check-runs. The coordinare
only reads back the `pr_url`/`pr_node_id` the performer reports. So "fake the GitHub
API only, with real performers" requires faking GitHub at the **performer's
boundary** too — a git remote it can clone/push and a REST endpoint it can call —
not just an in-process class.

**Design decision (this spec resolves 134's open PM question): fully-faked GitHub.**
The performer talks to a **local, in-harness fake GitHub** (git + REST), preserving
134's "simulated board / no real GitHub" guarantee. The rejected alternative —
pointing real performers at a real throwaway GitHub repo with only the board
simulated — is out of scope because it breaks that guarantee and couples runs to
live GitHub state, rate limits, and cost.

This spec **extends** 134's substrate (Protocol, `FakeGitHubService`, runner,
artifact); it does not replace it.

## Clarifications

### Session 2026-08-13

- Q: What platform scope should real mode commit to, given `--network host` + `http://127.0.0.1` URLs only route container→host loopback on Linux? → A: **Linux-only** (dev + CI); macOS/Windows Docker Desktop are unsupported for real mode.
- Q: Should the "bench relaxations default OFF / never change production behavior" guarantee be an explicit requirement? → A: Yes — recorded as **FR-013**.
- Q: How should SC-003 (no real GitHub) define verification, given the free/deterministic lane cannot block egress? → A: **Two levels** — the free lane asserts every configured/dispatched URL is a loopback bench host (no real-GitHub target); the opt-in real run additionally passes with outbound GitHub egress physically blocked.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Drive a card through the full lifecycle with real performers (Priority: P1)

A maintainer (or the 135–137 harness) runs the benchmark in **real** mode over one
seeded card. Coordinare dispatches each lifecycle persona (assessor → architect →
implementer → reviewer → security → qa → tech_writer → closer) as a **real model
call**; the implementer really writes code, really opens a PR, CI really runs the
fixture's pytest, and the card reaches a terminal state. The run emits an artifact
showing the real per-persona dispatches and their token usage.

**Why this priority**: This is the whole point of the follow-up and the last
unmet 134 acceptance criterion. It is the MVP of this spec.

**Independent Test**: Run one cheap fixture in real mode against a cheap model and
confirm the card traverses the real personas to a terminal state and the artifact
validates with more than one real dispatch recorded.

**Acceptance Scenarios**:

1. **Given** a config + one seeded card in real mode, **When** the run executes,
   **Then** coordinare dispatches the configured personas as real model calls and the
   card reaches a terminal state.
2. **Given** the completed run, **When** the artifact is written, **Then** it records
   each real persona dispatch (stage, role, model/backend, status) and best-effort
   token usage, and it validates against the 134 schema.
3. **Given** a satisfiable fixture, **When** the run completes, **Then** the card can
   reach `merged` — implementer opens a PR, gates + CI pass, the approver approves,
   and the merge happens.

### User Story 2 - The performer's GitHub calls are served by the fake, never real GitHub (Priority: P1)

Throughout a real run the performer container clones/pushes a **local** git remote
and opens its PR + polls checks against a **local** REST endpoint. No request reaches
real GitHub. The fake serves those calls in the shapes the performer expects.

**Why this priority**: Equal-priority with US1 — a real performer run cannot both use
real models *and* keep GitHub faked without this. It is the mechanism that makes US1
possible without live GitHub.

**Independent Test**: Run in real mode with outbound access to real GitHub blocked
and confirm the run still completes; assert no request targeted a real GitHub host.

**Acceptance Scenarios**:

1. **Given** a real run, **When** the implementer opens its PR, **Then** the PR-create
   call is answered by the harness's fake REST endpoint (not `api.github.com`).
2. **Given** a real run, **When** the performer clones/pushes, **Then** it uses the
   harness's local git remote (not `github.com`).
3. **Given** a real run, **When** the performer polls check status, **Then** it
   receives a verdict derived from the real pytest CI result.

### User Story 3 - One consistent PR identity end-to-end (Priority: P2)

The PR the performer opens (via the fake REST endpoint) is the **same** PR the
coordinare later reviews and merges (via `FakeGitHubService`). The `pr_url` /
`pr_node_id` the performer reports resolve to a real record on the coordinare side.

**Why this priority**: Without shared identity the coordinare's `check_mergeability` /
`get_pr_reviews` / `squash_merge` would operate on a PR the fake doesn't know,
stalling the card. P2 because it is an internal consistency requirement enabling US1,
not a separately demoable user outcome.

**Independent Test**: In a real run, capture the `pr_node_id` the performer minted and
assert the coordinare's later merge acts on that same id, ending in the merged branch.

**Acceptance Scenarios**:

1. **Given** the performer opened PR X, **When** the coordinare calls
   `check_mergeability`/`get_pr_reviews`/`squash_merge`, **Then** they operate on PR X.
2. **Given** the approver approves PR X, **When** the merge runs, **Then** X is merged
   into the fixture repo's default branch (a real local merge).

### User Story 4 - The approver keys on real gate passes (Priority: P2)

In a real run the card reaches the review stage only after the reviewer/security/qa
personas actually pass. The default approver approves based on those real gate
outcomes plus green CI — not on the stubbed-run proxy (134 approved on "card reached
IN_REVIEW").

**Why this priority**: Preserves the intent of 134's synthetic approver under real
conditions. P2 — refines behavior US1 depends on.

**Independent Test**: Drive a real run where CI is green and the personas pass;
confirm the approver approves; drive one where CI is red and confirm it withholds.

**Acceptance Scenarios**:

1. **Given** a real run with all gates + CI green, **When** the approver is consulted,
   **Then** it approves.
2. **Given** a real run with CI red (or a gate not passed), **When** the approver is
   consulted, **Then** it withholds and the card does not merge.

### Edge Cases

- **Performer cannot reach the fake services** (Docker networking misconfigured): the
  run must fail fast with an actionable error, not hang.
- **Performer's PR-create fails** (fake REST returns an error): the card reaches a
  terminal non-merge state (`error`/`blocked`) and an artifact is still emitted.
- **Real model loops or stalls**: the 134 wall-clock + cycle budget still bounds the
  run; the card gets a terminal state and the artifact is emitted.
- **Fixture whose acceptance test the model fails to satisfy**: CI is red → approver
  withholds → card terminates non-merge (this is a *valid* outcome — the substrate
  records it; judging quality is 135, not here).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The benchmark MUST support a real mode that drives every card to a
  terminal state via real performer dispatch (real models) through the existing
  `/jobs` path, reusing 134's runner and artifact.
- **FR-002**: A harness-owned **fake GitHub REST surface** MUST answer the calls a
  performer makes to GitHub during a run — at minimum: open a pull request, read the
  repository's default branch, and report commit check-runs — in the shapes the
  performer expects.
- **FR-003**: The performer MUST clone from and push to a **harness-local git
  remote**, never a real GitHub host.
- **FR-004**: The coordinare MUST be able to point its performer dispatch at the faked
  git host and REST endpoint (today the git host is hardcoded to `github.com`); this
  MUST be a configuration/injection change, not a rewrite of the performer.
- **FR-005**: Pull-request and CI state MUST be shared and consistent between the
  performer-facing fake and the coordinare-facing `FakeGitHubService`, so the PR the
  performer opens is the same PR the coordinare reviews and merges (same identity).
- **FR-006**: CI reported to the performer MUST be the **real pytest** result for the
  PR head, consistent with 134's fake CI (one source of truth).
- **FR-007**: The default approver MUST approve based on the real coordinare gate
  outcomes (reviewer/security/qa passed) plus green CI in a real run, superseding the
  stubbed-run proxy.
- **FR-008**: A real run MUST remain bounded (134's wall-clock + cycle budget) and
  MUST always emit a schema-valid artifact; the artifact MUST now capture the real
  per-persona dispatches and best-effort token usage per dispatch.
- **FR-009**: The harness MUST tear down the performer-facing fake cleanly — stop any
  local servers, remove ephemeral containers, and delete temporary repos/scratch.
- **FR-010**: A real run MUST make **no network calls to a real GitHub host**; this
  MUST be verifiable.
- **FR-011**: This spec MUST reuse 134's substrate (Protocol, `FakeGitHubService`,
  runner, artifact) and MUST NOT regress 134's stubbed path or its tests.
- **FR-012**: Scope stays one config, one run, end-to-end, artifact — **no scoring,
  no sweep, no optimizer** (135/136/137).
- **FR-013**: Every production guard the bench relaxes — the configurable git host, the
  `git://` scheme (via `ALLOW_INSECURE_REPO_URL`), the http-loopback GitHub URL, and
  `--network host` — MUST default to today's production behavior and take effect **only**
  when the bench explicitly opts in. A run with the bench flags unset MUST leave every
  production path unchanged (no regression to the stubbed path or production dispatch).

### Key Entities

- **Performer-facing fake GitHub** (implemented as `FakeGitHubServer` in code): the
  harness-local surface a performer container uses during a run — a git remote
  (clone/push) and a REST endpoint (open PR, default branch, check-runs) — backed by the
  same PR/CI state as `FakeGitHubService`.
- **Shared PR/CI state**: the single record of a card's PR (identity, head, reviews,
  merge) and its CI result, read by both the performer-facing fake and the
  coordinare-facing fake.
- **Git-host configuration**: the injection point that makes the coordinare's performer
  dispatch target the faked git host + REST endpoint instead of `github.com`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A card driven in real mode against a cheap model reaches a terminal
  state and produces a schema-valid artifact.
- **SC-002**: The artifact for a real run records more than one real persona dispatch
  (the configured lifecycle stages), each with a real model/backend and best-effort
  token usage — distinguishable from a stubbed run's single synthetic dispatch.
- **SC-003**: During a real run, zero network requests reach a real GitHub host,
  verified at two levels: (a) a **free/deterministic** check asserts every configured and
  dispatched URL is a loopback bench host — no `api.github.com`/`github.com` target; and
  (b) the **opt-in real run** additionally completes with outbound GitHub egress
  physically blocked.
- **SC-004**: The `pr_node_id` the performer minted is the identity the coordinare
  merges — the opened PR and the merged PR are the same record.
- **SC-005**: 100% of real runs terminate within the configured budget (no hang) and
  every card receives a terminal outcome in the artifact.
- **SC-006**: 134's substrate and its full test suite remain green (no regression);
  the stubbed path is unchanged.

## Token & Cost Measurement Pipeline

The artifact's per-dispatch `tokens_processed` / `seconds` and the derived `cost`
(SC-002, FR-008) are produced by a single chain that runs **inside the performer
container**, crosses the `/jobs` boundary, and is joined back together by the runner.
Every hop is a place a number can be lost, so all of them matter:

1. **Backend adapter → `BackendStatus.tokens_processed`.** Each backend
   (`agent/performer/src/performer/backends/*.py`) reads usage out of its own CLI/API
   stream and sets `tokens_processed` on the terminal `BackendStatus`. For
   `claude_code` this is read from the CLI `result` event
   (`claude_code.py`), which is the **only** point it is known — no intermediate
   `working` response carries it.
2. **`collect_metrics()` → `PerformerMetrics.tokens_processed`.** The performer's
   `collect_metrics()` (`main.py`) copies the backend's count into the metrics object
   returned on a response.
3. **Metrics stamped on the TERMINAL response.** The in-job status loop
   (`main.py`, `_perform_job`) attaches `collect_metrics(...)` to the **terminal**
   response before it is serialised into `JobResult.summary`. Historically metrics were
   attached only to `working` responses — so a backend that learns its token count only
   at completion (`claude_code`) reported `null`. The terminal stamp is what makes the
   count survive.
4. **Coordinare `check_status` passes it through.** `http_performer_service.check_status`
   returns the parsed `PerformerResponse` on the terminal branch; `metrics` (and hence
   `tokens_processed`) rides along inside it.
5. **Recorder reads + records.** `bench/recording_performer.py` wraps the performer
   service; on each non-`working` `check_status` return it records
   `result["metrics"]["tokens_processed"]` and the terminal marker/time, keyed by
   `session_id`.
6. **Runner joins by session → artifact.** `bench/runner.py::_records_to_dispatch_log`
   joins each dispatch to its terminal record by `session_id`, giving the real
   `finished_at`, `seconds = finished - started`, the job/session/container ids, and the
   honest terminal status (e.g. a `changes_requested` closer records `failed` +
   `terminal_marker`, not `succeeded`). `cost` is `tokens × --cost-rate` (FR-012 —
   estimate, not authoritative proxy USD).

**Backend coverage.** The pipeline is backend-agnostic from step 2 onward, so it works
unchanged for **any backend that populates `BackendStatus.tokens_processed`**. Today
that is `claude_code`, `codex`, `hermes`, and `junie`. The remaining adapters
(`opencode`, `opencode_compat`, `openclaw`, `pi`) return `state="done"` with no token
count, so those dispatches record `tokens_processed: null` and null cost — a
backend-capability gap, not a run error (the run still terminates and emits a valid
artifact). To add coverage, teach that adapter's terminal-event handler to parse usage
and set `tokens_processed` (same shape as `codex.py` / `junie.py`); this is a performer
change and requires a Docker rebuild. Some CLIs may not surface usage at all, in which
case no adapter change can recover it.

**Self-hosted-model caveat (why the LiteLLM variant needs `MAX_THINKING_TOKENS=0`).**
The `claude_code` CLI sends a `thinking` block by default. A self-hosted non-reasoning
model behind LiteLLM rejects it (`400 … "<model>" does not support thinking`) on
**every** request, so stages terminate `blocked` with `pytest_exit: 2` and
`tokens_processed: 0`. Setting `MAX_THINKING_TOKENS: "0"` in the endpoint `env` disables
it. This is bench-only — a real Claude endpoint should keep extended thinking.

## Running the Benchmark

The entry point is `scripts/board_bench.py`. Runs write `runs/<ts>/run.json` (a
134-schema `RunArtifact`). See `quickstart.md` for the networking deep-dive; this section
is the self-contained how-to.

### Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--real` | off (stubbed) | Dispatch real performers instead of the stub implementer. |
| `--config <yaml>` | — | Coordinare config (fingerprinted into the artifact). Required for real mode. |
| `--fixtures <yaml>` | built-in tiny fixture | Fixture manifest. A missing path fails fast with `FileNotFoundError`. |
| `--run-dir <dir>` | `runs/` | Where `run.json` is written. |
| `--wall-clock-budget <s>` | `1200` | Real-mode budget → `max_cycles = ceil(budget / poll_interval)`. |
| `--poll-interval <s>` | `5.0` | Seconds between real-mode status polls. |
| `--cost-rate <usd>` | `3.0` | USD per million tokens for the cost estimate. |
| `--max-cycles <n>` | derived | Override the cycle budget directly. |

### Stub run (free, no Docker / model / keys)

Confirm the harness works deterministically before any real-mode setup:

```bash
.venv/bin/python scripts/board_bench.py \
  --fixtures specs/151-real-performers/examples/fixture-manifest.yaml \
  --run-dir runs/
```

Drives the card to `merged` with the stub implementer + real CI/merge, at zero cost.

### Real run (opt-in, paid) — prerequisites, IN ORDER

1. **Dev environment** — `.venv` via `uv`; `git` on PATH (the harness runs `git daemon`).
2. **Docker** — portable across Docker Desktop and native Linux; the performer reaches
   the harness-local fakes over `host.docker.internal`
   (`--add-host=host.docker.internal:host-gateway`, Docker 20.10+). Verify `docker info`.
3. **Build the performer image** named by the config's `image:` (`coordinare-performer:full`):
   `bin/build --docker` (builds `:base` + `:full`). **Rebuild rule:** config / runner /
   server / test edits need no rebuild; any change under `agent/performer/**` (e.g. the
   token-metrics stamp) does.
4. **Export the model credentials** the config's backend needs (see the two variants
   below).

### Config variant A — `claude_code` via LiteLLM gateway (reports tokens/cost)

`bench-real.litellm.yaml` — routes `claude_code` through a LiteLLM proxy and exercises
the full token/cost pipeline. This is the **verified-green** path.

```bash
export ANTHROPIC_BASE_URL=https://your-litellm-host      # proxy root, NO /v1 suffix
export ANTHROPIC_AUTH_TOKEN=<litellm-master-key>
.venv/bin/python scripts/board_bench.py --real \
  --config specs/151-real-performers/examples/bench-real.litellm.yaml \
  --fixtures specs/151-real-performers/examples/fixture-manifest.yaml \
  --run-dir runs/ --wall-clock-budget 900
```

The endpoint `env` sets `BACKEND: claude_code` (entrypoint installs the CLI),
`MAX_THINKING_TOKENS: "0"` (see caveat above), `ALLOW_INSECURE_REPO_URL` +
`ALLOW_HOST_GATEWAY_GITHUB`. `base_url` is the gateway root with **no** `/v1` (the CLI
appends `/v1/messages`; a `/v1` suffix yields `/v1/v1/messages` → 404). Set `model:` in
the `model_endpoints` block to a model your gateway serves.

### Config variant B — `opencode` on a vendor cloud (no token reporting)

`bench-real.yaml` — `backend: opencode` against `anthropic-cloud`
(`auth_env: ANTHROPIC_API_KEY`, model `claude-haiku-4-5-20251001`).

```bash
export ANTHROPIC_API_KEY=<key>
.venv/bin/python scripts/board_bench.py --real \
  --config specs/151-real-performers/examples/bench-real.yaml \
  --fixtures specs/151-real-performers/examples/fixture-manifest.yaml \
  --run-dir runs/
```

`opencode` does not surface usage, so this run terminates and emits a valid artifact but
records `tokens_processed: null` / null cost (see Backend coverage above).

Either way, **do NOT change `github_org` / `project_name`** — they MUST stay
`bench-org` / `bench-repo` (the `FakeGitHubService` identity).

### Inspect the result

```bash
jq '.cards[] | {title, final_state, merged: .merge.merged,
     tokens: .cost.tokens_processed, cost: .cost.cost_usd,
     dispatches: [.dispatches[] | {stage, status, terminal_marker, seconds, tokens_processed}]}' \
  runs/<ts>/run.json
```

The tell for a real run is **>1** dispatch per card (stub records one synthetic dispatch).

### Automated tests (free, deterministic)

```bash
.venv/bin/python -m pytest tests/unit -k 134 -q   # fake-REST shapes, git-host injection, recorder join
```

The real-model end-to-end run is opt-in (Docker + paid model) and never runs in the free
deterministic lane (SC-006, 134 parity).

### Notes

- **`security_floor.scan_failed` (ScannerError)** during a run means semgrep/bandit are
  not installed on the host — the security floor is nominal, not enforced. `uv pip
  install semgrep bandit` to make it bite. Non-fatal (fails closed).
- Cost is a token×rate **estimate** (FR-012), not authoritative proxy USD.

## Assumptions & Dependencies

- **Depends on spec 134** (this branch stacks on `134-board-sim-benchmark`); 134 must
  land first or merge together.
- **Requires Docker on Linux** — real mode is **Linux-only** (dev + CI). The performer
  container reaches the harness-local fake services via `--network host` + loopback
  (`127.0.0.1`) URLs, which only routes container→host on Linux; macOS/Windows Docker
  Desktop are unsupported for real mode.
- **Requires a cheap model endpoint** for the verification run; the automated
  integration test from 134 stays stubbed (deterministic, free) — the real-mode run
  is opt-in and validated manually or in a separate, non-unit CI lane.
- **Fully-faked GitHub is the chosen fidelity** (see Overview); real-throwaway-repo is
  explicitly out of scope.
- Cost remains a **token×rate estimate** (134 FR-012); authoritative proxy USD stays
  deferred.

## Out of Scope

- Scoring / correctness judgement of produced code (135).
- Config sweep / noise measurement (136) and the optimizer (137).
- Authoritative per-request cost via the LiteLLM proxy.
- Multi-card whole-lifecycle runs in a single shared repo (134 documented the
  one-fixture-per-run constraint; per-card repos are a possible refinement, not
  required here).
