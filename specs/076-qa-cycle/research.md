# Phase 0 Research — QA Cycle 076

**Spec:** [spec.md](./spec.md) | **Plan:** [plan.md](./plan.md)

Resolves every NEEDS CLARIFICATION-equivalent unknown from the Technical Context. All five clarifications from `/speckit.clarify` (Q1–Q5) are referenced where they touch a research item.

---

## R-01 — Docker labelling: insertion point & lookup mechanism

**Decision:** Extend `coordinare.services.performer_lifecycle.start_ephemeral(config, *, extra_labels: dict[str, str] | None = None)` to accept additional labels. The caller (`http_performer_service.dispatch_card`) supplies the 4 new labels (`coordinare.session_id`, `coordinare.card_id`, `coordinare.performer_stage`, `coordinare.daemon_started_at`) plus a `coordinare.spec_version=076` marker. Existing `coordinare.performer.id` label stays. Lookup uses `docker ps --filter label=coordinare.session_id=<sid> --format '{{.ID}}'` for a specific session, and `docker ps --filter label=coordinare.spec_version=076` for the broad sweep.

**Rationale:**
- `start_ephemeral` already calls `docker run -d --rm --label coordinare.performer.id=…` (verified at `src/coordinare/services/performer_lifecycle.py:99`); adding more `--label` flags is the smallest possible patch.
- No Docker SDK dependency needed — coordinare already shells out to `docker run` / `docker logs` / `docker stop`.
- Labels are queryable in O(1) per Docker API call via `--filter`; no `docker inspect` parse loop needed.

**Alternatives considered:**
- *Docker SDK (`docker` PyPI package)* — adds a 4 MB dependency and a new error surface for an existing subprocess pattern. Rejected.
- *Encoding all metadata in container name* — limits to 1 dimension; labels are k/v and richer.

---

## R-02 — `_active_jobs` lifecycle and re-adoption shape

**Decision:** The re-adoption code path constructs a fresh `_EphemeralJob(container_id=found_cid, endpoint=resolved_url, client=PerformerHTTPClient(resolved_url, auth_token=…))` and inserts it into `self._active_jobs[session_id]`. `endpoint` is computed by `docker port <cid> 8088` (the same path `start_ephemeral` uses to resolve the host port today).

**Rationale:**
- `_EphemeralJob` is a flat dataclass at `http_performer_service.py:84`; its 3 fields are `container_id`, `endpoint`, `client`. Construction from external state is straightforward.
- The performer container's job-runner is itself stateful: it remembers job IDs across reconnects. A fresh `PerformerHTTPClient` against the same endpoint can resume monitoring without disrupting in-flight work.

**Alternatives considered:**
- *Persist `_active_jobs` to disk between daemon runs* — rejected in spec's Out of Scope; introduces a second source of truth that can disagree with Docker.
- *Always reap, never re-adopt* — wastes work; the in-flight LLM call inside the container would die. The "re-adopt" branch is what makes daemon restarts cheap.

---

## R-03 — Snapshot schema migration v6 → v7

**Decision:** Bump `CURRENT_SCHEMA_VERSION` in `state_store.py` from 6 to 7. The v7 PersistedSession adds:
- `idle_timeout_retry_count: int = 0` (FR-019)
- `idle_timeout_window_started_at: datetime | None = None` (FR-019)
- `pr_artefacts_recorded_at: datetime | None = None` (FR-016 audit timestamp)
- `multi_pr_divergence: dict | None = None` (FR-024 surfaced record)

`MIN_SUPPORTED_SCHEMA_VERSION` stays at 1; v6 snapshots load with the new fields at their defaults — no behaviour change for pre-076 sessions.

**Rationale:**
- The existing `state_store.py:18-31` migration pattern already handles forward-compatible additions (v1–v6 chain). Following the same idiom keeps reviewer-tax low.
- `_SESSION_FIELDS` in `session.py` is the round-trip surface; new fields go there in the same commit.

**Alternatives considered:**
- *Pack the new fields into an existing dict like `bounce_counter`* — overloading the existing field; rejected for clarity.
- *Separate file (`retry_counter.json`)* — second source of truth; rejected.

