# Feature Specification: QA Cycle 076

**Feature Branch**: `076-qa-cycle`
**Created**: 2026-05-28
**Status**: Draft — actively collecting QA findings
**Input**: User description: "Another QA and optimization iteration on coordinare, started during live testing immediately after the 075 implementer CI gate landed on main. Seed bug: coordinare spawned two performer containers on the same card after a restart sequence. More findings will be appended as testing continues."

## Clarifications

### Session 2026-05-28

- Q: What is the default recovery action when coordinare detects the wedged state (`active_card` pinned + `active_sessions` empty + `phase` in {None, idle})? → A: Release the pin (set `active_card=None`) and let the next poll cycle re-pick via normal eligibility; aligns with "board is source of truth" (FR-025).
- Q: What is the canonical branch naming pattern that the dispatch path enforces per card? → A: `coordinare/<card_node_id>/<deterministic-slug-from-card-title>` — slug is derived purely from the card title (lowercased, non-alphanumerics → `-`, collapsed runs, trimmed, truncated to ~60 chars), so the name is stable across dispatches and shared across all lifecycle stages on a single PR.
- Q: How many idle-timeout retries does a card get per stage before the orchestrator gives up? → A: 2 retries per `(card_id, performer_stage)` per rolling 24-hour window; third stall transitions the card to BLOCKED with a `card_blocked` notification. Matches the 075 CI gate's `max_bounces_per_head=3` precedent.
- Q: How often does the orchestrator query GitHub for multi-PR divergence per tracked card? → A: Not every poll cycle. Trigger the multi-PR detection check on three events: (1) immediately before a `dispatch_performer` call, (2) during the restart-time reconciliation pass, and (3) opportunistically when a PR-modifying webhook event arrives. Keeps GitHub API usage bounded to the moments a new divergent PR could actually have appeared.
- Q: On a relay-handoff (PARTIAL_PROGRESS or any mid-flow path that clears `agent_dispatch`), should the prior container be drained or stopped? → A: Drain first with a bounded 5 s budget — request the job-runner to land its current LLM call cleanly; if it does not finish within 5 s, force-stop via `docker stop` with an additional 5 s SIGTERM-then-SIGKILL window. Total relay-handoff teardown latency ≤ 10 s.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — A Card Has At Most One Live Performer Container At A Time (Priority: P1)

An operator restarts coordinare while a card is `IN_PROGRESS` with a live performer container. After the restart, exactly one performer container is associated with the card — never two, never zero. The operator does not see two Slack notifications for the same card on the same performer, and the operator does not need to manually `docker stop` a stranded container.

The build path that explains today's failure: when coordinare restarts and finds an `IN_PROGRESS` card in its persisted snapshot, the new process re-creates the session id but cannot reach the original container because the in-process registry (`_active_jobs`) is fresh and empty. The `check_board` stale-session detector reads the empty registry as "session dead," clears `agent_dispatch`, and the next graph tick lands in `dispatch_performer`, which has no precondition guard and spawns a new container. The old container survives the daemon restart because Docker workloads outlive their orchestrator's SIGTERM, so the system ends up with two competing implementer turns pushing to the same branch and emitting independent Slack notifications.

The desired behaviour is a steady-state invariant: at every moment, for every `(card_id, performer_stage)` tuple, there is **at most one** running performer container that coordinare is currently authoritative over. When a daemon restart finds a card with a still-alive prior container, coordinare MUST resolve the situation by either re-adopting the container into its in-process registry (so the existing session is honoured) or by reaping the orphan before launching a replacement — never by silently spawning a duplicate.

**Why this priority**: This is the most expensive class of coordinare bug. Each duplicate dispatch burns tokens at the LLM provider in proportion to a full implementer turn (tens of minutes of inference), races against itself for git pushes (causing avoidable merge conflicts on the PR branch), and emits duplicate Slack notifications that train operators to ignore the channel. It also undermines every per-card invariant downstream of dispatch: bounce counters, CI gate decisions, and persona scope choices all assume one performer per card. Without this fix, the 075 CI gate's correctness is at risk because the gate can fire against the wrong head SHA when two implementers race to push.

**Independent Test**: With one card in `IN_PROGRESS` and a live performer container, restart coordinare. Within 60 seconds the system MUST be in one of two states: (a) the original container is registered in the new process's in-flight registry and `agent_dispatch.session_id` matches it; or (b) the original container has been stopped (verified via `docker ps`) and a single new container has been launched. The test FAILS if at any point during or after the restart there are two running containers belonging to the same card and stage.

