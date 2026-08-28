# Implementation Plan: Board-Simulation Benchmark — Real Performers (Spec 151)

**Branch**: `151-real-performers` | **Date**: 2026-08-13 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/151-real-performers/spec.md`

## Summary

Close 134's deferred acceptance criterion 3: drive a card to a terminal state with
**real** performers (real model dispatch through the existing `/jobs` path) while
GitHub stays **fully faked** — at the performer's own boundary, not just the
coordinare's. A real performer clones/pushes a git remote and opens its PR + polls
check-runs *itself* over the network (`agent/performer/src/performer/github.py`), so
faking GitHub for it means standing up (a) a harness-local **git remote** the
container can clone/push and (b) a harness-local **REST surface** answering PR-create
/ default-branch / check-runs, both backed by the **same** `FakeGitHubService` state
(one PR identity, one CI source of truth). The performer's REST base is already
config-injectable (`dispatch_performer.py:1394` flows `config.github_api_url` into the
dispatch); the git host is not (`workspace.py:263,284` hardcodes `github.com`) — that
injection point plus two `api.github.com` leaks (the performer's graphql URL and its
`https`-only `repo_url` regex) are the surface this spec changes. One hard constraint
governs the fake's address: the performer applies `github_api_url`/`github_graphql_url` at
runtime and accepts an `http` host **only** for loopback (`main.py:1489`), so the bench
serves the fake on `127.0.0.1` and launches the performer with `--network host` (a new
`network_mode` option on `performer_lifecycle.start_ephemeral`, which has none today). The
runner gains a non-stub real path at the existing `# ponytail` seam (`bench/runner.py:16`).

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv). Generated
git operations run inside the Debian-based performer container.
**Primary Dependencies (reused, no new runtime deps)**: `FakeGitHubService`
(`services/fake_github.py` — extended, not replaced), `CoordinareDaemon` +
`CoordinareGraphBuilder` (the real graph, no stub overrides in real mode),
`http_performer_service` (`/jobs` dispatch), `WorkspaceManager`
(`workspace.py` — git-host injection point), the performer git/REST client
(`agent/performer/src/performer/github.py`, `.../workspace.py`, `.../models.py`,
`.../config.py`), aiohttp (the fake REST server — same dep the proxy shims already
use), `git daemon` (git's own anonymous smart-protocol server, for clone/push),
pydantic 2.x (artifact + protocol models — reused), structlog.
**Storage**: None new in coordinare. Reuses 134's harness-owned run dir for the
artifact and `state_store=None` daemon. PR/CI state is the in-memory
`FakeGitHubService` (per run). Git is a real local bare repo (134's `materialize_repo`).
**Testing**: pytest via `make test` (unit + contract) / `make test-all` (adds
integration). New deterministic tests under `tests/unit/test_151_*.py`
(fake-REST handlers, git-host injection, graphql/regex config) + a **contract** test
proving the performer-facing fake serves the exact shapes the performer's
`github.py` parses. The real-model end-to-end run is **opt-in** (a marked/skipped
integration lane needing a cheap model endpoint + Docker), never in the free unit CI.
**Target Platform**: Linux (dev + CI), same as coordinare; performer in Docker.
**Project Type**: single project (coordinare) + the performer package (`agent/performer`).
**Performance / termination budget**: bounded by 134's `max_cycles` + wall-clock guard
(SC-005) — every real run terminates and emits a schema-valid artifact even on
performer/network failure. Runtime is dominated by real models; the measurable budget
is "always terminates within budget", not throughput.
**Scale/Scope**: one config, one run, one fixture card end-to-end (134's
one-fixture-per-repo constraint holds). No scoring, sweep, or optimizer (135/136/137).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. No new external dependencies (aiohttp + `git daemon`
  are already present). Changes are additive and single-responsibility: a new
  `bench/fake_github_server.py` (REST + git-remote lifecycle), a real branch in
  `bench/runner.py`, and three narrow injection points (git host, graphql URL, repo_url
  scheme) each config/flag-gated so **production behavior is unchanged unless the bench
  flag is set**. No speculative abstraction — one fake, one server, one real runner path.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. A **contract test** asserts the
  fake REST returns the exact JSON shapes the performer's `github.py` reads
  (`create_pull_request` → `html_url`/`node_id`; `get_default_branch` →
  `default_branch`; `get_check_runs` → `check_runs[]`). Unit tests cover each injection
  point (git-host override builds the fake URL; graphql URL honored; repo_url scheme
  flag). 134's full suite MUST stay green (SC-006) — the stub path and existing fake
  methods are untouched. The real-model run is deterministic-free-CI-exempt by design
  (opt-in lane).
- **III. User Experience Consistency** — N/A (no user-facing UI; a CLI + machine-read
  artifact). Misconfig (container can't reach fake, PR-create fails) surfaces an
  actionable error and still emits an artifact (spec Edge Cases, FR-008/FR-009).
- **IV. Performance by Design** — PASS via the termination budget in Success Criteria
  (SC-005): 100% of real runs terminate within `max_cycles` + wall-clock, no hang. This
  is the domain-appropriate budget for a model-dominated harness.
- **V. Clarity Before Action** — PASS. The one PM-level ambiguity 134 left open
  (fidelity: fully-faked GitHub vs. real throwaway repo) is **resolved in the spec**
  (Overview: fully-faked, real-repo out of scope). The remaining choices (git-server
  mechanism, scheme relaxation) are implementation decisions resolved in
  [research.md](./research.md), not PM clarifications. Zero `NEEDS CLARIFICATION` remain.

**Result**: All gates pass. No Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/151-real-performers/
├── plan.md              # This file (/speckit.plan output)
├── spec.md              # Feature spec
├── research.md          # Phase 0 — decisions + rationale (git server, leaks, networking)
├── data-model.md        # Phase 1 — entities (performer-facing fake, shared state, git-host config)
├── quickstart.md        # Phase 1 — how to run a real-mode benchmark
├── contracts/
│   ├── performer-facing-rest.md   # the REST shapes the performer's github.py requires
│   └── git-remote.md              # the clone/push contract + scheme/networking
├── checklists/          # (exists) spec quality checklist
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── workspace.py                    # CHANGE — git host configurable (was hardcoded github.com @ 263,284)
├── config.py                       # CHANGE — add git_base_url (default https://github.com), injected into WorkspaceManager
├── services/
│   ├── fake_github.py              # EXTEND — expose PR/CI state to the REST server (open_pr-by-performer, shared identity)
│   └── performer_lifecycle.py      # CHANGE — add network_mode → `docker run --network host` (@131); no such option today
├── graph/nodes/dispatch_performer.py  # CHANGE — set card_context["github_graphql_url"] (parallel to github_api_url @1395)
└── bench/
    ├── runner.py                   # CHANGE — real path at the `# ponytail` seam: start fake server, dispatch real, record real per-persona
    ├── fake_github_server.py       # NEW — aiohttp REST surface + `git daemon` lifecycle, backed by FakeGitHubService
    └── recording_performer.py      # REUSE/EXTEND — record real per-dispatch model/backend/token usage into the artifact

agent/performer/src/performer/
├── config.py                       # CHANGE — add GITHUB_GRAPHQL_URL (default https://api.github.com/graphql)
├── github.py                       # CHANGE — resolve_pr_review_threads uses GITHUB_GRAPHQL_URL (was hardcoded @ 399)
├── main.py                         # CHANGE — apply Score.github_graphql_url at runtime, mirroring the github_api_url override (@1470-1494)
└── models.py                       # CHANGE — add Score.github_graphql_url field (@130); repo_url scheme relaxation flag-gated (ALLOW_INSECURE_REPO_URL) for the local git daemon

scripts/
└── board_bench.py                  # CHANGE — --real flag / real-mode wiring over run_board(stub=False)

tests/
├── unit/test_151_fake_rest.py            # fake REST handlers return performer-parseable shapes
├── unit/test_151_git_host_injection.py   # WorkspaceManager builds repo_url from config.git_base_url
├── unit/test_151_performer_config.py      # graphql URL honored; repo_url flag admits git:// only when set
└── contract/test_151_performer_boundary.py # fake REST ⟷ performer github.py round-trip (create PR / default branch / check-runs)
```

**Structure Decision**: Single-project layout continued from 134. The new
performer-boundary fake lives in the self-contained `src/coordinare/bench/` package
(never imported by production paths); it reuses and shares state with 134's
`services/fake_github.py`. The three production-surface edits (git host, graphql URL,
repo_url scheme) are each **default-off / config-gated** so no production behavior
changes — the bench opts in. The real-mode fixtures reuse 134's
`materialize_repo`/`seed_board`; whole-lifecycle fixture content lives in the sibling
`ViviDynamics/conductor-bench` repo as in 134.

## Complexity Tracking

No constitution violations — section intentionally empty.
</content>
</invoke>
