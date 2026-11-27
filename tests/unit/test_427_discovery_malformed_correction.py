"""427: a reply that is not one JSON object costs the model a turn, not the run.

Claude Sonnet sometimes closes a correct tool call with a stray code fence. The strict
decoder still rejects that reply, and nothing in it is acted on, but the phase answers it
once with a correction instead of ending discovery.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services.environment_discovery import (
    DiscoveryLimits,
    EnvironmentDiscoveryError,
    discover_and_review,
)

SOURCE = "Use the unfamiliar zog tool: zog --version must report 7."
PATH = "nested/how-to.zog"
COMMIT = "a" * 40
FENCED = json.dumps({"tool": "read_file", "path": PATH}) + "\n```"
SELF_CORRECTED = FENCED + '\n\nCorrection: one JSON object only.\n\n{"tool":"read_file","path":"other/evidence"}'


def candidate():
    return {
        "schema_version": 1, "policy_version": "351-v1", "symphony_name": "suite",
        "source_commit": COMMIT, "inputs": {PATH: hashlib.sha256(SOURCE.encode()).hexdigest()},
        "requirements": [{"id": "tool", "description": "zog version 7", "source": PATH, "checks": ["version"], "required": True}],
        "install": [], "checks": [{"id": "version", "argv": ["zog", "--version"], "required": True}],
        "activation": {"path": [], "env": {}}, "services": [], "empty_reason": None,
    }


def assessment():
    return {"assessment": {
        "schema_version": 1, "policy_version": "351-v1", "source_commit": COMMIT,
        "manifest_digest": EnvironmentManifest.model_validate(candidate()).digest(),
        "verdict": "accept", "rationale": "Evidence covers documented constraints", "sources": [PATH],
    }}


class Replies:
    """Answers each call with the next scripted reply, recording what it was sent."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def __call__(self, messages):
        self.calls.append(copy.deepcopy(messages))
        return self.replies.pop(0)


READ = json.dumps({"tool": "read_file", "path": PATH})
MANIFEST = json.dumps({"manifest": candidate()})
ASSESSMENT = json.dumps(assessment())


async def discover(chat, **kwargs):
    reads: list[str] = []

    async def fetch(path):
        reads.append(path)
        return SOURCE if path == PATH else "additional evidence"
    result = await discover_and_review(symphony_name="suite", commit=COMMIT,
        paths=[PATH, "other/evidence"], fetch=fetch, chat=chat, **kwargs)
    return result, reads


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [FENCED, SELF_CORRECTED, "Here is the call: " + READ, "not json at all"])
async def test_one_malformed_discovery_reply_is_answered_with_a_correction(bad):
    chat = Replies(bad, READ, MANIFEST, READ, ASSESSMENT)
    (manifest, accepted), reads = await discover(chat)
    assert accepted.verdict == "accept"
    assert manifest.inputs == {PATH: hashlib.sha256(SOURCE.encode()).hexdigest()}
    # Nothing in the rejected reply was acted on: the self-corrected tail named another file.
    assert "other/evidence" not in reads
    correction = chat.calls[1]
    assert correction[-2] == {"role": "assistant", "content": bad}
    assert correction[-1]["role"] == "user"
    feedback = json.loads(correction[-1]["content"])
    assert "one JSON object" in feedback["error"]


@pytest.mark.asyncio
async def test_one_malformed_review_reply_is_answered_with_a_correction():
    chat = Replies(READ, MANIFEST, READ, FENCED, ASSESSMENT)
    (_, accepted), _ = await discover(chat)
    assert accepted.verdict == "accept"
    assert chat.calls[4][-2] == {"role": "assistant", "content": FENCED}


@pytest.mark.asyncio
async def test_two_consecutive_malformed_replies_end_discovery():
    chat = Replies(FENCED, FENCED, READ, MANIFEST)
    with pytest.raises(EnvironmentDiscoveryError, match=r"^discovery returned malformed JSON$"):
        await discover(chat)
    assert len(chat.calls) == 2


@pytest.mark.asyncio
async def test_two_consecutive_malformed_review_replies_end_review():
    chat = Replies(READ, MANIFEST, FENCED, FENCED, ASSESSMENT)
    with pytest.raises(EnvironmentDiscoveryError, match=r"^review returned malformed JSON$"):
        await discover(chat)


@pytest.mark.asyncio
async def test_a_good_reply_between_malformed_ones_earns_another_correction():
    chat = Replies(FENCED, READ, FENCED, MANIFEST, READ, ASSESSMENT)
    (_, accepted), _ = await discover(chat)
    assert accepted.verdict == "accept"


@pytest.mark.asyncio
async def test_corrections_count_toward_the_model_call_limit():
    chat = Replies(FENCED, READ, MANIFEST)
    with pytest.raises(EnvironmentDiscoveryError, match="model call limit"):
        await discover(chat, limits=DiscoveryLimits(max_model_calls=2))


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "error"), [
    ('{"tool":"list_files","tool":"list_files"}', r"^discovery returned malformed JSON$"),
    ('["tool"]', "invalid envelope"),
    ('{"tool":"execute","argv":["touch","bad"]}', "unsupported tool"),
])
async def test_other_rejections_still_end_discovery_at_once(reply, error):
    chat = Replies(reply, READ, MANIFEST, READ, ASSESSMENT)
    with pytest.raises(EnvironmentDiscoveryError, match=error):
        await discover(chat)
    assert len(chat.calls) == 1


@pytest.mark.asyncio
async def test_an_oversize_reply_still_ends_discovery_at_once():
    chat = Replies(" " * 1024, READ, MANIFEST, READ, ASSESSMENT)
    with pytest.raises(EnvironmentDiscoveryError, match="output limit exceeded"):
        await discover(chat, limits=DiscoveryLimits(max_output_bytes=100))
    assert len(chat.calls) == 1
