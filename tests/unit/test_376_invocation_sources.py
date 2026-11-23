"""nare-native-024, -026, -028 and -031 each spent a repair on a check that called a command without an argument or
switch it requires, or with a help syntax its version rejects. Every check author is told to copy invocations from the
repository's own tests and documentation."""
from __future__ import annotations

from coordinare.services.environment_discovery_prompts import INVOCATION_SOURCES


def test_every_check_author_is_told_to_copy_invocations_from_the_repository() -> None:
    from coordinare.services import environment_discovery, environment_discovery_prompts
    from coordinare.services.environment_shard_construction import CHECK_AUTHOR_INSTRUCTIONS

    assert "Copy each command's invocation from the repository, never from memory" in INVOCATION_SOURCES
    assert INVOCATION_SOURCES in CHECK_AUTHOR_INSTRUCTIONS
    for module in (environment_discovery, environment_discovery_prompts):
        assert any(isinstance(value, str) and INVOCATION_SOURCES in value for value in vars(module).values()), module


def test_the_guidance_names_no_project_or_stack() -> None:
    lowered = INVOCATION_SOURCES.lower()
    for name in ('nare', 'python', 'pytest', 'ruby', 'rails', 'node', 'npm', 'aws', 'docker', 'uv '):
        assert name not in lowered
