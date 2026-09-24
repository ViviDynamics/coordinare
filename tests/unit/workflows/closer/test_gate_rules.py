"""Spec 172 FR-005, FR-006, FR-013: every gate rule is a pure function with its own
test. Each test names the mutation that breaks it; the PR records each one applied
in the real tree."""
from __future__ import annotations

from performer.workflows.closer.gate import (
    accept_judgement,
    quote_found,
    run_gate,
    to_resolve,
    verdict,
)
from performer.workflows.closer.models import Classification, Judgement, Thread

from tests.unit.workflows.closer._fakes import comment, thread

ASK = comment("reviewer", "This needs a guard for the empty case.")
REPLY = comment("implementer", "Added the guard in commit abc123; it returns early when the list is empty.", "2026-09-07T11:00:00Z")
CONFIRM = comment("reviewer", "Confirmed, works now", "2026-09-07T12:00:00Z")  # the raiser's sign-off: the quote authority
T1 = Thread.model_validate(thread("t1", ASK, REPLY, CONFIRM))
BY_ID = {"t1": T1}


def _j(**over) -> Judgement:
    base = {"thread_id": "t1", "addressed": True, "quote": "Confirmed, works now", "reason": "", "accepted": False}
    base.update(over)
    return Judgement(**base)


# quote_found: mutation = always true; accept an empty quote
def test_a_quote_must_appear_in_the_threads_own_comments():
    assert quote_found("Added the guard in commit abc123", T1)
    assert quote_found("ADDED THE   GUARD in commit abc123", T1), "case and whitespace fold"
    assert quote_found("This needs a guard", T1), "any comment in the thread counts"
    assert not quote_found("I rewrote the whole module", T1)
    assert not quote_found("", T1), "an empty quote proves nothing"
    assert not quote_found("   ", T1)


# accept_judgement: mutations = accept an unsent thread; skip the quote check
def test_a_judgement_is_accepted_only_for_a_sent_thread_it_can_prove():
    assert accept_judgement(_j(), ["t1"], BY_ID) == (True, None)
    assert accept_judgement(_j(thread_id="ghost"), ["t1"], BY_ID) == (False, "thread_not_sent")
    assert accept_judgement(_j(quote="never said this"), ["t1"], BY_ID) == (False, "quote_not_found")
    assert accept_judgement(_j(addressed=False, quote="", reason="still open"), ["t1"], BY_ID) == (True, None), "a rejection needs no quote"
    assert accept_judgement(_j(), ["t1"], {}) == (False, "quote_not_found"), "an unknown thread cannot be proved"


# verdict: mutation = approve with open threads
def test_the_verdict_is_derived_from_what_stays_open():
    assert verdict([]) == "approved"
    assert verdict(["t1"]) == "changes_requested"


# to_resolve: mutations = include open ids; drop the stale ids
def test_only_stale_and_accepted_addressed_threads_are_resolved():
    classifications = [
        Classification(thread_id="stale1", state="stale", rule="is_stale"),
        Classification(thread_id="open1", state="open", rule="default"),
        Classification(thread_id="t1", state="answered", rule="is_answered"),
        Classification(thread_id="res1", state="resolved", rule="is_resolved"),
    ]
    judgements = [_j(accepted=True), _j(thread_id="open1", accepted=False), _j(thread_id="other", addressed=False, accepted=True)]
    out = to_resolve(classifications, judgements)
    assert [tid for tid, _r in out] == ["stale1", "t1"]
    assert out[0][1].startswith("outdated") and out[1][1].startswith("addressed:")


# run_gate: mutations = keep a duplicate judgement; treat an unjudged answered thread as closed
def test_run_gate_leaves_every_unproved_thread_open():
    classifications = [
        Classification(thread_id="t1", state="answered", rule="is_answered"),
        Classification(thread_id="t2", state="answered", rule="is_answered"),
        Classification(thread_id="open1", state="open", rule="default"),
    ]
    raw = [_j(), _j(quote="also mine")]  # the second is a duplicate for t1
    outcome = run_gate(classifications, raw, ["t1", "t2"], BY_ID)
    assert outcome.verdict == "changes_requested"
    assert outcome.open_thread_ids == ["open1", "t2"], "t2 was never judged, so it stays open"
    assert [j.discard_reason for j in outcome.judgements] == [None, "duplicate_judgement"]
    assert [tid for tid, _r in outcome.resolve] == ["t1"]
    clean = run_gate([Classification(thread_id="t1", state="answered", rule="is_answered")], [_j()], ["t1"], BY_ID)
    assert clean.verdict == "approved" and clean.open_thread_ids == []
