"""148: the chained command redactor stays linear on adversarial input.

The timing assertion is a ratio: the same input at 1 MB against 100 KB. A linear
pass grows about tenfold, a quadratic one a hundredfold, and the bound sits between
them. A slow CI machine (the coverage job runs several times slower) scales both
sides alike, so the ratio holds where an absolute bound does not.
"""
from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from coordinare.lib.redaction import redact_command_text

SMALL_CHARS = 100_000
LARGE_CHARS = 1_000_000
# Linear growth is 10x; allow 3x for scheduling noise. Quadratic would be 100x.
MAX_GROWTH = 30

# (repeated unit, a secret that must not survive in the output)
ADVERSARIAL = [
    ("curl -u a:b ", "a:b"),
    ("://a:b@", ":b@"),
    ("--password x ", "--password x"),
]


def _best_of(function: Callable[[str], str], text: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        function(text)
        best = min(best, time.perf_counter() - start)
    return best


def _repeated(unit: str, chars: int) -> str:
    return unit * (chars // len(unit))


@pytest.mark.parametrize(("unit", "secret"), ADVERSARIAL)
def test_a_megabyte_of_adversarial_input_scales_linearly(unit: str, secret: str) -> None:
    small = _repeated(unit, SMALL_CHARS)
    large = _repeated(unit, LARGE_CHARS)
    growth = _best_of(redact_command_text, large) / _best_of(redact_command_text, small)
    assert growth < MAX_GROWTH, f"1 MB took {growth:.1f}x the 100 KB time for {unit!r}"


@pytest.mark.parametrize(("unit", "secret"), ADVERSARIAL)
def test_every_repeat_is_redacted_at_a_megabyte(unit: str, secret: str) -> None:
    out = redact_command_text(_repeated(unit, LARGE_CHARS))
    assert secret not in out


def test_the_flood_record_costs_little_per_character() -> None:
    """A 4000 character record, the shape the activity flood test writes."""
    record = "1474 " + "z" * 4000
    per_char = _best_of(redact_command_text, record) / len(record)
    # Generous: a real regression (quadratic, or a new unbounded pass) shows up as orders of magnitude.
    assert per_char < 1e-4
