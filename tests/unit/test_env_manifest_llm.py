"""Unit tests for the env-manifest README LLM pass (077)."""

from __future__ import annotations

import pytest

from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.services.env_manifest_llm import (
    enrich_from_readme,
    parse_system_items,
)


class TestParseSystemItems:
    def test_valid_json(self) -> None:
        reply = '{"system": [{"name": "chromium", "binary": "chromium"}]}'
        items = parse_system_items(reply, known=set())
        assert [i.name for i in items] == ["chromium"]
        assert items[0].kind == "system"
        assert items[0].source == "README.md"

    def test_binary_preferred_over_name(self) -> None:
        reply = '{"system": [{"name": "postgresql-client", "binary": "psql"}]}'
        items = parse_system_items(reply, known=set())
        assert items[0].name == "psql"

    def test_fenced_json(self) -> None:
        reply = '```json\n{"system": [{"binary": "redis-cli"}]}\n```'
        items = parse_system_items(reply, known=set())
        assert [i.name for i in items] == ["redis-cli"]

    def test_skips_known(self) -> None:
        reply = '{"system": [{"binary": "ruby"}, {"binary": "chromium"}]}'
        items = parse_system_items(reply, known={"ruby"})
        assert [i.name for i in items] == ["chromium"]

    def test_dedup(self) -> None:
        reply = '{"system": [{"binary": "chromium"}, {"binary": "chromium"}]}'
        items = parse_system_items(reply, known=set())
        assert [i.name for i in items] == ["chromium"]

    def test_malformed_returns_empty(self) -> None:
        assert parse_system_items("not json at all", known=set()) == []
        assert parse_system_items("", known=set()) == []
        assert parse_system_items('{"system": "nope"}', known=set()) == []

    def test_prose_wrapped_json(self) -> None:
        reply = 'Here is the result:\n{"system": [{"binary": "chromium"}]}\nDone.'
        items = parse_system_items(reply, known=set())
        assert [i.name for i in items] == ["chromium"]


class TestEnrichFromReadme:
    def _manifest(self) -> EnvManifest:
        return EnvManifest(
            symphony_name="sym",
            items=[ManifestItem(name="ruby", kind="runtime", version="3.4.2", source=".ruby-version")],
        )

    @pytest.mark.asyncio
    async def test_merges_system_items(self) -> None:
        async def fake_chat(messages: list[dict[str, str]]) -> str:
            return '{"system": [{"binary": "chromium"}]}'

        out = await enrich_from_readme(self._manifest(), "install chromium", fake_chat)
        assert {i.name for i in out.items} == {"ruby", "chromium"}
        assert out.llm_derived is True

    @pytest.mark.asyncio
    async def test_none_chat_is_noop(self) -> None:
        m = self._manifest()
        out = await enrich_from_readme(m, "install chromium", None)
        assert out is m

    @pytest.mark.asyncio
    async def test_empty_readme_is_noop(self) -> None:
        async def fake_chat(messages: list[dict[str, str]]) -> str:  # pragma: no cover
            raise AssertionError("should not be called")

        m = self._manifest()
        out = await enrich_from_readme(m, "   ", fake_chat)
        assert out is m

    @pytest.mark.asyncio
    async def test_chat_failure_is_noop(self) -> None:
        async def boom(messages: list[dict[str, str]]) -> str:
            raise RuntimeError("LLM down")

        m = self._manifest()
        out = await enrich_from_readme(m, "install chromium", boom)
        assert out.items == m.items
        assert out.llm_derived is False

    @pytest.mark.asyncio
    async def test_no_new_items_keeps_manifest(self) -> None:
        async def fake_chat(messages: list[dict[str, str]]) -> str:
            return '{"system": []}'

        m = self._manifest()
        out = await enrich_from_readme(m, "no system deps", fake_chat)
        assert out.items == m.items
        assert out.llm_derived is False
