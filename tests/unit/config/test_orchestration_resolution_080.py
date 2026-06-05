"""080 — coordinare resolves a role's mode into the dispatch orchestration block (T008)."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinare.config import ProjectConfiguration


def _cfg(tmp_path: Path, modes: list[dict], performers: dict) -> ProjectConfiguration:
    base = {
        "project_name": "t", "github_org": "o", "github_project_number": 1,
        "github_token": "ghp_x", "human_reviewers": ["a"],
        "endpoints": [
            {"name": "spark-litellm", "kind": "litellm", "base_url": "http://spark:4000",
             "auth_env": "LITELLM_PROXY_AUTH_TOKEN"},
            {"name": "spark-ollama", "kind": "ollama", "base_url": "http://spark:11434"},
            {"name": "anthropic-cloud", "kind": "anthropic", "auth_env": "ANTHROPIC_API_KEY"},
        ],
        "model_endpoints": [
            {"name": "gptoss-litellm", "endpoint": "spark-litellm", "model": "spark/gpt-oss:120b"},
            {"name": "gptoss-ollama", "endpoint": "spark-ollama", "model": "gpt-oss:120b"},
            {"name": "qwen-litellm", "endpoint": "spark-litellm", "model": "spark/qwen3.6:35b"},
            {"name": "sonnet-native", "endpoint": "anthropic-cloud", "model": "claude-sonnet-4-5"},
        ],
        "modes": modes,
        "performers": performers,
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(base))
    return ProjectConfiguration.from_yaml(p)


def test_single_mode_has_no_orchestration(tmp_path):
    cfg = _cfg(
        tmp_path,
        [{"name": "single-sonnet", "strategy": "single", "tool": "sonnet-native"}],
        {"reviewer": {"backend": "claude_code", "mode": "single-sonnet"}},
    )
    assert cfg.resolve_performer_orchestration("reviewer") is None


def test_always_orchestration_resolves_both_legs(tmp_path):
    cfg = _cfg(
        tmp_path,
        [{"name": "always", "strategy": "always", "thinking": "gptoss-litellm",
          "tool": "gptoss-ollama", "expose_plan_as": "thinking"}],
        {"architect": {"backend": "codex", "mode": "always"}},
    )
    orch = cfg.resolve_performer_orchestration("architect")
    assert orch["strategy"] == "always"
    assert orch["thinking"]["model"] == "spark/gpt-oss:120b"
    assert orch["thinking"]["base_url"] == "http://spark:4000"
    assert orch["thinking"]["auth_style"] == "bearer"
    assert orch["thinking"]["wire_format"] == "openai"
    assert orch["tool"]["model"] == "gpt-oss:120b"
    assert orch["tool"]["base_url"] == "http://spark:11434"  # ollama-direct
    assert orch["expose_plan_as"] == "thinking"
    assert orch["on_think_error"] == "fall_back_to_act"
    assert "threshold" not in orch  # not conditional


def test_conditional_includes_classifier_and_threshold(tmp_path):
    cfg = _cfg(
        tmp_path,
        [{"name": "cond", "strategy": "conditional", "thinking": "gptoss-litellm",
          "tool": "qwen-litellm", "classifier": "qwen-litellm", "threshold": 0.7}],
        {"implementer": {"backend": "codex", "mode": "cond"}},
    )
    orch = cfg.resolve_performer_orchestration("implementer")
    assert orch["strategy"] == "conditional"
    assert orch["classifier"]["model"] == "spark/qwen3.6:35b"
    assert orch["threshold"] == 0.7


def test_think_once_includes_invalidation_params(tmp_path):
    cfg = _cfg(
        tmp_path,
        [{"name": "to", "strategy": "think_once", "thinking": "gptoss-litellm",
          "tool": "qwen-litellm", "invalidate_after_turns": 8, "invalidate_on_error": True}],
        {"qa": {"backend": "claude_code", "mode": "to"}},
    )
    orch = cfg.resolve_performer_orchestration("qa")
    assert orch["strategy"] == "think_once"
    assert orch["invalidate_after_turns"] == 8
    assert orch["invalidate_on_error"] is True


def test_native_thinking_leg_uses_x_api_key_and_anthropic_wire(tmp_path):
    cfg = _cfg(
        tmp_path,
        [{"name": "mixed", "strategy": "always", "thinking": "sonnet-native",
          "tool": "qwen-litellm"}],
        {"architect": {"backend": "codex", "mode": "mixed"}},
    )
    orch = cfg.resolve_performer_orchestration("architect")
    assert orch["thinking"]["wire_format"] == "anthropic"
    assert orch["thinking"]["auth_style"] == "x-api-key"
    assert orch["thinking"]["base_url"] is None  # native → no override
