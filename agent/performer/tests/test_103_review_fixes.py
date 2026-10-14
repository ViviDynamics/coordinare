"""103 review fixes (performer): the shape classifier says what it means, its
guidance table covers every shape, and a restart delivers the repair once."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from performer.main import (
    Performance,
    _handle_backend_parse_failure,
    _OUTPUT_SHAPE_GUIDANCE,
    _output_shape,
)
from performer.models import Score, Stand

_ALL_SHAPES = {"empty", "array", "head_cut", "tail_cut", "whole", "wrapped", "no_object"}
_HEAD_CUT = 'ufficient": true, "questions": [], "dependencies": []}'


def _perf(backend: object) -> Performance:
    stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
    stand.git_env = {}
    score = Score(
        title="Format phone",
        repo_url="https://github.com/acme/repo",
        branch="feat/test",
        model="m",
        max_tokens=100,
    )
    return Performance(
        session_id="sid", stand=stand, score=score, backend=backend, role="assessing"
    )


@pytest.mark.parametrize(
    ("raw", "shape"),
    [
        ("}", "no_object"),
        ("plain prose", "no_object"),
        ("[1,2,3]", "array"),
        ("see {x} ok", "wrapped"),
        ('{"a": 1}', "whole"),
        (_HEAD_CUT, "head_cut"),
        ('{"a": ', "tail_cut"),
        ("", "empty"),
    ],
)
def test_output_shape_names_what_it_sees(raw: str, shape: str) -> None:
    assert _output_shape(raw) == shape


def test_guidance_table_covers_every_shape() -> None:
    assert set(_OUTPUT_SHAPE_GUIDANCE) == _ALL_SHAPES


@pytest.mark.asyncio
async def test_restart_delivers_the_repair_once_and_replaces_the_daemon_hint() -> None:
    backend = MagicMock()
    backend.relay_feedback = AsyncMock(side_effect=RuntimeError("no interactive channel"))
    backend.stop = AsyncMock()
    backend.start = AsyncMock()
    perf = _perf(backend)
    perf.score.relay_feedback = [
        {"body": "daemon hint", "author_login": "coordinare", "kind": "format_repair"},
        {"body": "review note", "author_login": "reviewer"},
    ]
    await _handle_backend_parse_failure(
        perf,
        _HEAD_CUT,
        "assessment",
        SimpleNamespace(BACKEND_PARSE_RETRIES=1),
        "could not be parsed as a JSON object",
    )
    started = backend.start.await_args.args[1]
    bodies = [item["body"] for item in started.relay_feedback]
    assert "daemon hint" not in bodies
    assert "review note" in bodies
    repairs = [item for item in started.relay_feedback if item.get("author_login") == "performer"]
    assert len(repairs) == 1
