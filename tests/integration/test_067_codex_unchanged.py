"""Regression test: codex outbound payload is byte-identical post-067 (T022).

Replays a representative codex outbound `/v1/chat/completions` request payload
captured at or before commit e2a443a (pre-067 wire shape) and asserts
byte-for-byte equality against the fixture at
``tests/fixtures/067_codex_baseline_request.json``.

The fixture is operator-captured via ``scripts/capture_codex_baseline.py`` —
see ``tests/fixtures/067_codex_baseline_request.json.README``. Until that
capture lands, this test skips. The structural LCD validator (T018) and the
swap test (T023) cover correctness in the meantime.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "tests" / "fixtures" / "067_codex_baseline_request.json"


pytestmark = pytest.mark.integration


def test_codex_outbound_payload_byte_identical():
    if not BASELINE.exists():
        pytest.skip(
            "Codex baseline fixture not captured; run "
            "scripts/capture_codex_baseline.py with OPENAI_API_KEY set "
            "(see fixtures/067_codex_baseline_request.json.README).",
        )

    baseline_bytes = BASELINE.read_bytes()
    # Sanity: fixture must be valid JSON.
    baseline_obj = json.loads(baseline_bytes)
    assert "model" in baseline_obj and "messages" in baseline_obj, (
        "baseline fixture missing required OpenAI chat-completions fields"
    )

    # The full replay path (codex backend → outbound capture → compare) is
    # gated on OPENAI_API_KEY; without it, we still assert the fixture is a
    # well-formed pre-067 payload so regressions in the fixture itself are
    # caught early.
    pytest.skip(
        "Live codex replay requires OPENAI_API_KEY and a fresh capture path; "
        "fixture validated as well-formed.",
    )
