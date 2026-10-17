"""Evidence-bound discovery and fresh, read-only coverage review."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest
from coordinare_service_inference.environment.models import (
    EnvironmentManifest,
    ExecutionCapabilities,
)

from coordinare.services.environment_discovery import (
    DiscoveryLimits,
    EnvironmentDiscoveryError,
    discover_and_review,
)

SOURCE = "Use the unfamiliar zog tool: zog --version must report 7."
PATH = "nested/how-to.zog"
COMMIT = "a" * 40


CAPABILITIES = ExecutionCapabilities(schema_version=1, image_ref="trusted/global@sha256:" + "a" * 64,
    platform="linux/x86_64", cache_mount_path="/devenv/suite", install_network="enabled", verify_network="disabled")

INVENTORY_DIGEST = hashlib.sha256(json.dumps(sorted([PATH, "other/evidence"]), separators=(",", ":")).encode()).hexdigest()

def candidate():
    return {
        "inventory_digest": INVENTORY_DIGEST, "capabilities_digest": CAPABILITIES.digest(),
        "schema_version": 1, "policy_version": "351-v1", "symphony_name": "suite",
        "source_commit": COMMIT, "inputs": {PATH: hashlib.sha256(SOURCE.encode()).hexdigest()},
        "requirements": [{"id": "tool", "description": "zog version 7", "source": PATH, "checks": ["version"], "required": True}],
        "install": [], "checks": [{"id": "version", "argv": ["zog", "--version"], "required": True}],
        "activation": {"path": [], "env": {}}, "services": [], "empty_reason": None,
    }


class ScriptedChat:
    def __init__(self, manifest=None, verdict="accept", digest=None, sources=None):
        self.manifest = manifest or candidate()
        self.verdict = verdict
        self.digest = digest
        self.sources = sources if sources is not None else [PATH]
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(copy.deepcopy(messages))
        index = len(self.calls)
        if index in (1, 3):
            return json.dumps({"tool": "read_file", "path": PATH})
        if index == 2:
            return json.dumps({"manifest": self.manifest})
        return json.dumps({"assessment": {
            "inventory_digest": INVENTORY_DIGEST, "capabilities_digest": CAPABILITIES.digest(),
            "schema_version": 1, "policy_version": "351-v1", "source_commit": COMMIT,
            "manifest_digest": self.digest or EnvironmentManifest.model_validate(self.manifest).digest(),
            "verdict": self.verdict, "rationale": "Evidence covers documented constraints", "sources": self.sources,
        }})


async def discover(chat, **kwargs):
    async def fetch(path):
        return SOURCE if path == PATH else "additional evidence"
    return await discover_and_review(symphony_name="suite", commit=COMMIT,
        paths=[PATH, "other/evidence"], fetch=fetch, chat=chat, capabilities=CAPABILITIES, **kwargs)


@pytest.mark.asyncio
async def test_accepts_unfamiliar_nested_evidence_with_fresh_review():
    chat = ScriptedChat()
    manifest, assessment = await discover(chat)
    assert assessment.manifest_digest == manifest.digest()
    reviewer_start = chat.calls[2]
    assert len(reviewer_start) == 2
    assert not any(message["role"] == "assistant" for message in reviewer_start)
    assert SOURCE not in json.dumps(reviewer_start)
    assert PATH in json.dumps(reviewer_start)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["wrong_digest", "revise", "fake_source", "wrong_input", "wrong_commit", "unknown_key"])
async def test_refuses_unaccepted_or_unproven_output(change):
    chat = ScriptedChat()
    if change == "wrong_digest":
        chat.digest = "0" * 64
    elif change == "revise":
        chat.verdict = "revise"
    elif change == "fake_source":
        chat.sources = ["other/evidence"]
    elif change == "wrong_input":
        chat.manifest["inputs"][PATH] = "0" * 64
    elif change == "wrong_commit":
        chat.manifest["source_commit"] = "b" * 40
    else:
        chat.manifest["unsupported"] = True
    with pytest.raises(EnvironmentDiscoveryError):
        await discover(chat)


@pytest.mark.asyncio
async def test_review_must_independently_read_manifest_evidence():
    chat = ScriptedChat()
    original = chat.__call__

    async def no_review_reads(messages):
        if len(chat.calls) == 2:
            chat.calls.append(copy.deepcopy(messages))
        return await original(messages)

    with pytest.raises(EnvironmentDiscoveryError, match=r"review.*read|inspect"):
        await discover(no_review_reads)


@pytest.mark.asyncio
async def test_review_can_inspect_additional_repository_evidence():
    base = ScriptedChat()
    count = 0

    async def chat(messages):
        nonlocal count
        count += 1
        if count == 4:
            return json.dumps({"tool": "read_file", "path": "other/evidence"})
        return await base(messages)

    _, assessment = await discover(chat)
    assert assessment.verdict == "accept"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["{", "{}", '{"tool":"execute","argv":["touch","bad"]}', '{"tool":"read_file","path":"../bad"}', '{"tool":"list_files","extra":1}'])
async def test_malformed_or_mutating_tools_hold(reply):
    async def chat(messages):
        return reply
    with pytest.raises(EnvironmentDiscoveryError):
        await discover(chat)


@pytest.mark.asyncio
async def test_missing_model_configuration_holds():
    with pytest.raises(EnvironmentDiscoveryError, match="config"):
        await discover(None)


@pytest.mark.asyncio
async def test_phase_deadline_covers_model_call():
    async def chat(messages):
        await asyncio.sleep(1)
        return "{}"
    with pytest.raises(EnvironmentDiscoveryError, match="deadline"):
        await discover(chat, limits=DiscoveryLimits(phase_timeout_seconds=0.01))


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [DiscoveryLimits(max_reads=1), DiscoveryLimits(max_file_bytes=3), DiscoveryLimits(max_source_bytes=3), DiscoveryLimits(max_inventory_entries=1), DiscoveryLimits(max_model_calls=1)])
async def test_bounds_fail_explicitly(limits):
    async def chat(messages):
        return json.dumps({"tool": "read_file", "path": PATH})
    with pytest.raises(EnvironmentDiscoveryError, match="limit"):
        await discover(chat, limits=limits)


@pytest.mark.asyncio
async def test_assessment_records_trusted_reviewer_reads():
    manifest, assessment = await discover(ScriptedChat())
    assert assessment.inputs == manifest.inputs
    assert assessment.symphony_name == "suite"


@pytest.mark.asyncio
async def test_snapshot_must_not_change_between_discovery_and_review():
    count = 0

    async def fetch(path):
        nonlocal count
        count += 1
        return SOURCE if count == 1 else SOURCE + "changed"

    with pytest.raises(EnvironmentDiscoveryError):
        await discover_and_review(symphony_name="suite", commit=COMMIT, paths=[PATH], fetch=fetch, chat=ScriptedChat(), capabilities=CAPABILITIES)


@pytest.mark.asyncio
async def test_source_unavailable_holds():
    async def fetch(path):
        return None

    with pytest.raises(EnvironmentDiscoveryError, match="unavailable"):
        await discover_and_review(symphony_name="suite", commit=COMMIT, paths=[PATH], fetch=fetch, chat=ScriptedChat(), capabilities=CAPABILITIES)


@pytest.mark.asyncio
async def test_duplicate_envelope_keys_hold():
    async def chat(messages):
        return '{"tool":"list_files","tool":"list_files"}'

    with pytest.raises(EnvironmentDiscoveryError, match="repair limit"):
        await discover(chat)


@pytest.mark.asyncio
async def test_output_limit_holds():
    async def chat(messages):
        return " " * 1024

    with pytest.raises(EnvironmentDiscoveryError, match="limit"):
        await discover(chat, limits=DiscoveryLimits(max_output_bytes=100))


def test_persisted_review_rationale_contains_only_daemon_generated_text():
    from coordinare.services.environment_discovery import _validated_assessment

    manifest = EnvironmentManifest.model_validate(ScriptedChat().manifest)
    sensitive = 'repository credential must never be persisted: private-value'
    assessment = _validated_assessment({
        'schema_version': 1, 'policy_version': '351-v1', 'source_commit': COMMIT,
        'manifest_digest': manifest.digest(), 'verdict': 'accept', 'inventory_digest': INVENTORY_DIGEST, 'capabilities_digest': CAPABILITIES.digest(),
        'rationale': sensitive, 'sources': [PATH],
    }, {PATH: manifest.inputs[PATH]}, manifest)
    assert assessment.rationale == 'Independent coverage review accepted; source identities validated by daemon.'
    assert sensitive not in assessment.model_dump_json()