---

## R-04 — GitHub API budget for multi-PR detection

**Decision:** The multi-PR detection check (FR-024) issues one GraphQL query per trigger: `gh api graphql -f query='{repository(...){pullRequests(first:20, states:OPEN, headRefPrefix:"coordinare/<card_node_id>/"){...}}}'`. The triggers are dispatch-time + restart + PR-webhook (per clarification Q4) — typically 1–3 calls per hour in steady state. Well within the GitHub App's 5000-points-per-hour budget.

**Rationale:**
- The existing PrChecksService (`src/coordinare/services/pr_checks_service.py`) already consumes ~5–10 calls per poll cycle for `statusCheckRollup`. The new check is dwarfed by that.
- GraphQL pullRequests query with branch-prefix filter returns ≤20 PRs typically; one query is sufficient.

**Alternatives considered:**
- *Per-cycle check (original FR-024 wording before clarification)* — adds ~10 calls/min on a 5-card symphony; rejected via Q4.
- *REST `GET /repos/:o/:r/pulls?head=<branch>`* — REST has weaker filter expressivity; GraphQL is the right tool here.

---

## R-05 — Idle-timeout signal source from the performer

**Decision:** The performer container's job-runner emits a structured exit status containing `outcome: "idle_timeout"` (alongside the existing `done | partial_progress | blocked`). Coordinare receives this as part of the JSON the job-runner returns when the host polls `GET /jobs/<id>`. No new transport plane needed.

**Rationale:**
- The performer log we observed (`compassionate_meitner` at 22:56:45) shows `[warning] claude code reader idle timeout had_output=False idle_seconds=600.0` followed by `[info] claude code stopped`. This is a clean signal the performer already produces.
- coordinare's `monitor_performer` already parses the job-runner status response; adding a new `outcome` enum value is a 1-line dispatch table change.

**Alternatives considered:**
- *Treat idle-timeout as a transport error* — loses semantic information; conflates "the model gave up" with "the network died." Rejected.

---

## R-06 — Per-card mutex implementation

**Decision:** A module-level `_dispatch_locks: dict[tuple[str, str], asyncio.Lock] = {}` in `src/coordinare/services/dispatch_guard.py`, keyed on `(card_id, performer_stage)`. The lock is acquired at the top of `dispatch_performer` (right after `_apply_pending_override`) and released in a `finally`. Lock entries are not cleaned up — the dict grows monotonically per session but each entry is ~200 bytes; even 10k cards stays under 2 MB.

**Rationale:**
- `asyncio.Lock` is in the stdlib, no new dependency.
- The existing graph nodes already use `asyncio` throughout (`async def dispatch_performer` etc.); adding a lock is idiomatic.
- The mutex IS the synchronisation point for FR-006 — without it, two concurrent graph invocations could both pass the in-flight guard's check before either sets `agent_dispatch`.

**Alternatives considered:**
- *Per-card threading.Lock* — wrong primitive; the graph is async, not threaded.
- *A single global dispatch lock* — serialises unrelated cards across symphonies; rejected as over-restrictive.

---

## R-07 — Wedge invariant placement

**Decision:** Add a `finally:` block at the end of `daemon.py`'s symphony cycle method (`_run_symphony_cycle` or equivalent) that calls `reconciliation.detect_wedged_state(state) -> WedgeResolution | None`. If a wedge is detected, applies the FR-020 default (release the pin) and emits `daemon.wedged_state_detected` + `daemon.wedge_resolution` structured events. The invariant MUST run even if the cycle body raised (hence `finally`).

**Rationale:**
- `daemon.py` is the right surface — it's the per-cycle driver; per-node placement would mean every node has to know about the invariant.
- `finally:` guarantees execution; otherwise an exception in the cycle would leave the state un-checked and a wedge could persist undetected.

**Alternatives considered:**
- *A new langgraph node at the end of the graph* — graph nodes don't run if upstream nodes raise; loses the safety guarantee.
- *A periodic timer task* — extra concurrency surface for no benefit over a `finally`.

