"""138 T049: WCAG 2.1 AA contrast gate over the feed's .ev-* colour pairings.

Pure arithmetic against the CSS in _DASHBOARD_HTML — no browser, no new
dependency. Fails on any future token edit that regresses contrast
(Constitution Gate 7). Where a pairing fails, fix the *token value*, never a
local override (Principle III).
"""
from __future__ import annotations

import re

import pytest

from coordinare.dashboard import _DASHBOARD_HTML

MIN_RATIO = 4.5


def _tokens() -> dict[str, str]:
    """Every --color-* custom property, with one level of var() indirection."""
    raw = dict(re.findall(r"(--color-[\w-]+)\s*:\s*([^;]+);", _DASHBOARD_HTML))
    resolved: dict[str, str] = {}
    for name, value in raw.items():
        value = value.strip()
        alias = re.fullmatch(r"var\((--color-[\w-]+)\)", value)
        if alias:
            value = raw.get(alias.group(1), "").strip()
        if value.startswith("#"):
            resolved[name] = value
    return resolved


def _ev_pairings() -> dict[str, tuple[str, str]]:
    """class name -> (background token, foreground token) for every .ev-*/.ov-* rule.

    429: the session view's observer-verdict chips (.ov-*) join the feed's
    .ev-* chips in the same gate — a new UI element gets no contrast amnesty.
    """
    pairs: dict[str, tuple[str, str]] = {}
    for cls, body in re.findall(r"\.((?:ev|ov)-[\w-]+)\s*\{([^}]*)\}", _DASHBOARD_HTML):
        bg = re.search(r"background:\s*var\((--color-[\w-]+)\)", body)
        fg = re.search(r"color:\s*var\((--color-[\w-]+)\)", body)
        if bg and fg:
            pairs[cls] = (bg.group(1), fg.group(1))
    return pairs


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_contrast_helper_matches_known_values() -> None:
    """Guard the arithmetic itself — black on white is exactly 21:1."""
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("#777777", "#777777") == pytest.approx(1.0, abs=0.01)


def test_every_ev_class_is_parsed() -> None:
    """A rule the parser cannot read is a rule this gate silently skips."""
    pairings = _ev_pairings()
    # Only rules that declare BOTH a background and a colour are in this
    # gate's scope — single-property rules (.ov-row, .ov-list, ...) have no
    # pairing to check.
    declared = {
        cls
        for cls, body in re.findall(
            r"\.((?:ev|ov)-[\w-]+)\s*\{([^}]*)\}", _DASHBOARD_HTML,
        )
        if "background:" in body and "color:" in body
    }
    assert declared == set(pairings), f"unparsed .ev-*/.ov-* rules: {declared - set(pairings)}"
    # The severity classes 138 adds must be among them.
    assert {"ev-quiet", "ev-stall", "ev-stuck"} <= set(pairings)
    # 429: the observer chips are in scope of the same gate.
    assert {"ev-observer_verdict", "ov-continue", "ov-kill"} <= set(pairings)


@pytest.mark.parametrize("cls", sorted(_ev_pairings()))
def test_ev_class_meets_wcag_aa(cls: str) -> None:
    tokens = _tokens()
    bg_token, fg_token = _ev_pairings()[cls]
    assert bg_token in tokens, f"{cls}: unresolved background token {bg_token}"
    assert fg_token in tokens, f"{cls}: unresolved foreground token {fg_token}"
    ratio = contrast_ratio(tokens[bg_token], tokens[fg_token])
    assert ratio >= MIN_RATIO, (
        f".{cls} is {ratio:.2f}:1 ({fg_token} on {bg_token}) — needs {MIN_RATIO}:1. "
        "Fix the token value, not with a local override."
    )
