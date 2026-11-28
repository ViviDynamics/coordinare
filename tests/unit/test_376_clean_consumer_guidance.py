"""nare-native-021 passed every install and check, then failed the clean consumer: the project was an editable install
bound to the install-time checkout, and a check resolved dependencies over the network. nare-native-023 failed it again
because tool caches were written into the read-only {cache}. Authors are told all three, once, in the execution rules every author and reviewer packet carries."""
from __future__ import annotations

from coordinare.services.environment_discovery import EXECUTION_RULES


def test_authors_are_told_the_clean_consumer_has_another_checkout_and_no_network() -> None:
    assert 'a fresh copy of {repo} at another path, with no network' in EXECUTION_RULES
    assert 'never as a link back to the install-time checkout' in EXECUTION_RULES
    assert 'never resolves, downloads or re-syncs dependencies, not even offline' in EXECUTION_RULES


def test_authors_are_told_the_clean_consumer_cache_is_read_only() -> None:
    from coordinare.services.environment_discovery_prompts import _MANIFEST_RULES

    assert 'They see {cache} read-only' in EXECUTION_RULES
    assert 'goes under {scratch}, never into {cache}' in EXECUTION_RULES
    assert '{cache} read-only and no network' in _MANIFEST_RULES


def test_reading_the_cache_and_writing_scratch_are_told_apart() -> None:
    """nare-native-034: reviewers read 'never into {cache}' as forbidding reads, and two findings asked opposite things
    (point the checks' tool cache at the persisted cache; keep it under scratch). Reads are how checks use the cache;
    writes go to scratch; no check re-syncs."""
    assert 'they read what the installs persisted in {cache}, which is how they use it' in EXECUTION_RULES
    assert 're-syncs dependencies, not even offline' in EXECUTION_RULES
