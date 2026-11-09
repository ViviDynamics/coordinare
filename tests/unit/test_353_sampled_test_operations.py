"""Test and build operations are sampled configuration proof, in stack-agnostic words (decision 2026-11-09)."""
from __future__ import annotations

import re

from coordinare.services.environment_discovery_prompts import (
    SAMPLE_TEST_CHECKS,
    legacy_discovery_system,
)
from coordinare.services.environment_scope import (
    AUTHOR_INSTRUCTIONS,
    GRANULARITY_GUIDANCE,
    REVIEW_INSTRUCTIONS,
    TEST_OPERATION_RULE,
)
from coordinare.services.environment_shard_construction import CHECK_AUTHOR_INSTRUCTIONS

STACK_NAMES = re.compile(r'\b(rails|ruby|rspec|cucumber|bundler?|gem|python|pytest|node|npm|jest|go test|cargo|maven|'
                         r'gradle|junit|django|postgres)\b', re.IGNORECASE)


def test_the_rules_name_no_technology():
    assert not STACK_NAMES.search(TEST_OPERATION_RULE)
    assert not STACK_NAMES.search(SAMPLE_TEST_CHECKS)


def test_scope_authors_and_reviewers_get_the_test_operation_rule():
    assert TEST_OPERATION_RULE in GRANULARITY_GUIDANCE and TEST_OPERATION_RULE in AUTHOR_INSTRUCTIONS
    assert TEST_OPERATION_RULE in REVIEW_INSTRUCTIONS
    assert 'operation that must succeed' not in GRANULARITY_GUIDANCE


def test_check_authors_get_the_sampling_rule():
    assert SAMPLE_TEST_CHECKS in CHECK_AUTHOR_INSTRUCTIONS
    assert SAMPLE_TEST_CHECKS in legacy_discovery_system()
    assert 'never a whole suite' in SAMPLE_TEST_CHECKS
    assert 'failed assertion' in SAMPLE_TEST_CHECKS
