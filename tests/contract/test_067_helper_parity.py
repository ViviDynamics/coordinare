"""Parity test: performer ``_lcd_helpers`` ↔ coordinare ``upstream_errors``.

Spec 067 deliberately duplicates ``strip_base_url_credentials`` and
``truncate_body`` across the coordinare and the performer to avoid a
coordinare↔performer import (see ``_lcd_helpers.py`` module docstring). This
test pins both copies to the *same* observable behaviour so they cannot drift
silently — if one is changed, the other must change too or this test fails.
"""
from __future__ import annotations

import pytest
from performer.backends._lcd_helpers import (
    BODY_CAP_BYTES as P_CAP,
)
from performer.backends._lcd_helpers import (
    TRUNCATION_SUFFIX as P_SUFFIX,
)
from performer.backends._lcd_helpers import (
    strip_base_url_credentials as p_strip,
)
from performer.backends._lcd_helpers import (
    truncate_body as p_truncate,
)

from coordinare.upstream_errors import (
    BODY_CAP_BYTES as C_CAP,
)
from coordinare.upstream_errors import (
    TRUNCATION_SUFFIX as C_SUFFIX,
)
from coordinare.upstream_errors import (
    strip_base_url_credentials as c_strip,
)
from coordinare.upstream_errors import (
    truncate_body as c_truncate,
)

pytestmark = pytest.mark.contract


def test_constants_match():
    assert C_CAP == P_CAP
    assert C_SUFFIX == P_SUFFIX


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1234/v1",
        "https://api.example.com/v1?api_key=sk-secret&model=foo",
        "https://user:pw@host:8443/path?api_key=k&model=x",
        "http://lm:1234",
        "https://api.example.com/v1?model=foo&api_key=sk-secret",
        "https://api.example.com/v1?API_KEY=sk-secret",  # case-insensitive
    ],
)
def test_strip_base_url_credentials_parity(url: str):
    assert c_strip(url) == p_strip(url), url


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "short body",
        "x" * (C_CAP - 1),
        "x" * C_CAP,
        "x" * (C_CAP + 1),
        "x" * (C_CAP * 2),
        # multi-byte chars at the boundary — both must agree on the cut.
        "α" * (C_CAP // 2 + 5),  # noqa: RUF001 — multi-byte boundary fixture
    ],
)
def test_truncate_body_parity(raw: str):
    assert c_truncate(raw) == p_truncate(raw)
