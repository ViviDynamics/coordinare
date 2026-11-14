"""Authors are told how carrier commands reach a guest container's port, and what an advisory audit check proves.

Dettmore e2e-58 held on its coverage limit. Postgres ran in guest Docker published on the guest's 127.0.0.1 with no
runtime_endpoints forward, so docker exec health checks passed while every carrier database client was refused; and
bin/bundler-audit failed on advisories published after the pinned commit, which no environment change could pass.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

from coordinare_service_inference.environment.runtime import RuntimeEndpoint

from coordinare.services.environment_capabilities import (
    GUEST_IMAGE_GUIDANCE,
    GUEST_PORT_GUIDANCE,
    carrier_process_policy,
)
from coordinare.services.environment_discovery import EXECUTABLE_REVIEW_INSTRUCTIONS
from coordinare.services.environment_discovery_prompts import (
    _MANIFEST_INSTRUCTIONS,
    ADVISORY_AUDIT_CHECKS,
)
from coordinare.services.environment_shard_construction import CHECK_AUTHOR_INSTRUCTIONS


def _policy():
    resources = SimpleNamespace(max_seconds=7200, cpus=8)
    return carrier_process_policy(SimpleNamespace(runtime=SimpleNamespace(resources=resources)))


def test_the_runtime_guidance_says_a_guest_port_needs_a_forward_published_on_every_guest_interface():
    guidance = _policy()['runtime_ordering_guidance']
    assert GUEST_PORT_GUIDANCE in guidance
    assert 'runtime_endpoints' in GUEST_PORT_GUIDANCE and 'never -p 127.0.0.1:5432:5432' in GUEST_PORT_GUIDANCE
    assert 'docker exec health check does not prove carrier clients can connect' in GUEST_PORT_GUIDANCE


def test_the_worked_endpoint_is_one_the_manifest_accepts():
    example = json.loads(re.search(r'\{"id": "postgres".*?\}', GUEST_PORT_GUIDANCE).group(0))
    endpoint = RuntimeEndpoint.model_validate(example)
    assert (endpoint.guest_port, endpoint.outer_port) == (5432, 5432)


def test_every_author_and_reviewer_of_checks_gets_the_advisory_audit_rule():
    for instructions in (_MANIFEST_INSTRUCTIONS, CHECK_AUTHOR_INSTRUCTIONS, EXECUTABLE_REVIEW_INSTRUCTIONS):
        assert ADVISORY_AUDIT_CHECKS in instructions
    assert 'Never require that no advisories are found' in ADVISORY_AUDIT_CHECKS
    assert 'never ignore advisories to make one pass' in ADVISORY_AUDIT_CHECKS


def test_the_runtime_guidance_says_to_pull_service_images_at_install():
    # Dettmore e2e-61: the service start had to pull pgvector itself and its health check ran out first.
    assert GUEST_IMAGE_GUIDANCE in _policy()['runtime_ordering_guidance']
    assert 'docker pull' in GUEST_IMAGE_GUIDANCE and 'no network' in GUEST_IMAGE_GUIDANCE


def test_a_guest_shutdown_gets_three_minutes_on_a_loaded_host():
    from coordinare_service_inference.environment.runtime_supervisor import CLEANUP_SECONDS

    assert CLEANUP_SECONDS == 180


def test_authors_and_reviewers_are_told_timeouts_are_per_command_ceilings():
    from coordinare.services.environment_discovery_prompts import TIMEOUT_CEILINGS

    # Dettmore e2e-66: reviewers summed every ceiling against the session budget for six coverage revisions.
    for instructions in (_MANIFEST_INSTRUCTIONS, CHECK_AUTHOR_INSTRUCTIONS, EXECUTABLE_REVIEW_INSTRUCTIONS):
        assert TIMEOUT_CEILINGS in instructions
    assert 'may add up to more than the session_budget' in TIMEOUT_CEILINGS
    assert 'Never shorten a ceiling below what its work needs' in TIMEOUT_CEILINGS
