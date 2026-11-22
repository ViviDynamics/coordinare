"""nare-native-027 never ran a check: a required install ran a full benchmark verify and left containers behind, and
an offline switch set in activation.env, which install commands also get, blocked the dependency install. Authors
are told both."""
from __future__ import annotations


def _prompt_text() -> str:
    from coordinare.services import environment_discovery_prompts as prompts

    return ' '.join(value for value in vars(prompts).values() if isinstance(value, str))


def test_authors_keep_verification_out_of_installs() -> None:
    text = _prompt_text()
    assert 'never run a test, verification or benchmark suite in an install' in text
    assert 'removes them before it exits, including when it fails' in text


def test_authors_are_told_activation_applies_to_installs() -> None:
    text = _prompt_text()
    assert 'Activation applies to install commands too' in text
    assert 'never set an offline or no-download switch in activation.env that an install needs' in text
