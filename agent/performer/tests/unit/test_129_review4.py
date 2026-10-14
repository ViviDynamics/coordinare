"""Spec 129, fourth review, performer side: one test per finding.

Covers the bounded serving of a batch, the size check that runs before any walk of a
large value, serving an adversarial field at 1 MB, and the single-record bound.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from performer import transcript

MEGABYTE = 1_000_000


@pytest.fixture
def buffer() -> Iterator[transcript.TranscriptBuffer]:
    buf = transcript.TranscriptBuffer("run-r4", max_bytes=500_000_000)
    transcript.set_active_buffer(buf)
    yield buf
    transcript.set_active_buffer(None)


def test_one_slow_record_ends_the_batch_so_completion_is_not_held_up(
    buffer: transcript.TranscriptBuffer,
) -> None:
    """Item 1: a poll does at most its time budget of redaction, and always serves one record."""
    for index in range(20):
        transcript.emit("assistant_text", text=f"record {index}")
    served = buffer.after(0, 20, time_budget_s=0.0)
    assert len(served) == 1, "the first record is always served, the rest wait for the next poll"
    assert [entry["seq"] for entry in served] == [1]
    assert len(buffer.after(1, 20, time_budget_s=10.0)) == 19


def test_a_huge_value_is_withheld_by_its_encoded_size_before_any_walk(
    buffer: transcript.TranscriptBuffer,
) -> None:
    """Item 1: the size is checked on the encoded value; a large structure is not walked."""
    huge = [index for index in range(2_000_000)]
    started = time.perf_counter()
    transcript.emit("tool_result", output_data=huge)
    elapsed = time.perf_counter() - started
    [record] = buffer.after(0, 10)
    assert record["output"] == {"withheld": "value exceeded the per-record size limit"}
    assert elapsed < 2.0


def test_an_adversarial_jwt_field_of_one_megabyte_is_served_quickly(
    buffer: transcript.TranscriptBuffer,
) -> None:
    """Item 1: the field is cut to the field limit before it is served, so the redaction
    work on it is bounded. The unbounded cost was the JWT scan over an ``eyJ`` run."""
    transcript.emit("assistant_text", text="eyJ" * (MEGABYTE // 3))
    started = time.perf_counter()
    [record] = buffer.after(0, 10)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0
    assert len(record["text"]) < transcript.FIELD_LIMIT_CHARS + 200


def test_a_batch_of_adversarial_records_is_served_within_the_budget(
    buffer: transcript.TranscriptBuffer,
) -> None:
    """Item 1: many heavy records in one poll are bounded in time, not only in bytes."""
    for _ in range(40):
        transcript.emit("tool_result", output_data="machine a " * 20_000)
    started = time.perf_counter()
    served = buffer.after(0, 200, time_budget_s=0.5)
    elapsed = time.perf_counter() - started
    assert 1 <= len(served) < 40
    assert elapsed < 3.0
