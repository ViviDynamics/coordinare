"""Every activity type must appear in BOTH dashboard label maps.

The feed renders each entry twice: a short chip from `AF_LABELS` and a
sentence from `AF_SUMMARIES`. Spec 327 added the `stream_truncated` type but
only to `AF_SUMMARIES`, so live feeds showed a raw uppercased `STREAM_TRUNCATED`
chip next to the generic fallback sentence. Neither map errors on a missing
key -- both silently fall back -- so nothing caught it until a human read the
dashboard and asked.

Parses the two JS object literals out of `dashboard.py`. The maps live in
inline JS, so there is no import to assert against.
"""
from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[3] / "src" / "coordinare" / "dashboard.py"


def _js_object_keys(var_name: str) -> set[str]:
    """Keys of `var <var_name> = { ... };` in the dashboard's inline JS.

    Deliberately anchored on the declaration and terminated at the closing
    brace, rather than scanning the whole file for anything that looks like a
    key -- a loose scan over a 4000-line module with several JS blobs would
    match unrelated text.
    """
    source = DASHBOARD.read_text(encoding="utf-8")
    match = re.search(rf"var {re.escape(var_name)} = \{{(.*?)\n\}};", source, re.DOTALL)
    assert match, f"{var_name} object literal not found in dashboard.py"
    body = match.group(1)
    # Strip // comments so commentary inside the literal is not read as keys.
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"(\w+)\s*:", body))


def test_label_and_summary_maps_cover_the_same_activity_types() -> None:
    labels = _js_object_keys("AF_LABELS")
    summaries = _js_object_keys("AF_SUMMARIES")

    assert len(labels) > 8, f"AF_LABELS parse looks wrong: {labels}"
    assert len(summaries) > 8, f"AF_SUMMARIES parse looks wrong: {summaries}"

    missing_label = sorted(summaries - labels)
    missing_summary = sorted(labels - summaries)
    assert not missing_label, (
        "activity types have a summary but no chip label, so the feed shows a raw "
        f"uppercased type: {missing_label}"
    )
    assert not missing_summary, (
        "activity types have a chip label but no summary, so the feed shows the "
        f"generic 'Activity reported.': {missing_summary}"
    )


def test_stream_truncated_is_covered() -> None:
    """The specific regression: 327's type reached only one of the two maps."""
    assert "stream_truncated" in _js_object_keys("AF_LABELS")
    assert "stream_truncated" in _js_object_keys("AF_SUMMARIES")
