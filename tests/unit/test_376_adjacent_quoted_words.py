"""nare-native-019: an install with two adjacent quoted words ('{cache}/x' '{scratch}') was left unjudged by every
static rule, so the daemon's URL check never requested its jq release URL, which answered 404 at execution."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import Command
from coordinare_service_inference.environment.static_rules import _segments, fetched_urls

JQ = 'https://github.com/jq/jq/releases/download/jq-1.7.1/jq-linux-aarch64'


def command(script: str) -> Command:
    return Command(id='install-jq', argv=['bash', '-c', script])


def test_adjacent_quoted_words_are_judged_and_their_download_is_found() -> None:
    script = (f"set -e; mkdir -p '{{cache}}/tools/bin' '{{scratch}}'; curl -fsSL -o '{{cache}}/tools/bin/jq' {JQ}; "
              "chmod +x '{cache}/tools/bin/jq'")
    assert fetched_urls(command(script)) == [JQ]


@pytest.mark.parametrize('script', ["echo ';' && curl -o x https://a.example/b", "a ' | ' b", "printf '%s' \"; \""])
def test_a_quoted_word_of_separators_still_leaves_the_script_unjudged(script: str) -> None:
    assert _segments(command(script)) == []
