# Contract: Wiki-initialization prerequisite gate

Mirrors the env-bootstrap gate (`dispatch_performer.py:1291-1337`, `services/env_cache.py`, `daemon.py`).

## Service: `WikiInitService` (NEW — `src/coordinare/services/wiki_init.py`)
Seeded in `__main__.py` like `EnvCacheService` (~1009-1017):
```python
_wiki_service = WikiInitService(coordinare_config)
await _wiki_service.initialise(daemon.state["env_cache"], daemon.state.get("symphony_github_services", {}))
daemon.state["wiki_init_service"] = _wiki_service
```
Startup detection runs in `daemon.start()` after board reconcile, before the poll loop (~line 2480).

### `detect(symphony)` → needs_init: bool
`needs_init = not snapshot.wiki_initialized and not _default_branch_has(docs/wiki/README.md)`.

### Completion actions (on the init documenter job)
1. Job success (PR opened by performer) → read back `pr_node_id`.
2. **Auto-merge** (see `benchmark.md` sibling `R8`): poll `github.check_mergeability(pr_node_id)`; require required-CI-green + a trusted-bot approval (`get_pr_reviews` + `classify_reviewer`); then `github.squash_merge(pr_node_id)`.
3. On `merged=True` → `wiki_initialized=True`, `last_wiki_init_succeeded=True`, `wiki_in_flight=False`.
4. On failure/branch-protection → `wiki_attempts += 1`, `last_wiki_init_error` set; if `wiki_attempts >= budget` → `wiki_exhausted=True`; **notify + hold** either way.

## Dispatch-time hold (EDIT `dispatch_performer.py`, mirror env-cache 1291-1337)
```python
_wiki_state = _env_cache_for_sym  # snapshot fields co-located
_is_wiki_init_dispatch = (performer_stage == "documenting" and card.get("doc_mode") == "init")
if not _is_wiki_init_dispatch and performer_stage != "documenting":
    if not _wiki_state.wiki_initialized:
        logger.info("dispatch_performer.wiki_not_initialized",
                    card_id=card_id, performer_stage=performer_stage,
                    detail=wiki_hold_detail(_wiki_state))  # human-readable
        _release_slot_on_error()   # release slot, not a failure
        return state
```
- `documenting` (update) is allowed to run once the wiki exists; the init job is the only dispatch permitted while `not wiki_initialized`.
- Hold releases the pool slot and returns early (no performer launched), exactly like env-cache-not-current.

## Persisted state (see data-model §5)
`EnvCacheStateSnapshot`: `wiki_initialized`, `wiki_attempts`, `wiki_exhausted`, `last_wiki_init_at/succeeded/error`. Transient `EnvCacheState.wiki_in_flight`. Schema v13.

## Notification
`EventType.wiki_init_exhausted` (critical, `dedup_key=f"wiki_init_exhausted:{symphony}"`) on exhaustion or auto-merge-blocked hold.

## Status: foundation landed, gate wiring deferred (adversarial-review outcome)

The whole-PR review (PR #166) found that a wired dispatch-hold gate with **no
wired trigger** is a latent deadlock (enabling it would hold non-documentation
dispatch forever). Rather than ship never-run cross-boundary I/O, the PR now
lands only the **tested foundation**: `WikiInitService` (decision brain +
auto-merge + budget + notify), the schema-v13 wiki-init state (persist/restore
+ JSON-schema contract), and `EventType.wiki_init_exhausted`. The dispatch-hold
gate in `dispatch_performer`, the `__main__` seed, and the
`wiki_init_gate_enabled` / `wiki_init_max_attempts` config flags were
**removed** so nothing can half-enable into a deadlock. Investigation also
showed T026 needs a **performer-side change** (the documenting terminal path
returns `docs_committed` early without opening a PR — see `main.py:2864` — so a
cardless wiki-init must add PR creation) on top of the daemon dispatch. All of
this lands together in T026 as a live-validated follow-up.

## Remaining daemon wiring (T026 — the one live-verified seam)

The `WikiInitService` brain is built + unit-tested (`check_and_trigger`,
`handle_init_result`, `try_auto_merge`, gate/state/notify) and the service is
seeded in `__main__.py` (gated, default OFF). What remains is the per-cycle
dispatch, which depends on **cardless documenting behavior that must create a
branch + open a PR** (env-bootstrap opens no PR, so it isn't a faithful
template) — verify against a live performer before shipping. Exact anchors:

- `daemon.py __init__` ~line 566: add `self._wiki_init_poll_tasks: set = set()`
  next to `_bootstrap_poll_tasks`.
- `daemon.py` per-cycle loop, **after** the env-cache `check_and_trigger` loop
  (ends ~line 2668): for each symphony call
  `wiki_svc.check_and_trigger(sym, ec_state, gh, org, repo, dispatch_fn=partial(self._execute_wiki_init_dispatch, sym))`.
- New `_execute_wiki_init_dispatch(sym)`: mirror `_execute_bootstrap_dispatch`
  — get the `documenting` service, build a documenting dispatch dict with
  `doc_mode="init"` (the `tech_writer` role's own self-hosted backend/model via
  `resolve_performer_dispatch_model("tech_writer")` — never a cloud model),
  `base_branch=<default>`, a fresh `wiki-init/<sanitised>` branch;
  `svc.dispatch_card(...)`; spawn `_poll_wiki_init_completion`.
- New `_poll_wiki_init_completion(...)`: await terminal status, extract
  `pr_node_id`, call `wiki_svc.handle_init_result(...)` (which auto-merges).

**Do not enable `wiki_init_gate_enabled` until this dispatch is wired** —
the gate would hold non-documentation dispatch with nothing to satisfy it.

## Test contract (test-first)
- fresh symphony, no `docs/wiki/README.md` on main → non-documenting dispatch is held; init job dispatched with `doc_mode="init"`.
- `wiki_initialized=True` → no hold; normal dispatch proceeds; no re-init.
- init job fails `budget` times → `wiki_exhausted=True` + one dedup notification; dispatch stays held.
- auto-merge returns `merged=False` (branch protection) → hold + notify (no crash, no infinite loop).
- restart with `wiki_initialized=True` in snapshot → no re-init (marker survives; transient `wiki_in_flight` reset).
