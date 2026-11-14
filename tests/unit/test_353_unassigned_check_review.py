"""Every declared check reaches a full review part after criterion mappings change."""

from __future__ import annotations

import copy
import json

import pytest
from coordinare_service_inference.environment.models import Command

from coordinare.services import environment_discovery as discovery
from tests.unit.scope_chat_fixture import discover_with_fixture_scope
from tests.unit.test_351_environment_discovery import (
    CAPABILITIES,
    COMMIT,
    PATH,
    SOURCE,
    ScriptedChat,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("placement", "identifier", "global_owner"),
    [
        ("unassigned", "c99-extra", True),
        ("mapped", "c99-extra", False),
        ("owned", "c00-extra", False),
        ("split_owned", "c00a-extra", False),
        ("global", "extra", True),
    ],
)
async def test_actual_part_review_routes_unassigned_full_records_to_global(
    monkeypatch, placement, identifier, global_owner,
):
    seen = {}
    original = discovery._review
    replies = ScriptedChat()
    parts = []

    async def chat(messages, **_kwargs):
        context = json.loads(messages[1]["content"])
        if "shared" in context and "part" in context.get("own", {}):
            parts.append(copy.deepcopy(context["own"]["part"]))
            return json.dumps(
                {
                    "assessment": {
                        "findings": [],
                        "decisions": [],
                        "capability_unavailable": False,
                        "verdict": "accept",
                        "rationale": "Synthetic review",
                        "sources": [PATH],
                    },
                },
            )
        return await replies(messages)

    async def remember(attempt, build, manifest):
        seen["attempt"] = attempt
        return await original(attempt, build, manifest)

    async def fetch(path):
        return SOURCE if path == PATH else "Synthetic evidence"

    monkeypatch.setattr(discovery, "_review", remember)
    manifest, _ = await discover_with_fixture_scope(
        symphony_name="suite",
        commit=COMMIT,
        paths=[PATH, "other/evidence"],
        fetch=fetch,
        chat=chat,
        capabilities=CAPABILITIES,
    )
    extra = Command(
        id=identifier,
        argv=["synthetic-tool", "--validate"],
        cwd="{scratch}",
        env={"CHECK_MODE": "synthetic"},
        depends_on=[manifest.checks[0].id],
        timeout_seconds=17,
        required=True,
    )
    mappings = copy.deepcopy(manifest.criterion_checks)
    if placement == "mapped":
        mappings[next(iter(mappings))].append(identifier)
    manifest = manifest.model_copy(
        update={"checks": [*manifest.checks, extra], "criterion_checks": mappings},
    )
    attempt = seen["attempt"]
    attempt.budget.scope_shards = {"synthetic_division": True}
    monkeypatch.setattr(discovery, "divided", lambda _scope: True)
    build = discovery._divided_construction(attempt.construction.require_scope(), attempt)
    await original(attempt, build, manifest)
    matching = [
        (part, check) for part in parts for check in part["checks"] if check["id"] == identifier
    ]
    assert matching, (
        "A retained check must have a full reviewer, including after its criterion link is released"
    )
    assert all(check == extra.model_dump(mode="json") for _part, check in matching)
    global_part = next(part for part in parts if "criteria" not in part)
    assert any(check["id"] == identifier for check in global_part["checks"]) is global_owner
    current_calls = len(parts)
    await original(attempt, build, manifest)
    assert len(parts) == current_calls
