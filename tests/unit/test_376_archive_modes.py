"""nare-native-029, -031 and -037 each spent a repair on an install that extracted a release archive with a
standard-library zip module, which dropped the executable bit. Every manifest author and reviewer is told to keep
file modes when extracting."""
from __future__ import annotations

from coordinare.services.environment_discovery import EXECUTABLE_REVIEW_CRITERIA
from coordinare.services.environment_discovery_prompts import _MANIFEST_RULES, ARCHIVE_MODES


def test_authors_and_reviewers_are_told_to_keep_modes_when_extracting() -> None:
    assert 'drop the executable bit' in ARCHIVE_MODES and 'unzip or tar' in ARCHIVE_MODES
    assert ARCHIVE_MODES in _MANIFEST_RULES
    assert ARCHIVE_MODES in EXECUTABLE_REVIEW_CRITERIA


def test_the_guidance_names_no_project_or_stack() -> None:
    lowered = ARCHIVE_MODES.lower()
    for name in ('nare', 'python', 'pytest', 'ruby', 'rails', 'node', 'npm', 'aws', 'docker', 'zipfile'):
        assert name not in lowered
