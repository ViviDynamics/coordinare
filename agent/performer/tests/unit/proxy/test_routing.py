"""078 — routing-table models + resolution (T009).

Covers ``(backend, model)`` resolution, kebab→snake backend normalization, the
missing-entry → ``None`` no-op contract (SC-003), and the load-time validation
errors that make a malformed target fail fast at config load rather than
black-holing a card mid-lifecycle.

Note: ``normalize`` targets require every declared key to be present in
``NORMALIZER_REGISTRY``. The registry is populated in US1 (T017); until then the
only constructible strategy is ``reroute``. These tests therefore monkeypatch a
stub normalizer into the registry to exercise the ``normalize`` validation paths
without depending on US1 landing first.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from performer.proxy import normalizers as normalizers_mod
from performer.proxy.routing import (
    RoutingEntry,
    RoutingTable,
    TargetDescriptor,
)


class _StubNormalizer:
    key = "stub_norm"

    def normalize_json(self, body):
        return body

    def sse_filter(self):  # pragma: no cover - not exercised here
        raise NotImplementedError


@pytest.fixture()
def registered_stub(monkeypatch):
    """Register a stub normalizer key so ``normalize`` targets validate."""
    patched = dict(normalizers_mod.NORMALIZER_REGISTRY)
    patched["stub_norm"] = _StubNormalizer()
    # routing.py imported the registry by reference; patch the object it sees.
    monkeypatch.setattr(normalizers_mod, "NORMALIZER_REGISTRY", patched)
    import performer.proxy.routing as routing_mod

    monkeypatch.setattr(routing_mod, "NORMALIZER_REGISTRY", patched)
    return patched


# --- resolution ---------------------------------------------------------- #


def _reroute_target(base_url="http://ollama:11434"):
    return TargetDescriptor(
        base_url=base_url, wire_format="openai", strategy="reroute"
    )


def test_resolve_hit_returns_target():
    target = _reroute_target()
    table = RoutingTable(
        entries=[RoutingEntry(backend="openclaw", model="gpt-oss:120b", target=target)]
    )
    assert table.resolve("openclaw", "gpt-oss:120b") is target


def test_resolve_miss_returns_none():
    table = RoutingTable(
        entries=[
            RoutingEntry(backend="openclaw", model="gpt-oss:120b", target=_reroute_target())
        ]
    )
    # SC-003: an unrouted pair is a byte-for-byte no-op (None), not an error.
    assert table.resolve("codex", "gpt-oss:120b") is None
    assert table.resolve("openclaw", "some-other-model") is None


def test_resolve_empty_table_returns_none():
    assert RoutingTable().resolve("codex", "anything") is None


def test_resolve_kebab_snake_backend_normalized():
    """A routing entry written ``claude-code`` resolves a dispatch for ``claude_code``."""
    target = _reroute_target()
    table = RoutingTable(
        entries=[RoutingEntry(backend="claude-code", model="m", target=target)]
    )
    assert table.resolve("claude_code", "m") is target
    assert table.resolve("Claude-Code", "m") is target


# --- load-time validation ------------------------------------------------ #


def test_reroute_with_normalizers_rejected():
    with pytest.raises(ValidationError, match="reroute is not a shim"):
        TargetDescriptor(
            base_url="http://ollama:11434",
            wire_format="openai",
            strategy="reroute",
            normalizers=["stub_norm"],
        )


def test_normalize_without_normalizers_rejected():
    with pytest.raises(ValidationError, match="at least one normalizer"):
        TargetDescriptor(
            base_url="http://litellm:4000",
            wire_format="openai",
            strategy="normalize",
            normalizers=[],
        )


def test_normalize_with_unknown_key_rejected(registered_stub):
    with pytest.raises(ValidationError, match="unknown normalizer key"):
        TargetDescriptor(
            base_url="http://litellm:4000",
            wire_format="openai",
            strategy="normalize",
            normalizers=["stub_norm", "does_not_exist"],
        )


def test_normalize_with_registered_key_accepted(registered_stub):
    target = TargetDescriptor(
        base_url="http://litellm:4000",
        wire_format="openai",
        strategy="normalize",
        normalizers=["stub_norm"],
    )
    assert target.normalizers == ["stub_norm"]


def test_missing_base_url_rejected():
    with pytest.raises(ValidationError):
        TargetDescriptor(wire_format="openai", strategy="reroute")


def test_empty_base_url_rejected():
    with pytest.raises(ValidationError):
        TargetDescriptor(base_url="", wire_format="openai", strategy="reroute")


def test_bad_wire_format_rejected():
    with pytest.raises(ValidationError):
        TargetDescriptor(
            base_url="http://x", wire_format="grpc", strategy="reroute"
        )


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        TargetDescriptor(
            base_url="http://x",
            wire_format="openai",
            strategy="reroute",
            bogus="nope",
        )


# --- from_yaml_file: the mounted-YAML activation surface (078 wiring) ---------


def test_from_yaml_file_loads_reroute_entry(tmp_path):
    """A valid mounted table parses into a RoutingTable that resolves its pair."""
    cfg = tmp_path / "routing.yaml"
    cfg.write_text(
        "entries:\n"
        "  - backend: openclaw\n"
        "    model: gpt-oss-120b\n"
        "    target:\n"
        "      base_url: http://ollama:11434\n"
        "      wire_format: openai\n"
        "      strategy: reroute\n",
        encoding="utf-8",
    )
    table = RoutingTable.from_yaml_file(cfg)
    target = table.resolve("openclaw", "gpt-oss-120b")
    assert target is not None
    assert target.base_url == "http://ollama:11434"
    assert target.strategy == "reroute"


def test_from_yaml_file_accepts_bare_list_root(tmp_path):
    """A top-level list of entries is accepted as shorthand for {entries: [...]}."""
    cfg = tmp_path / "routing.yaml"
    cfg.write_text(
        "- backend: codex\n"
        "  model: qwen3\n"
        "  target:\n"
        "    base_url: http://ollama:11434\n"
        "    wire_format: openai\n"
        "    strategy: reroute\n",
        encoding="utf-8",
    )
    table = RoutingTable.from_yaml_file(cfg)
    assert table.resolve("codex", "qwen3") is not None


def test_from_yaml_file_empty_doc_is_empty_table(tmp_path):
    """An empty file (no entries) is a valid no-op table, not an error."""
    cfg = tmp_path / "routing.yaml"
    cfg.write_text("", encoding="utf-8")
    table = RoutingTable.from_yaml_file(cfg)
    assert table.entries == []
    assert table.resolve("openclaw", "gpt-oss-120b") is None


def test_from_yaml_file_missing_path_raises(tmp_path):
    """A non-empty-but-missing path fails fast rather than silently no-op'ing."""
    with pytest.raises(FileNotFoundError):
        RoutingTable.from_yaml_file(tmp_path / "does-not-exist.yaml")


def test_from_yaml_file_malformed_yaml_raises(tmp_path):
    cfg = tmp_path / "routing.yaml"
    cfg.write_text("entries: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid YAML"):
        RoutingTable.from_yaml_file(cfg)


def test_from_yaml_file_invalid_target_fails_fast(tmp_path):
    """A reroute target carrying normalizers violates the strategy rule and must
    fail at load (FR-078-5) — not be accepted then break a card mid-lifecycle."""
    cfg = tmp_path / "routing.yaml"
    cfg.write_text(
        "entries:\n"
        "  - backend: openclaw\n"
        "    model: gpt-oss-120b\n"
        "    target:\n"
        "      base_url: http://ollama:11434\n"
        "      wire_format: openai\n"
        "      strategy: reroute\n"
        "      normalizers: [harmony_tool_calls]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="is invalid"):
        RoutingTable.from_yaml_file(cfg)


def test_from_yaml_file_non_mapping_root_rejected(tmp_path):
    cfg = tmp_path / "routing.yaml"
    cfg.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping or a list"):
        RoutingTable.from_yaml_file(cfg)
