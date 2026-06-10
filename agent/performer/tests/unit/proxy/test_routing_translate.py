"""084 US3 — translate routing-table load + validation (T018).

TDD: written for the US3 routing surface. Covers FR-006/FR-008/FR-009 from
``spec.md`` and ``contracts/``:

* a valid ``translate`` entry (``wire_format: openai``) loads — both as a bare
  ``TargetDescriptor`` and through ``RoutingTable.from_yaml_file`` — with or
  without optional response normalizers (translation alone is a valid target,
  Rule T2);
* ``translate`` + ``wire_format: anthropic`` is rejected at LOAD with an
  actionable message naming the strategy/wire_format pair and BOTH fields
  (Rule T1, FR-008) — never black-holed mid-lifecycle;
* a missing / unreadable / malformed table fails closed with a message naming
  the path AND the ``SELFHOSTED_ROUTING_CONFIG`` env var that points at it
  (FR-009).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from performer.proxy.routing import RoutingTable, TargetDescriptor


# --- a valid translate entry loads ------------------------------------------ #


def test_translate_target_openai_wire_loads():
    target = TargetDescriptor(
        base_url="http://ollama:11434",
        wire_format="openai",
        strategy="translate",
    )
    assert target.strategy == "translate"
    assert target.wire_format == "openai"
    # translation alone (no normalizers) is a valid target (Rule T2)
    assert target.normalizers == []


def test_translate_target_with_optional_normalizers_loads():
    target = TargetDescriptor(
        base_url="http://ollama:11434",
        wire_format="openai",
        strategy="translate",
        normalizers=["harmony_tool_calls", "strip_reasoning"],
    )
    assert target.normalizers == ["harmony_tool_calls", "strip_reasoning"]


def test_translate_target_with_upstream_model_loads():
    """084 Ollama-compat: a translate entry may declare an ``upstream_model`` so
    the OpenAI-wire upstream (e.g. Ollama) receives its own model name rather
    than the Anthropic name the CLI was started with. Default is ``None``."""
    target = TargetDescriptor(
        base_url="http://ollama:11434/v1",
        wire_format="openai",
        strategy="translate",
        upstream_model="gpt-oss:120b",
    )
    assert target.upstream_model == "gpt-oss:120b"


def test_translate_target_upstream_model_defaults_none():
    target = TargetDescriptor(
        base_url="http://ollama:11434",
        wire_format="openai",
        strategy="translate",
    )
    assert target.upstream_model is None


def test_translate_entry_with_upstream_model_loads_from_yaml_file(tmp_path):
    table_yaml = tmp_path / "routing.yaml"
    table_yaml.write_text(
        "selfhosted_routing:\n"
        "  - backend: claude_code\n"
        "    model: claude-sonnet-4-5\n"
        "    target:\n"
        "      base_url: http://ollama:11434/v1\n"
        "      wire_format: openai\n"
        "      strategy: translate\n"
        "      upstream_model: gpt-oss:120b\n",
        encoding="utf-8",
    )
    table = RoutingTable.from_yaml_file(table_yaml)
    target = table.resolve("claude_code", "claude-sonnet-4-5")
    assert target is not None
    assert target.upstream_model == "gpt-oss:120b"


def test_translate_entry_loads_from_yaml_file(tmp_path):
    table_yaml = tmp_path / "routing.yaml"
    table_yaml.write_text(
        "selfhosted_routing:\n"
        "  - backend: claude_code\n"
        "    model: gpt-oss:120b\n"
        "    target:\n"
        "      base_url: http://ollama:11434\n"
        "      wire_format: openai\n"
        "      strategy: translate\n"
        "      normalizers: [harmony_tool_calls]\n",
        encoding="utf-8",
    )
    table = RoutingTable.from_yaml_file(table_yaml)
    target = table.resolve("claude_code", "gpt-oss:120b")
    assert target is not None
    assert target.strategy == "translate"
    assert target.wire_format == "openai"
    assert target.normalizers == ["harmony_tool_calls"]


# --- Rule T1: translate + anthropic wire is rejected at load (FR-008) -------- #


def test_translate_anthropic_wire_rejected_with_actionable_message():
    with pytest.raises(ValidationError) as exc:
        TargetDescriptor(
            base_url="http://ollama:11434",
            wire_format="anthropic",
            strategy="translate",
        )
    msg = str(exc.value)
    # names both fields and the contradictory pair (Rule T1)
    assert "translate" in msg
    assert "wire_format" in msg
    assert "openai" in msg
    assert "anthropic" in msg


def test_translate_anthropic_wire_rejected_from_yaml_file(tmp_path):
    table_yaml = tmp_path / "routing.yaml"
    table_yaml.write_text(
        "selfhosted_routing:\n"
        "  - backend: claude_code\n"
        "    model: m\n"
        "    target:\n"
        "      base_url: http://ollama:11434\n"
        "      wire_format: anthropic\n"
        "      strategy: translate\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        RoutingTable.from_yaml_file(table_yaml)
    msg = str(exc.value)
    assert "translate" in msg and "wire_format" in msg


def test_translate_unknown_normalizer_rejected(tmp_path):
    with pytest.raises(ValidationError) as exc:
        TargetDescriptor(
            base_url="http://ollama:11434",
            wire_format="openai",
            strategy="translate",
            normalizers=["does_not_exist"],
        )
    msg = str(exc.value)
    assert "does_not_exist" in msg


# --- FR-009: missing / malformed table fails closed naming path + env var ---- #


def test_missing_table_fails_closed_naming_path_and_env_var(tmp_path):
    missing = tmp_path / "absent.yaml"
    with pytest.raises(ValueError) as exc:
        RoutingTable.from_yaml_file(missing)
    msg = str(exc.value)
    assert str(missing) in msg
    assert "SELFHOSTED_ROUTING_CONFIG" in msg


def test_malformed_yaml_fails_closed_naming_path(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("selfhosted_routing: [unterminated\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        RoutingTable.from_yaml_file(bad)
    assert str(bad) in str(exc.value)
