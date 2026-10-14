"""One rule for turning an id into a single safe folder name (#139 review, item 7).

The transcript writer names its folders with ``sanitize_segment``, and the token
ledger reads those folders back by name. Both must use this one function, or a
symphony's spend is keyed differently from the folder its runs were written to.
"""
from __future__ import annotations

import hashlib
import re

# Folder name used when a run's symphony or card is unknown (for example a
# status poll made without a transcript target). The file is still written.
UNATTRIBUTED = "unattributed"

# The folder the conductor's own model calls are written under (131). Every folder
# sanitize_segment() returns is stripped of leading underscores, so no symphony or card
# folder can ever be this name, in any letter case. is_conductor_folder() is the only
# place that admits it.
CONDUCTOR_FOLDER = "_conductor"


def is_conductor_folder(name: str) -> bool:
    """True only for the conductor's own folder. It is never a symphony's folder."""
    return name == CONDUCTOR_FOLDER

_SEGMENT_INVALID = re.compile(r"[^A-Za-z0-9._-]+")
_SEGMENT_MAX_LEN = 120


def sanitize_segment(value: str) -> str:
    """Make a symphony, card or run id safe as one path segment.

    Anything outside ``[A-Za-z0-9._-]`` becomes ``_``, leading and trailing dots
    and underscores are stripped (so ``..`` cannot survive), and the result is
    length-capped. An empty result becomes ``UNATTRIBUTED``.
    """
    stripped = value.strip()
    cleaned = _SEGMENT_INVALID.sub("_", stripped)[:_SEGMENT_MAX_LEN].strip("._")
    if not cleaned:
        return UNATTRIBUTED
    if cleaned != stripped:
        # Two different ids can clean to the same text ("a/b" and "a_b"). A short
        # digest of the original keeps them apart (review item 8).
        digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        return f"{cleaned[: _SEGMENT_MAX_LEN - 9].rstrip('._')}-{digest}"
    return cleaned