---

## R-08 — Canonical branch slug algorithm

**Decision:** The slug function in `dispatch_guard.compute_title_slug(title: str) -> str` does:

```python
def compute_title_slug(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) <= 60:
        return s
    # Truncate at the last `-` boundary at or before 60 chars
    cut = s[:60].rsplit("-", 1)
    return cut[0] if cut[0] else s[:60]
```

Test vectors (will be `tests/contract/test_canonical_branch_contract.py`):
- `"Feature: Time tracking schema and model foundation"` → `"feature-time-tracking-schema-and-model-foundation"`
- `"Fix bug #123 in OAuth/SSO flow"` → `"fix-bug-123-in-oauth-sso-flow"`
- `"…………………………"` (60 dots) → `""` (degenerate, but stable)
- `"A" * 200` → `"a"` (length-1 result; collapses runs of identical chars? No — only non-alphanumerics collapse; `"a"*200` is all alphanumeric so → `"a" * 60`)

**Rationale:**
- Pure function of input, no env dependence → deterministic across processes, hosts, time.
- Truncating at `-` boundaries preserves word boundaries where possible — more readable.
- The 60-char cap is below Git's 250-char branch-name limit by a wide margin, and matches the existing slug style seen on PR #148's branch.

**Alternatives considered:**
- *MD5/SHA of title* — opaque, hard for operators to grep. Rejected.
- *Title-cased camelCase* — branch names are conventionally kebab-case in this repo. Rejected.

---

## R-09 — Existing `check_board._is_stale` interaction with reconciliation

**Decision:** The stale-session detection at `check_board.py:484-498` is reframed (FR-008) to call `reconciliation.handle_potentially_stale_session(state, card_id) -> ReconciliationDecision` instead of unconditionally clearing `agent_dispatch`. The new function attempts re-adoption first, falls back to reap+replace, falls back to clearing `agent_dispatch` for fresh dispatch only as a last resort. The existing `check_board.stale_session_redispatch` log event is replaced with `check_board.stale_session_reconciled` carrying the decision branch.

**Rationale:**
- The existing path is the bug's trigger (Anomaly 5/6); reusing the entry point but changing what it does is less invasive than removing the code and rewriting the call sites.
- Preserves backward compatibility for the path that *does* legitimately need to fresh-dispatch (container gone for real, e.g., killed externally).

**Alternatives considered:**
- *Delete the stale-session detection entirely* — would break the legitimate restart-rehydration path; rejected.
- *Move the detection into reconciliation entirely and remove from check_board* — would couple check_board to reconciliation in a different way without simplifying the call graph; net zero. Rejected.

---

## R-10 — Performance budget validation strategy

**Decision:** A new `tests/perf/test_reconciliation_latency.py` benchmark:
- Creates 5 in-flight session snapshots
- Mocks `docker ps` to return 5 matching containers + 3 orphans
- Mocks the HTTP probe of each container's job-runner with realistic latencies (15–500 ms)
- Asserts `run_startup_reconciliation` completes in ≤30 s wall-clock at p95 across 100 trials, and ≤500 ms when the snapshot is empty.

A second microbenchmark for the in-flight guard:
- Calls `dispatch_guard.check_inflight(state, card_id, stage)` 10 000 times
- Asserts p95 ≤5 ms, p99 ≤10 ms.

**Rationale:**
- These are the two explicit performance budgets in the spec (SC-002 + the 5 ms in-flight guard target). Both are easy to assert with `time.perf_counter()` loops; no external benchmarking tool needed.
- Aligns with Principle IV's regression-prevention requirement: integrate into the same pytest suite, fail the build on regression.

**Alternatives considered:**
- *External benchmarking tool (pytest-benchmark)* — adds dependency; existing pytest patterns are sufficient.

---

## Open items deferred to Phase 2 (task generation)

None — every Phase-0 unknown is resolved. Phase 2 (`/speckit.tasks`) will turn the 27 FRs and 13 SCs into a dependency-ordered task list using the artefacts produced here.
