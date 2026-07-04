"""Score.doc_mode field (spec 124 Foundational / FR-006, CT1)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from performer.models import Score


def _score(**kw) -> Score:
    base = {"title": "t", "repo_url": "https://github.com/acme/repo", "branch": "feat/x"}
    base.update(kw)
    return Score(**base)


def test_doc_mode_defaults_to_update() -> None:
    assert _score().doc_mode == "update"


def test_doc_mode_accepts_init() -> None:
    assert _score(doc_mode="init").doc_mode == "init"


def test_doc_mode_rejects_invalid_value() -> None:
    with pytest.raises(ValidationError):
        _score(doc_mode="bogus")


def test_old_payload_without_doc_mode_validates_to_update() -> None:
    # CT1: an older coordinare dispatch payload (no doc_mode) still validates.
    s = Score.model_validate(
        {"title": "t", "repo_url": "https://github.com/acme/repo", "branch": "feat/x"}
    )
    assert s.doc_mode == "update"
