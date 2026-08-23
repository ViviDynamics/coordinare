"""Spec 135 — judge verdict parsing: malformed output is a judge ERROR, never a
False verdict (FR-004 edge case), and quality honors its documented 0-5 range."""

from __future__ import annotations

import pytest

from coordinare.bench.judge import parse_verdict


class TestParseVerdict:
    def test_well_formed_verdict(self) -> None:
        correct, quality, reason = parse_verdict(
            'Sure! {"correct": true, "quality": 4, "reason": "meets rubric"} done'
        )
        assert correct is True and quality == 4 and reason == "meets rubric"

    def test_false_verdict_is_false_not_error(self) -> None:
        correct, _, _ = parse_verdict('{"correct": false, "quality": 1, "reason": "no"}')
        assert correct is False

    def test_missing_correct_field_is_judge_error_not_false(self) -> None:
        correct, quality, reason = parse_verdict('{"quality": 5, "reason": "looks good"}')
        assert correct is None and quality is None
        assert "judge_error" in reason and "correct" in reason

    def test_null_correct_field_is_judge_error(self) -> None:
        correct, _, reason = parse_verdict('{"correct": null, "quality": 3, "reason": "?"}')
        assert correct is None and "judge_error" in reason

    @pytest.mark.parametrize("bad_quality", ["10", "-1", '"high"', "3.5", "true"])
    def test_out_of_range_or_non_int_quality_dropped_to_none(self, bad_quality: str) -> None:
        correct, quality, _ = parse_verdict(
            f'{{"correct": true, "quality": {bad_quality}, "reason": "r"}}'
        )
        assert correct is True and quality is None

    def test_no_json_object_raises_for_caller_to_wrap(self) -> None:
        # make_judge's try/except turns this into (None, None, "judge_error: ...").
        with pytest.raises(ValueError):
            parse_verdict("I cannot grade this.")
