"""Advocate triage (spec 173): the rule that runs before any model call.

Spec 170 established the shape: fail closed before spending a model call. A
billing dispute or a security report must reach a human whether or not the
gateway is up, whether or not the model behaves, and at no token cost.
"""
from __future__ import annotations


def matches_sensitive_keyword(text: str, keywords: list[str]) -> str | None:
    """The first keyword present in *text*, or None.

    Substring matching on lowercased text, matching the behaviour this replaces.
    It over-matches ("refund" inside "refundable"), and that is the correct
    direction to err: the cost of an unnecessary escalation is a human glance,
    and the cost of a miss is an automated reply to a legal complaint.
    """
    haystack = (text or "").lower()
    for keyword in keywords:
        needle = str(keyword or "").strip().lower()
        if needle and needle in haystack:
            return str(keyword)
    return None
