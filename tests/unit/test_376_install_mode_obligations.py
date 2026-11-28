"""nare-native-040 spent four coverage revisions on checks asserting an editable install under the checkout, because
the scope criterion named "the editable root package"; the clean consumer's copy install could never satisfy them.
Manifest authors, check authors and reviewers are told to verify the outcome an install mode serves, not the mode."""
from __future__ import annotations

from coordinare.services.environment_discovery import EXECUTABLE_REVIEW_CRITERIA
from coordinare.services.environment_discovery_prompts import (
    _MANIFEST_RULES,
    CHECK_PROVES_ENVIRONMENT,
)
from coordinare.services.environment_shard_construction import CHECK_AUTHOR_INSTRUCTIONS


def test_every_author_and_reviewer_verifies_the_outcome_not_the_install_mode() -> None:
    assert 'never the install mode' in CHECK_PROVES_ENVIRONMENT
    assert "the package's file path" in CHECK_PROVES_ENVIRONMENT
    for prompt in (_MANIFEST_RULES, CHECK_AUTHOR_INSTRUCTIONS, EXECUTABLE_REVIEW_CRITERIA):
        assert CHECK_PROVES_ENVIRONMENT in prompt


def test_the_guidance_names_no_project_or_stack() -> None:
    lowered = CHECK_PROVES_ENVIRONMENT.lower()
    for name in ('nare', 'python', 'pip', 'uv ', 'hatch', 'ruby', 'node', 'npm', 'docker'):
        assert name not in lowered
