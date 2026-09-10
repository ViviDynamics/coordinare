"""329: the watchdog must also catch a loop that PARAPHRASES itself.

#328 made the fingerprint rotation-stable for an exactly periodic cycle. A
second live loop on website #160 evaded it: the model varied a word or two per
repetition ("present the final answer" / "include the complete solution"), so
there was no exact repeating unit, the period test found nothing, and the raw
tail kept rotating. Measured against the running performer, the shipped
fingerprint changed on every poll and the 900s threshold was never reached --
the turn was still burning tokens 8 minutes after its last tool call.

The fixture here is that turn, captured live.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from coordinare.services.progress_fingerprint import (
    LOOP_MARKER,
    NOVELTY_FLOOR,
    progress_fingerprint,
)

FIXTURE = Path(__file__).parent / "fixtures" / "website_160_paraphrase_loop.json"


def _polls() -> dict[str, list[dict]]:
    raw = json.loads(FIXTURE.read_text())
    return {
        name: [{"type": "progress", "is_delta": True, "text": t} for t in texts]
        for name, texts in raw["polls"].items()
    }


def test_captured_live_loop_is_stable_across_polls() -> None:
    """THE regression, on real data: four successive polls of the same dead
    turn must produce ONE fingerprint, so `last_progress_at` stops resetting."""
    fps = {progress_fingerprint(ev) for ev in _polls().values()}
    assert len(fps) == 1, f"fingerprint still rotates on the live loop: {fps}"


def test_captured_live_loop_is_recognised_as_a_loop() -> None:
    """It must be caught by the novelty rule, not by accident."""
    any_poll = next(iter(_polls().values()))
    assert progress_fingerprint(any_poll).endswith(LOOP_MARKER)


def test_tool_activity_counts_as_progress_even_when_text_repeats() -> None:
    """A model can repeat itself while genuinely working (retry loops, polling
    a build). New tool activity must therefore always read as progress."""
    chatter = [{"type": "progress", "is_delta": True, "text": "waiting. "}] * 200
    before = [*chatter, {"type": "tool_use", "text": "bin/rails db:migrate"}]
    after = [*chatter, {"type": "tool_use", "text": "bundle exec rspec spec/models"}]
    assert progress_fingerprint(before) != progress_fingerprint(after)


def test_frozen_tools_plus_repeating_text_is_a_stall() -> None:
    """The live shape: tool events aged out of the window entirely and only
    prose kept arriving."""
    a = [{"type": "progress", "is_delta": True, "text": "Let me now output the final response. I should now present the final answer. "}] * 120
    b = [{"type": "progress", "is_delta": True, "text": "I should now present the final answer. Let me now output the final response. "}] * 120
    assert progress_fingerprint(a) == progress_fingerprint(b)


def test_genuine_work_is_never_flattened() -> None:
    """Real output must keep changing. These are the shapes measured off live
    performers: rspec example names, migration logs, npm output, prose plans --
    all scored 0.55-1.0 novelty, far above the floor."""
    def ev(texts):
        return [{"type": "progress", "is_delta": True, "text": t} for t in texts]

    rspec = ev([f"grants access to an admin {i}\ndenies access to a regular user {i}\n" for i in range(40)])
    migration = ev([f"-- create_table(:table_{i}) -> 0.0{i}s\n" for i in range(40)])
    prose = ev([f"Step {i}: modify the {['model', 'controller', 'view', 'spec', 'migration'][i % 5]} for keyed assignments.\n" for i in range(40)])
    for stream in (rspec, migration, prose):
        fp = progress_fingerprint(stream)
        assert not fp.endswith(LOOP_MARKER), f"genuine work flattened into a stall: {fp[:80]}"
        # and it must still move when new work arrives
        assert progress_fingerprint(stream) != progress_fingerprint(
            [*stream, {"type": "progress", "is_delta": True, "text": "all green, 214 examples, 0 failures\n"}]
        )


def test_short_repetitive_output_is_not_judged() -> None:
    """Below the minimum window there is not enough signal; do not guess."""
    short = [{"type": "progress", "is_delta": True, "text": "retrying. "}] * 3
    assert not progress_fingerprint(short).endswith(LOOP_MARKER)


def test_novelty_floor_sits_between_measured_loops_and_real_output() -> None:
    """Guards the constant itself. Live loops measured 0.017-0.051; the least
    novel genuine sample measured 0.554. The floor must stay between them."""
    assert 0.051 < NOVELTY_FLOOR < 0.554


def test_tool_signature_does_not_churn_as_tools_age_out() -> None:
    """Review finding: a COUNT of tool events falls 5,4,3,2,1,0 as the backend's
    capped window slides, and every decrement read as fresh progress -- so a
    turn that made a few tool calls and then started looping could reset the
    stall timer indefinitely. Only the newest tool event may contribute, and it
    is the last to age out, so a wedged turn changes this at most once."""
    loop = [{"type": "progress", "is_delta": True, "text": "Let me now output the final response. I should now present the final answer. "}] * 200
    tools = [{"type": "tool_use", "text": f"command number {i}"} for i in range(5)]
    stream = [*tools, *loop]
    seen = [progress_fingerprint(stream[drop:]) for drop in range(len(tools) + 1)]
    transitions = sum(1 for a, b in itertools.pairwise(seen) if a != b)
    assert transitions <= 1, f"tool aging churned the fingerprint {transitions} times"
