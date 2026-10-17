"""Large arbitrary layouts remain navigable without flooding every model turn."""
from __future__ import annotations

import hashlib
import json

import pytest

from coordinare.services.environment_discovery import DiscoveryLimits, EnvironmentDiscoveryError
from coordinare.services.environment_inspection import SourceInspection


async def fetch(path):
    return "evidence for " + path


def test_100000_paths_prompt_bounded_and_all_pages_reachable():
    paths = [f"file-{index:06}" for index in range(100_000)]
    inspection = SourceInspection(paths, fetch, DiscoveryLimits())
    seen = []
    cursor = 0
    while True:
        page = inspection.list_directory(cursor=cursor)
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 32 * 1024
        assert len(page["entries"]) <= 200
        seen.extend(entry["path"] for entry in page["entries"])
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert seen == paths
    assert len(json.dumps(inspection.list_directory())) < 32 * 1024


@pytest.mark.asyncio
async def test_nested_unknown_toolchain_discovery():
    inspection = SourceInspection(["alien/frob/usage", "other/zigzag", "guide"], fetch, DiscoveryLimits())
    root = inspection.list_directory()
    assert root["entries"] == [{"path": "alien", "kind": "directory"},
        {"path": "guide", "kind": "file"}, {"path": "other", "kind": "directory"}]
    assert inspection.list_directory("alien")["entries"] == [{"path": "alien/frob", "kind": "directory"}]
    result = await inspection.read_files(["alien/frob/usage", "guide"])
    assert [item["path"] for item in result["files"]] == ["alien/frob/usage", "guide"]
    assert result["files"][0]["sha256"] == hashlib.sha256(b"evidence for alien/frob/usage").hexdigest()
    assert inspection.inputs == {item["path"]: item["sha256"] for item in result["files"]}


@pytest.mark.asyncio
async def test_batch_limit_and_duplicate_reads_count():
    inspection = SourceInspection([str(i) for i in range(9)], fetch, DiscoveryLimits(max_reads=2))
    with pytest.raises(EnvironmentDiscoveryError):
        await inspection.read_files([str(i) for i in range(9)])
    with pytest.raises(EnvironmentDiscoveryError):
        await inspection.read_files(["0", "0"])
    await inspection.read_files(["0"])
    await inspection.read_files(["0"])
    with pytest.raises(EnvironmentDiscoveryError, match="read limit"):
        await inspection.read_files(["0"])


def test_added_deleted_paths_invalidate_inventory():
    original = SourceInspection(["a", "b"], fetch, DiscoveryLimits())
    added = SourceInspection(["a", "b", "unread"], fetch, DiscoveryLimits())
    deleted = SourceInspection(["a"], fetch, DiscoveryLimits())
    reordered = SourceInspection(["b", "a"], fetch, DiscoveryLimits())
    assert len({original.inventory_digest, added.inventory_digest, deleted.inventory_digest}) == 3
    assert original.inventory_digest == reordered.inventory_digest


@pytest.mark.asyncio
async def test_batch_snapshot_drift_holds():
    values = iter(["first", "changed"])

    async def changing(path):
        return next(values)

    inspection = SourceInspection(["a"], changing, DiscoveryLimits())
    await inspection.read_files(["a"])
    with pytest.raises(EnvironmentDiscoveryError, match="snapshot"):
        await inspection.read_files(["a"])


def test_long_multibyte_names_respect_page_bytes():
    paths = ["é" * 200 + str(i) for i in range(250)]
    inspection = SourceInspection(paths, fetch, DiscoveryLimits())
    page = inspection.list_directory()
    assert len(page["entries"]) < 200
    assert len(json.dumps(page, ensure_ascii=False).encode()) <= 32 * 1024
    assert page["next_cursor"] is not None
    for kwargs in ({"path": ".."}, {"path": "missing"}, {"cursor": -1}, {"cursor": True}, {"cursor": 251}):
        with pytest.raises(EnvironmentDiscoveryError):
            inspection.list_directory(**kwargs)


@pytest.mark.asyncio
async def test_large_inventory_is_not_repeated_in_model_prompt():
    from coordinare.services.environment_discovery import discover_and_review
    from tests.unit.test_351_environment_discovery import (
        CAPABILITIES,
        COMMIT,
        PATH,
        SOURCE,
        ScriptedChat,
        candidate,
    )

    paths = [PATH, "other/evidence", *[f"file-{index:06}" for index in range(99_998)]]
    inventory_digest = hashlib.sha256(json.dumps(sorted(paths), separators=(",", ":")).encode()).hexdigest()
    manifest = candidate()
    manifest["inventory_digest"] = inventory_digest
    scripted = ScriptedChat(manifest=manifest)

    async def chat(messages):
        reply = json.loads(await scripted(messages))
        if "assessment" in reply:
            reply["assessment"]["inventory_digest"] = inventory_digest
        return json.dumps(reply)

    async def source(path):
        return SOURCE

    accepted, review = await discover_and_review(symphony_name="suite", commit=COMMIT,
        paths=paths, fetch=source, chat=chat, capabilities=CAPABILITIES)
    assert accepted.inventory_digest == review.inventory_digest == inventory_digest
    initial = json.loads(scripted.calls[0][1]["content"])
    assert "paths" not in initial
    assert "file-099997" not in json.dumps(initial)
    assert len(json.dumps(initial["root_directory"]).encode()) <= 32 * 1024
