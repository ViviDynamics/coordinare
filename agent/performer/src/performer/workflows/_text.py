"""Text helpers for spec 166: token normalization and overlap matching.

Spec 166 FR-007: drop questions that match an answered clarification under
case-fold, punctuation strip, and 60 percent token overlap.
"""
from __future__ import annotations

import string


def normalize_tokens(text: str) -> frozenset[str]:
    """Normalize text to a frozenset of tokens.

    Case-fold, strip punctuation, tokenize on whitespace.

    Args:
        text: The text to normalize.

    Returns:
        A frozenset of normalized tokens.
    """
    if not text:
        return frozenset()

    lowered = text.casefold()
    no_punct = lowered.translate(str.maketrans("", "", string.punctuation))
    tokens = no_punct.split()
    return frozenset(tokens)


def token_overlap(a: str, b: str, threshold: float = 0.6) -> bool:
    """Compute token overlap between two strings.

    Tokens are normalized (case-fold, strip punctuation, split on whitespace).
    Overlap is: |A intersect B| / min(|A|, |B|).

    Args:
        a: First string.
        b: Second string.
        threshold: Minimum overlap ratio (0.0 to 1.0). Default 0.6 (60%).

    Returns:
        True if overlap >= threshold, False otherwise.
        Returns False if either string has zero tokens.
    """
    tokens_a = normalize_tokens(a)
    tokens_b = normalize_tokens(b)

    min_size = min(len(tokens_a), len(tokens_b))
    if min_size == 0:
        return False

    intersection = tokens_a & tokens_b
    overlap = len(intersection) / min_size
    return overlap >= threshold


def matches_answered(question: str, answered_question: str, threshold: float = 0.6) -> bool:
    """Check if a question matches an answered clarification.

    A match is either:
    1. The questions are equal after normalization, or
    2. They have at least the specified token overlap after normalization.

    Args:
        question: The question being asked.
        answered_question: An answered question from the history.
        threshold: Minimum token overlap ratio. Default 0.6 (60%).

    Returns:
        True if the questions match, False otherwise.
    """
    norm_q = normalize_tokens(question)
    norm_a = normalize_tokens(answered_question)

    if norm_q == norm_a:
        return True

    return token_overlap(question, answered_question, threshold=threshold)


__all__ = ["normalize_tokens", "token_overlap", "matches_answered"]
