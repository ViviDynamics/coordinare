"""nare-native-021 passed every install and check, then failed the clean consumer: the project was an editable install
bound to the install-time checkout, and a check resolved dependencies over the network. nare-native-023 failed it again
because tool caches were written into the read-only {cache}. Authors are told all three."""
from __future__ import annotations


def test_authors_are_told_the_clean_consumer_has_another_checkout_and_no_network() -> None:
    from coordinare.services import environment_discovery_prompts as prompts

    text = ' '.join(value for value in vars(prompts).values() if isinstance(value, str))
    assert 'a fresh copy of {repo} at another path, with no network' in text
    assert 'never as a link back to the install-time checkout' in text
    assert 'never resolves, downloads or re-syncs dependencies, not even offline' in text


def test_authors_are_told_the_clean_consumer_cache_is_read_only() -> None:
    from coordinare.services import environment_discovery_prompts as prompts

    text = ' '.join(value for value in vars(prompts).values() if isinstance(value, str))
    assert 'There {cache} is read-only: checks read what the installs persisted in {cache}' in text
    assert 'goes under {scratch}, never into {cache}' in text


def test_reading_the_cache_and_writing_scratch_are_told_apart() -> None:
    """nare-native-034: reviewers read 'never into {cache}' as forbidding reads, and two findings asked opposite things
    (point the checks' tool cache at the persisted cache; keep it under scratch). Reads are how checks use the cache;
    writes go to scratch; no check re-syncs."""
    import inspect

    from coordinare.services import environment_discovery
    from coordinare.services import environment_discovery_prompts as prompts

    text = ' '.join(value for value in vars(prompts).values() if isinstance(value, str))
    assert 'which is how they use it' in text
    # The execution rules every author and reviewer gets are built per attempt.
    assert 'and no check re-syncs dependencies, not even offline' in inspect.getsource(environment_discovery)
