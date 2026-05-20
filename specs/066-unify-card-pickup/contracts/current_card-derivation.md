# Contract: `current_card` Derivation

**Spec**: [../spec.md](../spec.md) — FR-010, FR-011

## Purpose

`state["current_card"]` is a derived mirror of
`state["active_sessions"][state["active_card_id"]]["current_card"]`.
This contract defines how and where the mirror is produced. It is the
load-bearing rule for the read-site-minimisation strategy in
[../research.md](../research.md) §2.

## Function

```python
def _rederive_current_card(state: CoordinareState) -> None:
    """Re-derive the top-level current_card mirror from active_sessions.

    Single mutation site for state["current_card"]. Idempotent: calling
    twice in sequence with no other mutation produces the same state.

    Post-conditions:
      - state["current_card"] is None xor state["active_card_id"] is None
      - When non-None, state["current_card"] is the same dict reference
        as state["active_sessions"][state["active_card_id"]]["current_card"]
    """
    sessions = state.get("active_sessions") or {}
    active_id = state.get("active_card_id")
    if active_id and active_id in sessions:
        state["current_card"] = sessions[active_id].get("current_card")
    else:
        state["current_card"] = None
```

## Invocation Points

The function MUST be invoked exactly at these sites:

1. **End of `check_board` node**, after session-set mutations are
   complete and `active_card_id` is set. This is the primary site.
2. **End of each per-session graph step** inside
   `_invoke_multi_session`, so downstream nodes in subsequent steps
   see a consistent mirror. The per-session loop sets
   `active_card_id` to the session's card before calling the inner
   graph; the re-derivation at the end of the step ensures the
   mirror tracks any mutations made by that step.
3. **End of `daemon._restore_from_snapshot`**, after v1 → v2
   synthesis (if any) is complete.

No other site may mutate `state["current_card"]`. Sites that
previously did so MUST be migrated to mutate the session entry
instead; the next scheduled re-derivation propagates the change to
the mirror.

## Reference identity

The mirror SHOULD share dict identity with the session entry's
`current_card`. Code that mutates the dict (e.g.
`state["current_card"]["status"] = "..."`) therefore continues to
work by accident — the mutation lands on the session-owned dict too.
This is by design for the migration: read-sites and most write-sites
do not have to change. The constitutional rule we enforce is on
*replacing* the reference (`state["current_card"] = {...}`), not on
mutating its fields.

## Anti-patterns

The following patterns MUST be removed during the 066 implementation:

```python
# BAD — replaces the reference outside the derivation function
state["current_card"] = some_new_card

# BAD — sets to None outside the derivation function
state["current_card"] = None

# OK — mutates the shared dict; both mirror and session see it
state["current_card"]["status"] = "IN_REVIEW"
```

## Test invariant

After every state mutation in a per-session graph step, the
following holds (see [../data-model.md](../data-model.md) I3):

```python
assert state.get("current_card") == (
    state.get("active_sessions", {})
    .get(state.get("active_card_id"), {})
    .get("current_card")
)
```

A pytest fixture in `tests/unit/graph/nodes/conftest.py` (new) MUST
assert this at the end of every test that exercises a per-session
step. The invariant is the contract under test.
