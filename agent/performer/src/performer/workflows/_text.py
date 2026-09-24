"""Text helpers for spec 166: token normalization and overlap matching.

Spec 166 FR-007: drop questions that match an answered clarification under
case-fold, punctuation strip, and 60 percent token overlap. 417: the overlap
is the symmetric Dice coefficient (a short new question is not swallowed by a
long answered one) and CJK text tokenizes per character, since it has no
whitespace to split on.
"""
from __future__ import annotations

import re

# Unicode punctuation and symbols are stripped by ``[^\w\s]``, which also
# removes CJK punctuation (，。) — \w keeps letters and digits, \s whitespace.
_UNICODE_NOISE = re.compile(r"[^\w\s]+", re.UNICODE)

# Scripts with no whitespace word boundaries: one character, one token.
_CJK_RANGES = (
    ("\u3040", "\u30ff"),  # hiragana + katakana
    ("\u3400", "\u4dbf"),  # CJK ideograph extension A
    ("\u4e00", "\u9fff"),  # CJK ideographs
    ("\uf900", "\ufaff"),  # CJK compatibility ideographs
    ("\uac00", "\ud7af"),  # Hangul syllables
)


def _is_cjk(ch: str) -> bool:
    return any(lo <= ch <= hi for lo, hi in _CJK_RANGES)


def _split_cjk(word: str) -> list[str]:
    chunks: list[str] = []
    buffered = ""
    for ch in word:
        if _is_cjk(ch):
            if buffered:
                chunks.append(buffered)
                buffered = ""
            chunks.append(ch)
        else:
            buffered += ch
    if buffered:
        chunks.append(buffered)
    return chunks


def normalize_tokens(text: str) -> frozenset[str]:
    """Normalize text to a frozenset of tokens.

    Case-fold, strip punctuation (Unicode, not just ASCII), tokenize on
    whitespace; CJK characters become one token each.

    Args:
        text: The text to normalize.

    Returns:
        A frozenset of normalized tokens.
    """
    if not text:
        return frozenset()

    lowered = text.casefold()
    no_punct = _UNICODE_NOISE.sub(" ", lowered)
    tokens: list[str] = []
    for word in no_punct.split():
        tokens.extend(_split_cjk(word))
    return frozenset(tokens)


def token_overlap(a: str, b: str, threshold: float = 0.6) -> bool:
    """Compute token overlap between two strings.

    Tokens are normalized (case-fold, strip punctuation, split on whitespace,
    CJK per character). The overlap is the symmetric Dice coefficient:
    2 * |A intersect B| / (|A| + |B|) — symmetric in its arguments, so a short
    new question is never swallowed by a long answered one (417).

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

    if not tokens_a or not tokens_b:
        return False

    intersection = len(tokens_a & tokens_b)
    overlap = 2 * intersection / (len(tokens_a) + len(tokens_b))
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
