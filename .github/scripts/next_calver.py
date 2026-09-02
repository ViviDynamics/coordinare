"""The next CalVer for this repository: `YYYY.M.N`.

The same scheme in-house and sibling repos use, so every repo in the org versions the same
way and a chart version is just the release version. Month is unpadded and the
counter runs per month, which makes the result valid SemVer — Helm requires that,
and it is why nothing here maps one version onto another.

Coordinare previously minted `YYYY.MM.DD[.N]`. Those tags still exist and are
deliberately NOT candidates: parsed loosely, `2026.09.01.3` offers a patch of
`01`, and the next mint would collide with a tag that is already pushed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Strict, and the strictness is SemVer's own rule: no leading zeros. Three
# numeric components alone is not enough to exclude the legacy tags -- `2026.09.01`
# has three, and int("09") is 9, so a looser pattern reads it as month 9 patch 1
# and mints 2026.9.2, silently skipping a version. Requiring unpadded components
# rejects exactly the tags that are not this scheme. Optional legacy `v` because
# retagging git does not rename artefacts published under the old name.
_TAG = re.compile(r"^v?(\d{4})\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def next_calver(tags: Iterable[str], *, year: int, month: int) -> str:
    patches = []
    for tag in tags:
        match = _TAG.match((tag or "").strip())
        if not match:
            continue
        tag_year, tag_month, patch = (int(group) for group in match.groups())
        if tag_year == year and tag_month == month:
            patches.append(patch)
    return f"{year}.{month}.{max(patches) + 1 if patches else 0}"


if __name__ == "__main__":
    now = datetime.now(UTC)
    existing = subprocess.run(
        ["git", "tag", "-l"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    sys.stdout.write(next_calver(existing, year=now.year, month=now.month) + "\n")