**Concrete Repro (today's incident, card #101 / PR #133)**: With the `website` symphony running and card #101 (PR #133) in `IN_PROGRESS`, the operator restarted coordinare three times in quick succession (21:08, 21:14, 21:23 wall time). Each restart found the card's persisted session id, declared it stale because the new process's `_active_jobs` was empty, cleared `agent_dispatch`, and the next graph tick dispatched a fresh container. The orphaned containers from the prior restarts kept running because Docker containers survive their orchestrator. The system ended up with two containers (`compassionate_meitner` at 22:06:50 UTC, `keen_poitras` at 22:07:41 UTC) both running identical `claude --print … ## Role Implement the card` invocations against `spark/qwen3.6:35b`. Both continued making LLM completions for 30+ minutes; coordinare was tracking only the newer one. The operator received two Slack notifications for the same card on the same performer, which is how the issue surfaced.

**Acceptance Scenarios**:

1. **Given** a card is `IN_PROGRESS` with a live performer container that coordinare has registered, **When** the operator restarts the coordinare process (SIGTERM + relaunch), **Then** within 60 seconds the post-restart coordinare either lists exactly one matching container in its in-flight registry (re-adopted) or lists exactly one freshly-launched container after stopping the prior one. There is never a moment after the restart settles when two containers for the same `(card_id, performer_stage)` are simultaneously running.

2. **Given** a card is `IN_PROGRESS` and the post-restart re-adoption check determines the prior container's job-runner is unreachable (container exited, host network broken, container ID gone from `docker ps`), **When** the new process proceeds to dispatch, **Then** any lingering container that matches the card by label or job id MUST be forcibly stopped before the new dispatch creates a replacement.

3. **Given** the implementer returns `PARTIAL_PROGRESS` mid-turn (spec 072 relay), **When** the graph routes back through `dispatch_performer` to launch the continuation turn, **Then** the prior turn's container MUST be drained or stopped before the continuation container is started; the steady-state invariant "at most one live container per `(card_id, performer_stage)`" holds across the relay handoff.

4. **Given** the missing-PR fallback path at `dispatch_performer.py:307` fires because PR context disappeared mid-stage, **When** the dispatcher resets `agent_dispatch={}` and re-enters the dispatch flow, **Then** any container that was running under the cleared session id MUST be stopped before a replacement is launched.

5. **Given** the operator's Slack channel is wired for `card_dispatched` events, **When** a daemon restart causes a re-adoption (not a re-dispatch), **Then** no new `card_dispatched` notification is emitted — the operator sees one notification per genuine dispatch, not one per restart.

---

### User Story 2 — A Successful Performer Turn Is Never Forgotten (Priority: P1)

When a performer completes a turn — pushes a branch, opens or updates a pull request, and reports success — coordinare MUST record the resulting PR identifiers on the active session and advance the card to the next stage. The operator MUST never end up in a state where a performer produced real artefacts on GitHub (a new branch, a new PR, new commits) but coordinare's session state still references prior/stale artefacts.

The build path that explains today's failure: a first implementer turn for card #101 completed at 22:05:50 UTC, pushing branch `coordinare/PVTI…/feature-time-tracking-schema-and-model-foundation` and opening **PR #148** with the actual schema implementation (+597/−1, 16 files). The performer reported success and the container was cleanly torn down. But coordinare's `state.active_card.pr_url` continued to point at the prior **PR #133** (a +3367/−45, 71-file monster branch from a previous attempt). The new PR was never recorded; downstream stages (review, QA, closer, CI gate) would have evaluated against the wrong branch and the wrong head SHA. Even worse, the next implementer dispatch (Anomaly 3 below) tried to redo the same work because coordinare still believed the implementation didn't exist on the card's "current" PR.

The desired behaviour: every successful performer turn that produces or updates GitHub artefacts MUST have those artefacts reflected in the card's session state before the next stage dispatches. If the performer reports a PR url, pr node id, branch name, or head SHA in its output, those values MUST overwrite the corresponding fields on `state.active_card` and `state.active_sessions[card_id]`. The persisted snapshot MUST capture the updated values within the same poll cycle that processes the performer's success — no delayed-write, no "next cycle will catch it" patterns.

**Why this priority**: This is a correctness-critical data-loss bug. Real work was produced on GitHub and silently disconnected from the orchestrator's view of the card. Every downstream check (CI gate, reviewer dispatch, closer merge gate) becomes either a no-op or actively wrong, because they evaluate against a stale PR. In today's incident this caused a duplicate implementer to be dispatched immediately after a successful turn — the orchestrator literally did not know its own implementer had succeeded.

**Independent Test**: With a card in `IN_PROGRESS` and no prior PR, run an implementer turn that opens a new PR. Within one poll cycle after the implementer reports success, `state.active_card.pr_url`, `pr_node_id`, and `head_at_dispatch` MUST reflect the new PR. Restart coordinare; the new values MUST survive snapshot rehydration. The test FAILS if the post-turn state references a different (older) PR than the one the performer actually opened, or if the values are missing.

**Acceptance Scenarios**:

1. **Given** a card with no prior PR enters the implementer stage, **When** the implementer reports success with a new `pr_url`, **Then** `state.active_card.pr_url` is updated within the same poll cycle and persists across daemon restarts.

2. **Given** a card already has a prior PR (e.g., from a previous attempt that left a bloated branch), **When** a fresh implementer turn opens a different PR for the same card, **Then** the card's PR fields MUST update to the new PR — never stay pinned to the prior one — and any downstream stage MUST evaluate against the new PR's head SHA, not the stale one.

3. **Given** the persisted snapshot is the only durable source of card↔PR mapping, **When** a successful performer turn updates the card's PR fields and the next snapshot write happens, **Then** the snapshot MUST contain the new PR fields — not the prior values nor a partial overwrite.

---

### User Story 3 — Successful Stage Completion Advances the Lifecycle (Priority: P1)

When a performer reports success and the orchestrator records the result, the card MUST advance to the next stage in its configured lifecycle. The operator MUST never see "implementer succeeded → another implementer is dispatched on the same card" or "implementer succeeded → coordinare goes idle and the card just sits there."

The build path that explains today's failure: the first implementer turn completed successfully at 22:05:50, but at 22:06:50 (60 seconds later, well within the same coordinare run — no restart) coordinare dispatched **another implementer** on the same card instead of advancing to the reviewer stage. The orchestrator either lost the success result, mis-routed in the graph, or routed through `dispatch_performer` again without consulting the just-completed turn's outcome. Then, after that second implementer hung and timed out, coordinare went **idle** (`phase=None`, `performer_stage=None`, `lifecycle_sequence=None`, `active_sessions={}`) while the card remained pinned in `state.active_card`. The card did not advance, did not return to the board for re-pickup, and was not marked blocked — it was simply abandoned mid-flight.

The desired behaviour: every terminal outcome from a performer turn (DONE, PARTIAL_PROGRESS, BLOCKED, idle-timeout) MUST drive an explicit, recorded lifecycle transition. DONE advances to the next stage in `lifecycle_sequence`. PARTIAL_PROGRESS re-dispatches the same stage with relay feedback (spec 072 behaviour, but only after the prior container is reaped per User Story 1). BLOCKED moves the card to a blocked queue. Idle-timeout, transport error, or any other unexpected exit MUST be treated as a recoverable failure with a documented retry policy — never as silent abandonment. The orchestrator MUST never be in a state where `active_card` is set but `phase`, `performer_stage`, and `active_sessions` are all empty.

**Why this priority**: The bug above turned a successful implementer turn into a stranded card — burning the cost of the success without realizing any of the value (the new PR is not being reviewed, not being closed, not being merged). This is also the path that produces the duplicate dispatch in User Story 1; closing it removes one entire class of re-dispatch trigger. Without this, the 075 CI gate cannot reliably fire either, because the gate runs at the implementer→reviewer hand-off and there is no hand-off here.

**Independent Test**: Dispatch an implementer turn for a card with a clear `lifecycle_sequence` ending in `reviewing`. When the implementer reports DONE, within one poll cycle coordinare MUST either set `performer_stage=reviewing` and dispatch the reviewer, OR (if no reviewer is configured) move the card to IN_REVIEW on the board with no further dispatch. The test FAILS if coordinare dispatches another implementer, goes idle without advancing, or leaves the card in a state where `active_card` is pinned but `performer_stage` is unset.

**Acceptance Scenarios**:

1. **Given** an implementer reports DONE with a clean exit, **When** the next poll cycle runs, **Then** `state.performer_stage` advances to the next entry in `lifecycle_sequence` AND `state.active_sessions[card_id].performer_stage` matches AND a fresh dispatch for the new stage is queued.

2. **Given** an implementer turn ends with `idle_timeout` (claude code reader idle timeout from spec 070 / model stall), **When** the orchestrator processes the outcome, **Then** the card MUST either retry (with the retry counter incremented and visible in logs) or transition to BLOCKED — it MUST NOT silently transition to idle with `active_card` pinned.

3. **Given** the orchestrator processes any terminal performer outcome, **When** the resulting state is written to the snapshot, **Then** at least one of `performer_stage`, `phase=blocked`, or `card moved to terminal board status` MUST be set. The combination "phase=idle + active_card set + no session" MUST be impossible to reach via the success-path graph.

---

### User Story 4 — One Card, One PR (No Silent Branch Forking) (Priority: P2)

When the orchestrator dispatches a performer to work on a card that already has an open PR for that card, the performer MUST update the existing PR — not open a new one. If a fresh branch is genuinely warranted (e.g., the existing branch is so divergent that recovery is more expensive than restarting), that decision MUST be a deliberate orchestrator action with a recorded rationale, not an emergent behaviour from the performer choosing its own branch name.

The build path that explains today's failure: card #101 already had an open PR #133 (a bloated +3367/−45, 71-file monster). When the implementer ran today, it opened a **brand-new** PR #148 on a brand-new branch (`feature-time-tracking-schema-and-model-foundation`) instead of force-pushing to or updating PR #133's branch (`schema-employee-tiers-project-assignments-time-ent`). Both PRs are now open. Coordinare's active_card still points at #133. Downstream reviewers, CI checks, and merge gates all see two competing PRs for one card. There is no human decision-point at which "abandon #133, work in #148" was made — the performer made up a branch name and coordinare's dispatch path didn't constrain it.

The desired behaviour: the dispatch path MUST resolve a canonical branch name per card, derived deterministically from the card's identifier and the lifecycle. Performers MUST receive that branch name as input and MUST push to it. If a card legitimately needs to be restarted on a fresh branch (e.g., operator override, irrecoverable conflict), that MUST go through an explicit "abandon PR" code path that closes the old PR with a comment explaining why, before the new branch is created.

**Why this priority**: P2 rather than P1 because it is recoverable (the operator can close one PR manually) and because it is partially a consequence of User Stories 2 and 3 — when those are fixed, the duplicate-implementer dispatch path that produces orphan branches is closed at its source. But it is still a real bug: even within a single performer run, a long-running implementer that loses its working tree midway could restart with a different branch name, and the current code paths do not prevent that.

**Independent Test**: With a card that already has an open PR on branch B1, dispatch a new performer turn for the same card. The performer's working tree MUST be initialised on branch B1 (or a deterministically-derived fresh branch ONLY when the operator has explicitly opted into abandonment). The test FAILS if the resulting PR is on a branch other than B1 without an explicit abandonment record.

**Acceptance Scenarios**:

1. **Given** a card has exactly one open PR, **When** any new performer turn is dispatched, **Then** that performer's workspace MUST be initialised on the PR's branch and any commits MUST land on that branch.

2. **Given** a card has TWO open PRs (today's pathological state), **When** the orchestrator next picks up that card, **Then** it MUST flag the divergence to the operator (notification + dashboard surface) and refuse to dispatch a fresh performer turn until the divergence is resolved.

3. **Given** an operator explicitly requests a branch reset (mechanism out of scope here — assume some override exists), **When** the new branch is created, **Then** the prior PR MUST be closed automatically with a comment containing the operator's stated reason and a link to the new PR.

---

### User Story 5 — Project Board and Local State Stay In Sync (Priority: P2)

The GitHub project board's Status field for a card and coordinare's local `state.active_card.status` for the same card MUST never disagree by more than one poll cycle. If they drift, the next poll cycle MUST reconcile them — moving the card on the board to match local state, or updating local state to match the board, with a recorded reason for the choice.

The build path that explains today's failure: by the time of investigation, card #101's Status on the GitHub project board was **`TODO`** (the orchestrator had presumably moved it back when it noticed the session went stale), but coordinare's `state.active_card.status` was still **`IN_PROGRESS`** with `previous_status=IN_PROGRESS`. The board and the orchestrator disagreed; the orchestrator was idle (no session active) but its `active_card` pin prevented the card from being picked up fresh from the board's TODO column. The card was effectively wedged: too "owned" to be re-picked, too dead to advance.

The desired behaviour: the project board is the source of truth for "what queue is this card in." `state.active_card` is a transient pointer that MUST be cleared when the card's board status leaves `IN_PROGRESS`, regardless of the reason it left (operator move, orchestrator-driven status change, external automation). When the board's status disagrees with `active_card.status`, the next poll cycle MUST either re-acquire the card (re-pick from TODO/IN_PROGRESS) or release the pin (set `active_card=None`) so the symphony can pick up the next card.

**Why this priority**: Same severity argument as User Story 4 — recoverable by operator intervention, but causes silent throughput loss. With User Stories 1–3 fixed, the pathways that produce board/state divergence are mostly closed; this story exists to catch the residual races and any future regression.

**Independent Test**: With a card in `IN_PROGRESS` on the board and `state.active_card.id == card_id`, manually move the card to `TODO` on the GitHub project board. Within one poll cycle coordinare MUST notice the divergence and EITHER re-acquire the card (move it back to IN_PROGRESS, restore the session) OR release the pin (`active_card=None`) so the next eligible card can be picked. The test FAILS if the divergence persists beyond one cycle.

**Acceptance Scenarios**:

1. **Given** `state.active_card.id == X` and `state.active_card.status == IN_PROGRESS`, **When** the next poll observes that card X's board Status is now TODO, **Then** within one cycle coordinare MUST either re-acquire X (move it back to IN_PROGRESS) or release `state.active_card` so X can be re-picked through the normal eligibility path.

2. **Given** `state.active_card.id == X` and `state.active_sessions == {}` (today's wedge), **When** the next poll cycle runs, **Then** coordinare MUST log a structured event identifying the wedge and MUST take a recovery action — re-dispatch (if board says IN_PROGRESS), release the pin (if board says anything else), or move to BLOCKED — with a documented rationale.

3. **Given** the dashboard renders both the board status and the local `active_card` view, **When** they diverge, **Then** the divergence MUST be visible on the dashboard so the operator can audit. (Surfaces an existing latent bug; closing the bug is the structural fix.)

---

### Edge Cases

- **Container exists but job-runner is unreachable**: A container is listed in `docker ps` and tagged with the coordinare's session label, but its job-runner HTTP endpoint does not respond within a bounded re-adoption window (default: 15 s). The system MUST treat the container as un-adoptable and stop it before dispatching a replacement; it MUST NOT leave it running.

- **Container exists but is for a stale card or stage**: A leftover container from a prior card or a prior stage of the same card exists on the host but does not match the current `(card_id, performer_stage)`. The system MUST stop these orphans on startup as part of its reconciliation pass; they MUST NOT be re-adopted into the new session.

- **Two containers exist for the same card at startup**: An operator who killed coordinare mid-dispatch may have already produced two containers (today's actual situation). On the next coordinare startup, the reconciliation pass MUST detect the duplicate and choose one to keep: prefer the one whose job-runner responds with a job id that matches the persisted session id; if neither matches or both match, prefer the most recently started; stop all others.

- **Container reports a job that disagrees with the persisted session**: A container's job-runner returns a `job_id` different from the one in `agent_dispatch.session_id` in the persisted snapshot (e.g., the container was reassigned manually, or state corruption). The container MUST be stopped; the persisted session id MUST be cleared and a fresh dispatch performed.

- **Persistent (non-ephemeral) performer mode**: When `performer_endpoints.mode == "persistent"`, `_active_jobs` is not used and `has_live_session` returns true based on endpoint resolution. The re-adoption logic MUST NOT apply to persistent mode — for persistent endpoints, the in-flight guard is sufficient on its own because there is no per-job container to reap.

- **Race between two graph fanout sessions for the same card**: With multi-session fanout (one session per card), it is theoretically possible for two graph invocations to both observe an empty `agent_dispatch` between cycles. The in-flight guard MUST be the synchronisation point, not the dispatch syscall — i.e., the guard MUST be backed by a per-card mutex (or equivalent atomic check-and-set) that prevents two concurrent `dispatch_performer` calls from both passing the guard.

- **Restart during PARTIAL_PROGRESS turnaround**: The performer emits `partial_progress`, the graph sets `phase=dispatching` and `agent_dispatch={}`, and coordinare restarts before `dispatch_performer` runs. On startup the system MUST recognise that `agent_dispatch={}` plus `phase=dispatching` plus a live container belonging to the card is a "relay-mid-turn" state, not a duplicate-dispatch state. It MUST stop the prior container (which was working a stale prompt) before launching the continuation turn.

- **Docker daemon unreachable at startup**: If `docker ps` itself fails on startup (Docker daemon down, socket unmounted, permission error), coordinare MUST refuse to dispatch any ephemeral performer until Docker is reachable. It MUST NOT silently fall back to the old behaviour of dispatching without reconciling — that path is exactly the bug we are fixing.

- **Slack notification dedup across restarts**: The notification layer SHOULD not emit a `card_dispatched` event during the restart reconciliation pass if the resulting state is "container re-adopted, no new dispatch happened." If a stop-and-replace happens, it is acceptable for one new `card_dispatched` to fire, but never two.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001 (in-flight guard)**: `dispatch_performer` MUST refuse to dispatch when `state["agent_dispatch"]` already contains a non-empty `session_id` AND the resolved performer service reports that session as live. The refusal MUST be observable through a structured log event (suggested: `dispatch_performer.in_flight_guard_tripped`) including `card_id`, `performer_stage`, and `session_id`. The refusal MUST NOT advance the graph stage; the graph MUST loop back through `monitor_performer` for the existing session.

- **FR-002 (restart-time reconciliation pass)**: On startup, after loading the persisted snapshot and before the first graph tick, coordinare MUST run a reconciliation pass that, for every card with `phase ∈ {dispatching, monitoring_performer, monitoring_agent}` and a non-empty `agent_dispatch.session_id`, looks for a matching container on the host. The pass MUST be bounded in time (default: 30 s total wall-clock budget for the full reconciliation, regardless of card count) and MUST log a structured `daemon.reconciliation_pass_started` event at the beginning and `daemon.reconciliation_pass_complete` at the end with a summary of actions taken.

- **FR-003 (re-adoption)**: When the reconciliation pass finds a container whose label or job id matches a persisted session id AND the container's job-runner responds healthy within a bounded probe window (default: 15 s), coordinare MUST register the container in the new process's `_active_jobs` (re-adoption) without launching a new container. The re-adoption MUST preserve `agent_dispatch.session_id` exactly as persisted; the graph MUST resume from `phase=monitoring_performer` for that card.

- **FR-004 (reap before replace)**: When the reconciliation pass finds a container that matches a persisted session id but whose job-runner is unreachable, or whose job id disagrees with the persisted session, coordinare MUST stop the container (Docker `stop` with a bounded timeout, default 10 s, falling back to `kill`) BEFORE clearing the session id and routing the card back through dispatch. The stop MUST be best-effort: if it fails after the timeout, coordinare MUST log a `daemon.reap_failed` warning and proceed with the new dispatch anyway, so a sticky orphan container cannot block the system indefinitely.

- **FR-005 (orphan sweep)**: The reconciliation pass MUST also enumerate every container with the coordinare performer label whose session id is NOT in the persisted snapshot (true orphans — e.g. from a prior coordinare run that exited uncleanly). All such orphans MUST be stopped during the reconciliation pass. The sweep MUST log per-orphan structured events including container name, image, started-at timestamp, and any coordinare labels.

- **FR-006 (per-card mutex on dispatch)**: The in-flight guard in FR-001 MUST be enforced via an atomic check-and-set, not a non-atomic check-then-set. Two concurrent graph invocations for the same card MUST NOT both pass the guard. The mutex granularity MUST be `(card_id, performer_stage)` so that two genuinely different stages of the same card can dispatch concurrently if and only if the graph itself permits that.

- **FR-007 (relay handoff containment)**: The `partial_progress` relay path at `monitor_performer.py:2183` and any other path that clears `agent_dispatch` mid-flow MUST first attempt to **drain** the existing container before the next `dispatch_performer` invocation. The drain procedure is bounded: (a) request the job-runner to finish its current LLM call and exit cleanly, with a **5-second drain budget**; (b) if the drain does not complete within the budget, issue `docker stop` with a default 5 s SIGTERM-then-SIGKILL window. Total teardown latency MUST NOT exceed 10 s under any circumstance. If the drain succeeds, the relay handoff MAY use the most recent committed work as the input to the continuation turn; if the drain times out, the continuation turn MUST treat the prior turn as having produced no additional work beyond what was committed before the relay was triggered. The graph MUST NOT enter a state where `agent_dispatch={}` but a container under the prior session id is still actively making LLM calls — the relay handoff is the synchronisation point.

- **FR-008 (stale-session re-dispatch hardening)**: The `check_board._is_stale` path at `check_board.py:484-498` MUST be reframed: instead of unconditionally clearing `agent_dispatch` when `has_live_session` returns False, it MUST first invoke the reconciliation logic for that specific card — attempting re-adoption first, falling back to reap+replace, falling back to fresh dispatch only as a last resort. The log event today emitted as `check_board.stale_session_redispatch` MUST be replaced with a more accurate event that reports which branch was taken (`adopted`, `reaped`, or `fresh_dispatch`).

- **FR-009 (container labelling)**: Every performer container coordinare launches MUST be tagged with Docker labels that allow positive identification on a future startup: `coordinare.session_id`, `coordinare.card_id`, `coordinare.performer_stage`, `coordinare.daemon_started_at`. The reconciliation pass in FR-002 MUST use these labels (not container name or image alone) to match containers to persisted sessions.

- **FR-010 (notification dedup across reconciliation)**: When the reconciliation pass results in a re-adoption (FR-003), coordinare MUST NOT emit a `card_dispatched` notification. When the reconciliation results in a reap-and-replace (FR-004), coordinare MAY emit one `card_dispatched` notification for the new container, but it MUST NOT also emit one for the reaped container. The notification layer MUST consult the reconciliation outcome to decide.

- **FR-011 (persistent-mode opt-out)**: For performer endpoints configured as `mode=persistent`, FR-002 through FR-005 MUST be no-ops. Persistent endpoints have no per-job container to reconcile; the in-flight guard (FR-001) is sufficient on its own. The reconciliation pass MUST skip any session whose resolved service is persistent and MUST log this as a structured event so the operator can audit which sessions were skipped and why.

- **FR-012 (Docker-down failure mode)**: If the reconciliation pass cannot reach the Docker daemon (socket missing, daemon down, permission denied), coordinare MUST refuse to dispatch any ephemeral performer and MUST emit a `daemon.reconciliation_pass_aborted_docker_unreachable` error event. It MUST NOT fall back to the pre-076 behaviour of dispatching without reconciling. The daemon MUST continue serving the dashboard and health endpoint so the operator can diagnose. Recovery is operator-driven: once Docker is reachable, restart coordinare.

- **FR-013 (observability)**: The structured logs from FR-001, FR-002, FR-004, FR-005, and FR-008 MUST be sufficient for an operator to reconstruct, after the fact, exactly what happened during any reconciliation pass — which sessions were adopted, which were reaped, which dispatched fresh, which orphans were swept. A regression test under `tests/unit/` MUST capture a synthetic reconciliation pass and assert each branch produces its documented log event.

- **FR-014 (regression test for today's incident)**: A regression test MUST simulate today's specific failure mode: persisted snapshot showing card `IN_PROGRESS` with a session id, no entry in `_active_jobs`, a still-running container labelled with that session id. The test MUST assert that after one reconciliation pass the container is either adopted (single `_active_jobs` entry, no new dispatch) or stopped+replaced (single new `_active_jobs` entry, prior container is gone) — never two containers, never zero.

#### User Story 2 — PR-Tracking After Successful Turns

- **FR-015 (PR fields update on success)**: When `monitor_performer` processes a successful performer turn whose output includes a `pr_url`, `pr_node_id`, `pr_number`, `branch_name`, or `head_sha`, every populated field MUST be written through to `state.active_card` AND `state.active_sessions[card_id]` AND the next persisted snapshot before the orchestrator advances to the next stage. No path may set a new `performer_stage` while the prior turn's PR identifiers are still stale.

- **FR-016 (snapshot write barrier on success)**: The state changes from FR-015 MUST be flushed to the persisted snapshot synchronously within the same poll cycle that processes the success. A daemon restart immediately after a successful turn MUST find the new PR identifiers on the card, not the prior ones.

- **FR-017 (success-result schema is strict)**: The performer→orchestrator success-result contract MUST require explicit declaration of any new PR/branch/head produced during the turn. A success result that names new artefacts but provides no identifiers MUST be treated as a malformed result (PARTIAL_PROGRESS or BLOCKED, not DONE), with a documented log event. The orchestrator MUST NOT silently accept "I succeeded but I won't tell you what I produced."

#### User Story 3 — Lifecycle Advancement on Terminal Outcomes

- **FR-018 (DONE advances stage)**: A performer turn that returns DONE with valid artefact identifiers (per FR-017) MUST cause `state.performer_stage` to advance to the next entry in `state.lifecycle_sequence` within the same poll cycle. If `lifecycle_sequence` is exhausted, the orchestrator MUST move the card to its terminal board column (IN_REVIEW for review-ending lifecycles, DONE for closer-ending lifecycles). No success outcome MAY leave `performer_stage` unchanged for the same stage.

- **FR-019 (idle-timeout is recoverable failure, not silent abandonment)**: When a performer container reports `claude code reader idle timeout` (or any equivalent stall/timeout signal), the orchestrator MUST treat the turn as a recoverable failure and take exactly one of the following recorded actions, governed by a per-`(card_id, performer_stage)` retry counter scoped to a rolling 24-hour window (default budget: **2 retries**, configurable): (a) if the counter is below the budget, **re-dispatch the same stage** with the counter incremented and a structured `idle_timeout_retry` log event; (b) if the counter has reached the budget, **move the card to BLOCKED** with a `card_blocked` notification whose body cites the stall pattern and the model in use, AND emit a `card_blocked` slack event so the operator can intervene; (c) at operator's discretion via a future override mechanism (out of scope for 076; placeholder only — implementations MAY add a `# TODO(future-spec)` marker at the code site but MUST NOT implement the override now), the orchestrator MAY transition the card back to TODO instead of BLOCKED, at which point the counter resets. It MUST NOT result in `phase=idle` with `active_card` still pinned and `active_sessions={}`. The retry counter MUST persist across daemon restarts so that a restart-loop cannot launder away the counter and create an unbounded retry sequence.

- **FR-020 (no half-state)**: The combination `state.active_card != None AND state.active_sessions[active_card.id] is missing AND state.performer_stage is None AND state.phase in {None, "idle"}` is a forbidden state. Equivalently: setting `state.active_card` to a non-None value MUST imply that within the same poll cycle either `state.active_sessions[active_card.id]` exists OR a dispatch is queued for that card; the reverse MUST be unreachable through normal graph operation. A graph-level invariant check MUST run at the end of every poll cycle and, if the forbidden combination is detected, MUST log `daemon.wedged_state_detected` and take recovery action. The **default** recovery action is to **release the pin** (set `state.active_card = None`); the next poll cycle's normal eligibility filter will re-pick the card from the board if (and only if) its board Status makes it eligible. Aligns with the "board is source of truth" assumption shared with FR-025. An operator-configurable override MAY substitute "transition to BLOCKED" for cards that have already wedged more than N times in a rolling window (default N=3), but MUST NOT substitute "immediate re-dispatch on the same stage" — that path historically produces the duplicate dispatch this spec is closing. Every wedge resolution MUST emit a structured `wedge_resolution` field naming the chosen branch (`released` / `blocked` / `op_override:<reason>`).

- **FR-021 (terminal-outcome dispatch event)**: For every terminal performer outcome (DONE/PARTIAL_PROGRESS/BLOCKED/idle-timeout/transport-error), the orchestrator MUST emit a structured log event including the outcome, the card id, the performer stage, the resulting next-stage decision, and the new PR head SHA (if any). A grep for these events MUST be sufficient to reconstruct the lifecycle progression of any card.

#### User Story 4 — One Card, One PR

- **FR-022 (canonical branch per card)**: The dispatch path MUST resolve a canonical branch name for each card before launching a performer turn. The canonical name format is `coordinare/<card_node_id>/<title-slug>` where `<title-slug>` is derived from the card's `title` field by: (a) lowercasing; (b) replacing every run of non-`[a-z0-9]` characters with a single `-`; (c) trimming leading/trailing `-`; (d) truncating to a maximum of 60 characters (cut on a `-` boundary if possible). The slug is computed **once** from the card title and reused for every lifecycle stage of the same card. The same `(card_node_id, title)` MUST produce byte-identical slugs across daemon restarts and across dispatches. Resolution preference: (1) reuse the branch of the card's existing open PR if exactly one exists AND its branch matches the canonical pattern; (2) compute the canonical name from card id + title; (3) if the existing PR's branch does NOT match the canonical pattern (e.g., legacy branch from before this spec), the orchestrator MUST flag the divergence (per FR-024) and refuse to dispatch until resolved. Operator overrides require an explicit abandonment record (mechanism out of scope here).

- **FR-023 (performer receives the branch as input)**: The performer's input contract MUST include the canonical branch name from FR-022. The performer MUST be instructed (and, where feasible, mechanically constrained) to push commits to that branch and to update — not replace — the card's open PR. Performers that produce a PR on a different branch MUST have their turn rejected as a contract violation, and the orchestrator MUST close the spurious PR with a structured comment.

- **FR-024 (multi-PR detection)**: The orchestrator MUST run a multi-PR detection check for a card at exactly these three trigger points, NOT on every poll cycle: (1) **immediately before** every `dispatch_performer` call for that card; (2) **once per card** during the restart-time reconciliation pass (FR-002); (3) **opportunistically** when a GitHub webhook event arrives indicating a PR was opened, closed, or had its head branch changed on the same repo, scoped to the affected card. The check itself queries GitHub for ALL open PRs whose head branch matches `coordinare/<card_node_id>/*` (the canonical pattern prefix from FR-022). If more than one is found, the orchestrator MUST: (a) flag the divergence in a structured `daemon.multi_pr_divergence_detected` log event AND surface it on the dashboard (per FR-027); (b) refuse the imminent dispatch — for trigger (1), the dispatch call MUST short-circuit with a `dispatch_performer.multi_pr_divergence_refused` event; for trigger (2)/(3), the card MUST transition to BLOCKED until resolved; (c) emit a `card_blocked` notification naming both PR numbers. Resolution mechanism (which PR survives, what happens to the loser) is out of scope; only detection and dispatch-refusal are mandated here. The check MUST NOT be added to every poll cycle, and MUST NOT add measurable GitHub API load to symphonies that have no in-flight cards.

#### User Story 5 — Board ↔ State Reconciliation Each Cycle

- **FR-025 (per-cycle reconciliation)**: At the start of every poll cycle, for every symphony, after the board snapshot is fetched but before any dispatch decision is made, coordinare MUST compare `state.active_card.status` with the board's current Status for that card. If they disagree, coordinare MUST take exactly one of: (a) re-acquire the card (issue the move that restores agreement, e.g., move TODO→IN_PROGRESS if local state says IN_PROGRESS); (b) release the pin (`active_card=None`); (c) transition to BLOCKED. The choice MUST be logged with a structured `daemon.board_state_reconciled` event including the local view, the board view, and the chosen action.

- **FR-027 (dashboard surfaces divergence)**: The dashboard MUST render both the board status and the local `active_card` view for each symphony's active card. When they disagree, the divergence MUST be visually prominent (warning colour, "DIVERGED" label, or equivalent) so the operator can audit without consulting logs.

### Key Entities *(include if feature involves data)*

- **Performer Container**: A Docker workload that runs one performer turn. Carries labels identifying its coordinare session, card, stage, and the daemon instance that launched it. Its lifecycle is normally owned by coordinare but it survives the daemon process — the reconciliation pass is the mechanism by which a new daemon process re-establishes ownership.

- **In-Flight Registry (`_active_jobs`)**: The in-process map from session id to active container client. Today this is the source of truth for "is this session live?"; this spec extends it so that it can be populated from external state (Docker labels + persisted snapshot) on startup, not just from in-process dispatches.

- **Persisted Session Snapshot**: The on-disk record of a card's session state, including `agent_dispatch.session_id`, `phase`, and `performer_stage`. The reconciliation pass treats this as the authoritative description of intent ("the prior daemon believed this card had a performer running") and uses Docker as the source of truth for reality.

- **Reconciliation Pass**: A startup-time procedure that walks every in-flight card in the persisted snapshot, enumerates the host's performer containers, and produces a per-card decision: adopt, reap-and-replace, or fresh-dispatch. The pass is the new boundary between "what the snapshot says" and "what is actually running."

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001 (no duplicate containers)**: In 100 consecutive forced restarts of coordinare with at least one card `IN_PROGRESS` and a live performer container, the post-restart steady state MUST contain exactly one container per `(card_id, performer_stage)` tuple — zero failures over the 100-trial sample.

- **SC-002 (reconciliation latency)**: The reconciliation pass MUST complete within 30 seconds of coordinare process startup at the 95th percentile, across snapshots containing up to the configured `max_concurrent_cards` value (currently up to 5 active cards). A snapshot with zero in-flight cards MUST add no more than 500 ms to startup time.

- **SC-003 (re-adoption preserves work)**: When the reconciliation pass adopts a container instead of replacing it, the implementer turn that was already in flight MUST continue without losing its progress — the next user-visible commit on the PR branch MUST originate from the same container that was running before the restart, not from a fresh implementer that lost the prior turn's context.

- **SC-004 (notification correctness)**: Across the 100-trial sample in SC-001, the number of `card_dispatched` Slack notifications MUST equal the number of genuine new dispatches (reaps that produced replacements, plus first-time dispatches), and MUST NOT increase by 1 per restart. Operators observing a single card through a restart MUST see at most one new `card_dispatched` event, and zero events when the container was simply re-adopted.

- **SC-005 (no token waste)**: Across the 100-trial sample in SC-001, the cumulative LLM-token cost attributable to abandoned orphan containers (containers that coordinare dispatched but later lost track of) MUST be zero. Either the original container is re-adopted and its tokens count toward the legitimate turn, or it is reaped before being replaced and its tokens are bounded by the reap-budget window.

- **SC-006 (no regression in dispatch success)**: The first-attempt dispatch success rate for cards that have no prior in-flight container MUST be unchanged by this work — within 1 percentage point of the pre-076 baseline. The reconciliation pass MUST NOT introduce a new class of dispatch failures for fresh cards.

- **SC-007 (observability completeness)**: An operator who runs `grep daemon.reconciliation_pass /path/to/coordinare.log` after any restart MUST be able to determine, without consulting code or external state, exactly which cards were in flight at restart, what the reconciliation decision was for each (adopt / reap+replace / fresh-dispatch / skipped-persistent), and how long the pass took.

- **SC-008 (no forgotten successful turns)**: Across 100 implementer turns that report DONE with a new PR, the resulting `state.active_card.pr_url` matches the new PR in 100% of cases. Zero stale-PR-pinned states are produced.

- **SC-009 (no post-success duplicate dispatch)**: Across 100 implementer turns that report DONE, the very next performer dispatch for that card is for the next stage in `lifecycle_sequence`, not the same stage. Zero same-stage re-dispatches happen on the success path.

- **SC-010 (no silent abandonment on stall)**: Across 100 simulated idle-timeout outcomes, every single one results in an explicit recorded transition (retry / blocked / requeue). Zero cards end up in the wedged state (`active_card` pinned + empty sessions + `phase=idle`).

- **SC-011 (one card, one PR)**: Across 100 implementer turns on cards that already have an open PR, the resulting PR count for the card is exactly one (the existing one, updated) in 100% of cases. Zero new PRs are opened that compete with an existing open PR for the same card without an explicit abandonment record.

- **SC-012 (board ↔ state divergence resolved in one cycle)**: When the project board's Status field for a card is changed externally (operator move, automation) while coordinare's `active_card` is still pinned, the next poll cycle resolves the divergence within its normal cycle budget (default 30 s). No card is ever observed in a divergent state across two consecutive poll cycles.

- **SC-013 (dashboard surfaces wedged state)**: When any of the failure modes covered by User Stories 2–5 occur, the dashboard's symphony view surfaces the issue (banner, warning colour, or equivalent) within one SSE broadcast of the underlying state being written. Operators do not need to grep logs to discover a wedged card.

## Out of Scope

- Changes to the persona system, performer container image, or LLM model selection.
- Changes to the implementer's relay/PARTIAL_PROGRESS contract (spec 072) beyond ensuring its handoff path participates in the new in-flight invariant.
- Changes to the 075 implementer CI gate beyond confirming the gate continues to evaluate against the correct single head SHA after this fix.
- Persistent storage of `_active_jobs` between daemon runs (rejected — Docker labels plus reconciliation is sufficient and avoids a new on-disk-state surface).
- Cross-host or distributed coordinare coordination — this spec assumes one coordinare process per host and one Docker daemon per host.
- Automatic detection of "performer is stuck in an infinite loop" — that is a separate concern from "two performers are running concurrently."

## Assumptions

- The ephemeral performer transport is Docker; the reconciliation pass uses the Docker CLI / API to enumerate and stop containers. If a future transport (e.g., Kubernetes job, remote subprocess) becomes the default, the reconciliation pass will need a per-transport implementation. This spec covers the Docker case only.
- The persisted snapshot is authoritative for "what the prior daemon believed it dispatched"; Docker is authoritative for "what is actually running." Where the two disagree, Docker wins for liveness, snapshot wins for intent.
- Container labels added in FR-009 are available immediately on `docker run` (Docker has supported `--label` since 1.6). No daemon-side label propagation delay is anticipated.
- The 30-second reconciliation budget (SC-002) is generous; in practice it should complete in under 5 seconds for typical workloads. The 30-second bound exists to handle edge cases (a hung Docker daemon, many lingering containers).
- "At most one live container per `(card_id, performer_stage)`" is the correct invariant. If a future spec introduces deliberately-parallel performer turns (e.g., A/B'ing two implementer strategies on the same card), this invariant will need to be re-examined; the current spec assumes that is not happening.
