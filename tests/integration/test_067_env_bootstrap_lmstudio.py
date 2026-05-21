"""Integration test for self-hosted LCD bootstrap (spec 067 T021).

Gated by ``@pytest.mark.lmstudio``; auto-skips when ``LMSTUDIO_AVAILABLE != "1"``
or LM Studio is not reachable. Validates that a representative card moves to
``IN_REVIEW`` against a self-hosted LCD endpoint without any source patches
and that the three forbidden warning patterns do not appear.
"""
from __future__ import annotations

import os

import httpx
import pytest

LMSTUDIO_BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
LMSTUDIO_AVAILABLE = os.environ.get("LMSTUDIO_AVAILABLE") == "1"

pytestmark = pytest.mark.lmstudio

FORBIDDEN_WARNINGS = (
    "unsupported tool type",
    "developer role rewritten",
    "prompt_cache_key ignored",
)


@pytest.fixture(scope="module")
def lmstudio_reachable() -> bool:
    if not LMSTUDIO_AVAILABLE:
        pytest.skip("LMSTUDIO_AVAILABLE != 1")
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{LMSTUDIO_BASE_URL}/models")
            if resp.status_code != 200:
                pytest.skip(f"LM Studio /models returned {resp.status_code}")
    except Exception as exc:
        pytest.skip(f"LM Studio not reachable at {LMSTUDIO_BASE_URL}: {exc}")
    return True


def test_env_bootstrap_reaches_in_review(lmstudio_reachable: bool):
    """End-to-end smoke: dispatch a website card, expect IN_REVIEW.

    This test currently asserts the harness scaffolding only; the full
    end-to-end run is operator-driven via ``specs/067-compatibility-first-
    backend/quickstart.md`` Steps 4-5. Once a deterministic local-card
    fixture exists, replace this body with the full dispatch flow.
    """
    pytest.skip(
        "Operator-driven; run quickstart.md Steps 4-5 against an LM Studio "
        "instance serving qwen3-coder-30b at n_ctx >= 32768."
    )


def test_no_forbidden_warnings_in_lmstudio_console(lmstudio_reachable: bool):
    """When (and only when) the LM Studio console can be surfaced into the
    test, the three forbidden warning patterns must not appear. Until the
    console-tap interface lands, this test is a placeholder that documents
    the assertion.
    """
    pytest.skip("Console-tap not yet wired; assertion documented for spec 067.")
    captured_console_text = ""  # populated by console tap when available
    for needle in FORBIDDEN_WARNINGS:
        assert needle not in captured_console_text, (
            f"LM Studio console emitted forbidden warning {needle!r}; "
            "LCD profile is not being respected by opencode_compat"
        )
