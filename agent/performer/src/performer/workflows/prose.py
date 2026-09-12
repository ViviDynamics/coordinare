"""Bounds on model-written prose that are not fatal (#383).

A card was lost because the model wrote a long sentence. Measured on the
website symphony in one night: eight "String should have at most 500
characters" violations, three escalating through BACKEND_FORMAT_ERROR and the
bounded retries into a blocked card. The offending field was
``TestObservation.summary`` -- text that exists to be read and that nothing
branches on. The verdict it accompanied, ``is_expected_red``, validated
perfectly every time.

The caps themselves are worth keeping: an unbounded response eats the context
of every step downstream. What was wrong was the failure mode. A bound on prose
should clamp, not raise.

The line this draws is between what a field is FOR:

* **read** -- summaries, reasons, explanations. Clamp them. Losing the tail of
  a sentence costs a little context; raising costs the card.
* **executed or matched** -- ``test_command``, ``start_command``, the
  ``evidence`` strings anchored verbatim against source. These keep
  ``StringConstraints``, because a truncated command is a DIFFERENT command and
  a truncated anchor matches something else, or nothing.

Clamping also removes a trap in the reprompt path. The personas never stated a
budget, so a model that overran had nothing to correct against and produced the
same over-long text on the retry -- which is why eight violations became four
escalations rather than eight recoveries.
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator, StringConstraints

__all__ = ["Prose", "clamp"]


def clamp(limit: int):
    """A validator that trims a string to *limit* characters."""

    def _clamp(value: Any) -> Any:
        if isinstance(value, str) and len(value) > limit:
            return value[:limit]
        return value

    return _clamp


def Prose(limit: int):  # noqa: N802 - reads as a type where it is used
    """A model-written string that is clamped to *limit* rather than rejected.

    For text that is read. Never for a value that is executed or matched --
    see the module docstring.
    """
    return Annotated[str, BeforeValidator(clamp(limit)), StringConstraints(max_length=limit)]
