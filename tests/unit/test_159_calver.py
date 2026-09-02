"""159: one CalVer for the tag, the release, the image and the chart.

Matching other in-house projects. The version is `YYYY.M.N` — month unpadded, counter
per month — which is valid SemVer, so nothing anywhere maps one version onto
another. Coordinare previously minted `YYYY.MM.DD[.N]`, which is neither SemVer
nor what the other repos do.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_MODULE = Path(".github/scripts/next_calver.py")


def _load():
    spec = importlib.util.spec_from_file_location("next_calver", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_first_release_of_a_month_starts_at_zero() -> None:
    assert _load().next_calver([], year=2026, month=9) == "2026.9.0"


def test_it_continues_the_months_counter() -> None:
    tags = ["2026.9.0", "2026.9.1", "2026.9.2"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.3"


def test_it_ignores_other_months_and_years() -> None:
    tags = ["2026.8.41", "2025.9.99", "2026.10.3"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.0"


def test_the_counter_is_numeric_not_lexical() -> None:
    """`9` sorts after `10` as text, and a version that goes backwards is worse
    than one that jumps."""
    tags = ["2026.9.8", "2026.9.9", "2026.9.10"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.11"


def test_coordinares_old_four_component_tags_are_not_candidates() -> None:
    """The tags this repo already has. Parsed loosely, `2026.09.01.3` offers a
    patch of `01` and the next mint collides with history."""
    tags = ["2026.09.01", "2026.09.01.3", "2026.08.30.4", "2026.9.0"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.1"


def test_a_v_prefixed_tag_still_counts() -> None:
    """the in-house scheme mints without a `v` but accepts both, because retagging git does not
    rename artefacts that were published under the old name."""
    tags = ["v2026.9.4"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.5"


def test_junk_tags_are_ignored_rather_than_fatal() -> None:
    tags = ["latest", "release-candidate", "", "2026.9.1", "2026.9.x"]

    assert _load().next_calver(tags, year=2026, month=9) == "2026.9.2"


@pytest.mark.parametrize(("year", "month"), ((2026, 9), (2026, 12), (2027, 1)))
def test_every_output_is_valid_semver(year, month) -> None:
    semver = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

    assert semver.match(_load().next_calver([], year=year, month=month))


def test_the_month_is_never_zero_padded() -> None:
    """`2026.09.0` is not SemVer, and Helm refuses it."""
    assert _load().next_calver([], year=2026, month=9) == "2026.9.0"


def test_ordering_survives_a_month_rollover() -> None:
    versions = ["2026.9.0", "2026.9.10", "2026.10.0", "2027.1.0"]
    keyed = [tuple(int(p) for p in v.split(".")) for v in versions]

    assert keyed == sorted(keyed)
