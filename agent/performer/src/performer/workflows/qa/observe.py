"""Step 4: compare before/after DOM observations (spec 164 FR-011).

HISTORY. This module once pooled repeated MODEL descriptions of a screenshot.
That path was superseded when observation moved to reading the DOM directly --
exact labels, no voting -- and the round-two review found the pooling code,
its persona and its schemas defined but never invoked. Dead code was removed
(Constitution Principle I); the lesson it encoded is kept below in _diff_key.

WHY THIS EXISTS IN THIS SHAPE — measured during design:

Three description passes over the same screenshot saw the same new dropdown and
named it "workspace", "workspace / acme hq", and "acme hq". Pooling required two
passes to agree on (kind, label), so it kept NONE of them. The element that
constituted the entire feature under test vanished from the description, and the
"after" became byte-identical to the "before" — a healthy change reported as no
change at all.

Two corrections follow from that, and both are structural rather than textual:

1. The pooling key is (kind, position). Never a free-text label.
2. The model is not asked for labels at all. Labels come from the DOM, which
   knows them exactly. Asking a model for text that is available exactly, and
   then voting on the answer, is both unreliable and unnecessary.
"""
from __future__ import annotations

from performer.workflows.models import Observation

def _diff_key(observation: Observation) -> tuple[str, int, str]:
    """The identity of an element for before/after comparison.

    Includes the label, unlike the POOLING key. The two operations differ in
    where their data comes from:

    * The former pooling of repeated MODEL descriptions (removed) could not use
      labels: the model was never allowed to supply one, so (kind, position)
      was everything available.
    * Diffing compares two DOM reads, where labels are exact and deterministic.

    Applying the pooling rule here was a real false pass: a form's password
    field replaced by a workspace field at the same position renders as
    text_input in both, so the delta came out empty and QA passed a change that
    deleted the password field.

    Normalised for case and surrounding whitespace, since DOM text varies
    harmlessly between renders and that is not a regression.
    """
    label = (observation.label or "").strip().lower()
    # A label is a stable identity across insertion; a position is not. Adding a
    # field shifts everything below it, and a position-bearing key reports those
    # untouched elements as removed-and-added -- a spurious regression on a good
    # change. Position only breaks ties between unlabelled elements of one kind.
    if label:
        return (observation.kind, -1, label)
    return (observation.kind, observation.position, "")


def diff_observations(
    before: list[Observation], after: list[Observation],
) -> tuple[list[Observation], list[Observation]]:
    """Return (added, removed) by element identity.

    ``removed`` is the regression signal: something present before the change
    and absent after it, which a single screenshot with no baseline cannot see.
    """
    before_keys = {_diff_key(o) for o in before}
    after_keys = {_diff_key(o) for o in after}
    added = [o for o in after if _diff_key(o) not in before_keys]
    removed = [o for o in before if _diff_key(o) not in after_keys]
    return added, removed
