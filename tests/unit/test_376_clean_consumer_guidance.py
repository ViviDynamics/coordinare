"""nare-native-021 passed every install and check, then failed the clean consumer: the project was an editable install
bound to the install-time checkout, and a check resolved dependencies over the network. Authors are told both."""
from __future__ import annotations


def test_authors_are_told_the_clean_consumer_has_another_checkout_and_no_network() -> None:
    from coordinare.services import environment_discovery_prompts as prompts

    text = ' '.join(value for value in vars(prompts).values() if isinstance(value, str))
    assert 'a fresh copy of {repo} at another path, with no network' in text
    assert 'never as a link back to the install-time checkout' in text
    assert 'resolving or downloading dependencies at run time' in text
