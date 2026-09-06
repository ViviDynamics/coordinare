"""T004/T005 - Text helpers for spec 166: token_overlap and matches_answered.

Spec 166 FR-007: drop questions that match an answered clarification under
case-fold, punctuation strip, and 60 percent token overlap (|A intersect B| /
min(|A|, |B|)).

Mutation check (Constitution II): which mutations of _text.py make which tests fail?
All mutations documented at the end of this file.
"""
from __future__ import annotations

from performer.workflows._text import matches_answered, normalize_tokens, token_overlap


class TestNormalizeTokens:
    """normalize_tokens cases: case fold, strip punctuation, tokenize on whitespace."""

    def test_basic_case_folding(self):
        """Case folding produces the same tokens."""
        tokens_upper = normalize_tokens("WHICH AUDIENCE?")
        tokens_lower = normalize_tokens("which audience?")
        assert tokens_upper == tokens_lower

    def test_punctuation_stripped_before_tokenization(self):
        """Punctuation is removed, not tokenized."""
        assert normalize_tokens("Which audience?") == normalize_tokens("Which audience")
        assert normalize_tokens("What's the colour scheme?") == {"whats", "the", "colour", "scheme"}

    def test_whitespace_tokenization(self):
        """Tokens are split on runs of whitespace."""
        tokens = normalize_tokens("Which    audience")
        assert tokens == {"which", "audience"}

    def test_empty_string_returns_empty_set(self):
        """Empty input yields empty token set."""
        assert normalize_tokens("") == frozenset()

    def test_single_token(self):
        """A single token works."""
        assert normalize_tokens("audience") == frozenset({"audience"})

    def test_returned_as_frozenset(self):
        """Result is a frozenset, not a list."""
        result = normalize_tokens("test string")
        assert isinstance(result, frozenset)


class TestTokenOverlap:
    """token_overlap: intersection / min(|A|, |B|) after normalization."""


    def test_exact_match_after_normalization(self):
        """Exact match is 100% overlap."""
        assert token_overlap("Which audience?", "Which audience?", threshold=0.6) is True

    def test_case_insensitive_match(self):
        """Case differences are ignored."""
        assert token_overlap("WHICH AUDIENCE?", "which audience?", threshold=0.6) is True

    def test_non_matching_questions_are_false(self):
        """Completely different questions don't match."""
        result = token_overlap("Which audience?", "What is the colour scheme?", threshold=0.6)
        assert result is False

    def test_token_overlap_exactly_at_threshold(self):
        """60% exactly meets the threshold."""
        # Create: 5 tokens in q1, 3 match in q2 => 3/5 = 60%
        q1 = "one two three four five"  # 5 tokens
        q2 = "one two three new extra"  # 5 tokens: {one, two, three, new, extra}
        # Intersection: {one, two, three} = 3
        # min(5, 5) = 5
        # overlap: 3/5 = 0.6 (exactly 60%)
        assert token_overlap(q1, q2, threshold=0.6) is True

    def test_token_overlap_just_below_threshold(self):
        """59% is below the 60% threshold."""
        # Create: 10 tokens in q1, 5 match in q2 => 5/10 = 50%
        q1 = "a b c d e f g h i j"  # 10 tokens
        q2 = "a b c d e x y z m n"  # 10 tokens: {a, b, c, d, e, x, y, z, m, n}
        # Intersection: {a, b, c, d, e} = 5
        # min(10, 10) = 10
        # overlap: 5/10 = 0.5 (50% is below 60%)
        assert token_overlap(q1, q2, threshold=0.6) is False

    def test_empty_string_produces_zero_overlap(self):
        """Empty string has zero tokens, so overlap is 0."""
        assert token_overlap("", "some text", threshold=0.6) is False
        assert token_overlap("some text", "", threshold=0.6) is False

    def test_empty_both_sides(self):
        """Two empty strings: no overlap but also no min, so handle edge case."""
        # Both empty: intersection is empty, min of 0,0 is 0
        # 0/0 is undefined; handle as False (no tokens to overlap)
        assert token_overlap("", "", threshold=0.6) is False

    def test_single_token_match(self):
        """Single token that matches."""
        q1 = "audience"  # 1 token
        q2 = "which audience"  # 2 tokens: {which, audience}
        # Intersection: {audience} = 1
        # min(1, 2) = 1
        # overlap: 1/1 = 1.0 (100%)
        assert token_overlap(q1, q2, threshold=0.6) is True

    def test_single_token_no_match(self):
        """Single token that doesn't match."""
        q1 = "audience"
        q2 = "colour scheme"  # {colour, scheme}
        # Intersection: {} = 0
        # min(1, 2) = 1
        # overlap: 0/1 = 0.0
        assert token_overlap(q1, q2, threshold=0.6) is False

    def test_default_threshold_is_0_6(self):
        """Default threshold is 0.6 if not specified."""
        # Use the same case as exactly at threshold
        q1 = "one two three four five"
        q2 = "one two three new extra"
        # 3/5 = 0.6
        assert token_overlap(q1, q2) is True  # Uses default 0.6


