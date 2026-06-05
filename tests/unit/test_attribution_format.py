"""Standardized PR/issue comment attribution header (spec 077).

Coordinare and performer post as the *same* GitHub app, so a comment's only
disambiguator is the attribution header: a hidden machine marker plus a visible
blockquote line naming who spoke, with which agent harness, driving which model.

The header is duplicated across two deployable units (coordinare's
``coordinare.graph.attribution`` and the performer's ``performer.main``) because
they cannot share a module. These tests pin the exact format AND assert the two
implementations stay byte-for-byte in sync — if one drifts, this fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinare.graph.attribution import (
    attribution_header,
    coordinare_attribution,
    resolve_stage_attribution,
)

# The performer is a separate package under agent/performer/src. Make it
# importable so we can assert the two header builders agree.
_PERFORMER_SRC = Path(__file__).resolve().parents[2] / "agent" / "performer" / "src"
if str(_PERFORMER_SRC) not in sys.path:
    sys.path.insert(0, str(_PERFORMER_SRC))

try:
    from performer.main import _attribution_header as performer_attribution_header
except Exception:  # pragma: no cover - performer source not present
    performer_attribution_header = None


# --- exact-format pins -----------------------------------------------------


def test_performer_origin_format():
    out = attribution_header(
        origin="performer",
        role="reviewing",
        display="Reviewer",
        harness="codex",
        model="spark/gpt-oss:120b",
    )
    assert out == (
        "<!-- coordinare-attribution origin=performer role=reviewing "
        "harness=codex model=spark/gpt-oss:120b -->\n"
        "> 🤖 **Reviewer** · harness `codex` · model `spark/gpt-oss:120b`"
    )


def test_coordinare_origin_tags_stage():
    out = attribution_header(
        origin="coordinare",
        role="implementing",
        display="Implementer",
        harness="codex",
        model="spark/qwen3.6:35b",
    )
    assert out == (
        "<!-- coordinare-attribution origin=coordinare role=implementing "
        "harness=codex model=spark/qwen3.6:35b -->\n"
        "> 🎼 **Coordinare** · re: Implementer (`codex` · `spark/qwen3.6:35b`)"
    )


def test_coordinare_bare_when_stage_agnostic():
    # Board-level coordinare comments (dep cycles, blocked reminders, advocate)
    # have no harness/model to tag — emit a bare identity, not `re: ? (? · ?)`.
    out = attribution_header(
        origin="coordinare",
        role="coordinare",
        display="Coordinare",
        harness="?",
        model="?",
    )
    assert out == (
        "<!-- coordinare-attribution origin=coordinare role=coordinare "
        "harness=? model=? -->\n"
        "> 🎼 **Coordinare**"
    )


# --- stage resolution from config -----------------------------------------


class _FakeRoleConfig:
    def __init__(self, backend: str, model: str) -> None:
        self.backend = backend
        self.model = model


class _FakePerformers:
    def __init__(self, mapping: dict[str, _FakeRoleConfig]) -> None:
        self._mapping = mapping

    def resolved_role(self, role: str) -> _FakeRoleConfig:
        return self._mapping[role]


class _FakeConfig:
    def __init__(self, performers: _FakePerformers) -> None:
        self.performers = performers


def test_resolve_stage_attribution_from_config():
    config = _FakeConfig(
        _FakePerformers({"implementer": _FakeRoleConfig("codex", "spark/qwen3.6:35b")})
    )
    display, harness, model = resolve_stage_attribution(config, "implementing")
    assert (display, harness, model) == ("Implementer", "codex", "spark/qwen3.6:35b")


def test_resolve_stage_falls_back_to_unknown():
    # Unmapped stage / config without performers → "?" sentinels, bare Coordinare.
    _display, harness, model = resolve_stage_attribution(object(), None)
    assert (harness, model) == ("?", "?")


def test_coordinare_attribution_end_to_end():
    config = _FakeConfig(
        _FakePerformers({"implementer": _FakeRoleConfig("codex", "spark/qwen3.6:35b")})
    )
    out = coordinare_attribution(config, "implementing")
    assert out == (
        "<!-- coordinare-attribution origin=coordinare role=implementing "
        "harness=codex model=spark/qwen3.6:35b -->\n"
        "> 🎼 **Coordinare** · re: Implementer (`codex` · `spark/qwen3.6:35b`)"
    )


def test_coordinare_attribution_bare_when_unresolvable():
    out = coordinare_attribution(object(), None)
    assert out == (
        "<!-- coordinare-attribution origin=coordinare role=coordinare "
        "harness=? model=? -->\n"
        "> 🎼 **Coordinare**"
    )


# --- cross-unit sync: coordinare header == performer header -----------------


@pytest.mark.skipif(
    performer_attribution_header is None, reason="performer source not importable"
)
@pytest.mark.parametrize(
    ("role", "display", "harness", "model"),
    [
        ("reviewing", "Reviewer", "codex", "spark/gpt-oss:120b"),
        ("qa", "QA", "claude_code", "spark/gpt-oss:20b"),
        ("documenting", "Tech Writer", "hermes", "spark/qwen3.6:35b"),
    ],
)
def test_performer_and_coordinare_headers_match(role, display, harness, model):
    coordinare_out = attribution_header(
        origin="performer", role=role, display=display, harness=harness, model=model
    )
    performer_out = performer_attribution_header(
        origin="performer", role=role, display=display, harness=harness, model=model
    )
    assert coordinare_out == performer_out