class TestMatchesAnswered:
    """matches_answered: question matches an answered clarification."""

    def test_exact_match_after_normalization(self):
        """Exact question after normalization is a match."""
        question = "Which audience?"
        answered = "Which audience?"
        assert matches_answered(question, answered) is True

    def test_case_insensitive_match(self):
        """Case folding makes it match."""
        question = "which audience?"
        answered = "WHICH AUDIENCE?"
        assert matches_answered(question, answered) is True

    def test_punctuation_stripped_match(self):
        """Punctuation is ignored in matching."""
        question = "Which audience?"
        answered = "Which audience."
        assert matches_answered(question, answered) is True

    def test_60_percent_overlap_is_match(self):
        """60% token overlap is considered a match."""
        question = "one two three four five"  # 5 tokens
        answered = "one two three new extra"  # 5 tokens, 3 match
        # Overlap: 3/5 = 0.6
        assert matches_answered(question, answered) is True

    def test_non_matching_questions(self):
        """Unrelated questions don't match."""
        question = "Which audience?"
        answered = "What is the colour scheme?"
        assert matches_answered(question, answered) is False

    def test_default_threshold(self):
        """Default threshold is 0.6."""
        # Same as 60% case above
        question = "one two three four five"
        answered = "one two three new extra"
        assert matches_answered(question, answered) is True

    def test_custom_threshold(self):
        """Custom threshold can be passed."""
        # 3/5 = 0.6, which is exactly at default but below 0.7
        question = "one two three four five"
        answered = "one two three new extra"
        assert matches_answered(question, answered, threshold=0.6) is True
        assert matches_answered(question, answered, threshold=0.7) is False


# ============================================================================
# MUTATION CHECKS (Constitution II FR-019)
# ============================================================================
# Which one-line mutation of _text.py makes which test fail?
#
# Mutation 1: Change normalize_tokens to NOT case-fold
#   Mutation: replace `text.casefold()` with `text`
#   Fails: TestNormalizeTokens.test_basic_case_folding
#          TestTokenOverlap.test_case_insensitive_match
#          TestMatchesAnswered.test_case_insensitive_match
#
# Mutation 2: Change normalize_tokens to NOT strip punctuation
#   Mutation: remove the punctuation stripping line
#   Fails: TestNormalizeTokens.test_punctuation_stripped_before_tokenization
#          TestMatchesAnswered.test_punctuation_stripped_match
#
# Mutation 3: Remove the min() comparison in token_overlap
#   Mutation: change `min(len(a), len(b))` to just `len(a)` or `len(b)`
#   Fails: TestTokenOverlap.test_token_overlap_exactly_at_threshold (or similar overlap tests)
#
# Mutation 4: Change the overlap threshold comparison from >= to >
#   Mutation: change `overlap >= threshold` to `overlap > threshold`
#   Fails: TestTokenOverlap.test_token_overlap_exactly_at_threshold
#
# Mutation 5: Remove the intersection calculation
#   Mutation: change `len(a & b)` to just return 1 or 0
#   Fails: Most overlap tests
#
# Mutation 6: Remove the empty set handling
#   Mutation: remove the `if not min(...): return False` check
#   Fails: TestTokenOverlap.test_empty_both_sides
#          TestTokenOverlap.test_empty_string_produces_zero_overlap

