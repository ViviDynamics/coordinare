"""080 — dual-model orchestration config catalogs.

Pins the Endpoint/ModelEndpoint/Mode models, their intra-model validators
(endpoint kind rules FR-007, strategy/field consistency FR-008), and the
ProjectConfiguration cross-reference + unique-name validation (FR-005). This
layer is additive — configs without catalogs are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from coordinare.config import (
    DEFAULT_THINK_ONCE_ERROR_PATTERN,
    Endpoint,
    Mode,
    ProjectConfiguration,
)


def _load_cfg(tmp_path: Path, **catalog: object) -> ProjectConfiguration:
    """Build a ProjectConfiguration from YAML with a minimal valid base + 080 catalogs.

    Uses from_yaml (not the env-driven constructor) so ambient COORDINARE_/.env
    vars don't interfere; mirrors the existing config-test convention.
    """
    base: dict[str, object] = {
        "project_name": "test",
        "github_org": "owner",
        "github_repo": "owner/repo",
        "github_project_number": 1,
        "github_auth": "pat",
        "github_token": "ghp_test",
        "human_reviewers": ["alice"],
    }
    base.update(catalog)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(base))
    return ProjectConfiguration.from_yaml(p)

# --- Endpoint kind rules (FR-007) -----------------------------------------


def test_self_hosted_endpoint_requires_base_url():
    Endpoint(name="spark", kind="litellm", base_url="http://spark:4000")  # ok
    with pytest.raises(ValidationError, match="requires base_url"):
        Endpoint(name="spark", kind="ollama")


def test_native_endpoint_must_not_set_base_url():
    e = Endpoint(name="anthropic-cloud", kind="anthropic", auth_env="ANTHROPIC_API_KEY")
    assert e.is_native is True
    assert e.base_url is None
    with pytest.raises(ValidationError, match="must not set base_url"):
        Endpoint(name="x", kind="openai", base_url="http://nope")


def test_self_hosted_kind_is_not_native():
    assert Endpoint(name="s", kind="vllm", base_url="http://v").is_native is False


# --- Mode strategy/field consistency (FR-008) ------------------------------


def test_single_mode_minimal_ok():
    m = Mode(name="single-x", strategy="single", tool="me-x")
    assert m.expose_plan_as == "thinking"  # default still set, harmless
    assert m.on_think_error == "fall_back_to_act"


@pytest.mark.parametrize("field", ["thinking", "classifier", "threshold", "invalidate_after_turns"])
def test_single_mode_rejects_multi_params(field):
    kwargs = {"name": "s", "strategy": "single", "tool": "me-x"}
    kwargs[field] = "me-y" if field in {"thinking", "classifier"} else (0.5 if field == "threshold" else 3)
    with pytest.raises(ValidationError, match="strategy 'single' must not set"):
        Mode(**kwargs)


def test_always_requires_thinking():
    Mode(name="a", strategy="always", tool="me-t", thinking="me-think")  # ok
    with pytest.raises(ValidationError, match="requires 'thinking'"):
        Mode(name="a", strategy="always", tool="me-t")


def test_conditional_requires_classifier_and_threshold():
    Mode(name="c", strategy="conditional", tool="t", thinking="th", classifier="cl", threshold=0.6)
    with pytest.raises(ValidationError, match="requires"):
        Mode(name="c", strategy="conditional", tool="t", thinking="th")  # no classifier/threshold


def test_non_conditional_rejects_classifier():
    with pytest.raises(ValidationError, match="conditional-only"):
        Mode(name="a", strategy="always", tool="t", thinking="th", classifier="cl")


def test_non_think_once_rejects_invalidate_params():
    with pytest.raises(ValidationError, match="think_once-only"):
        Mode(name="a", strategy="always", tool="t", thinking="th", invalidate_after_turns=5)


def test_think_once_accepts_its_params():
    m = Mode(
        name="to",
        strategy="think_once",
        tool="t",
        thinking="th",
        invalidate_after_turns=8,
        invalidate_on_error=True,
        error_pattern=r"(?i)boom",
    )
    assert m.invalidate_after_turns == 8


def test_threshold_range_enforced():
    with pytest.raises(ValidationError, match="threshold must be between"):
        Mode(name="c", strategy="conditional", tool="t", thinking="th", classifier="cl", threshold=1.5)


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        Mode(name="x", strategy="single", tool="t", bogus=True)


def test_default_error_pattern_constant_present():
    assert "exit code [1-9]" in DEFAULT_THINK_ONCE_ERROR_PATTERN


# --- ProjectConfiguration cross-reference + unique names (FR-005) ----------


def _valid_catalogs() -> dict:
    return {
        "endpoints": [
            {"name": "spark-litellm", "kind": "litellm", "base_url": "http://spark:4000"},
            {"name": "spark-ollama", "kind": "ollama", "base_url": "http://spark:11434"},
        ],
        "model_endpoints": [
            {"name": "gptoss-litellm", "endpoint": "spark-litellm", "model": "spark/gpt-oss:120b"},
            {"name": "gptoss-ollama", "endpoint": "spark-ollama", "model": "gpt-oss:120b"},
        ],
        "modes": [
            {"name": "always-gptoss", "strategy": "always",
             "thinking": "gptoss-litellm", "tool": "gptoss-ollama"},
        ],
    }


def test_valid_catalogs_load_and_resolve(tmp_path):
    cfg = _load_cfg(tmp_path, **_valid_catalogs())
    assert cfg.resolve_endpoint("spark-litellm").kind == "litellm"
    assert cfg.resolve_model_endpoint("gptoss-ollama").model == "gpt-oss:120b"
    assert cfg.resolve_mode("always-gptoss").strategy == "always"
    assert cfg.resolve_mode("missing") is None


def test_empty_catalogs_are_noop(tmp_path):
    cfg = _load_cfg(tmp_path)
    assert cfg.endpoints == [] and cfg.model_endpoints == [] and cfg.modes == []


def test_model_endpoint_dangling_endpoint_rejected(tmp_path):
    cat = _valid_catalogs()
    cat["model_endpoints"][0]["endpoint"] = "nope"
    with pytest.raises(ValidationError, match="unknown endpoint 'nope'"):
        _load_cfg(tmp_path, **cat)


def test_mode_dangling_model_endpoint_rejected(tmp_path):
    cat = _valid_catalogs()
    cat["modes"][0]["tool"] = "ghost"
    with pytest.raises(ValidationError, match="unknown model_endpoint 'ghost'"):
        _load_cfg(tmp_path, **cat)


def test_duplicate_names_rejected(tmp_path):
    cat = _valid_catalogs()
    cat["endpoints"].append({"name": "spark-litellm", "kind": "vllm", "base_url": "http://x"})
    with pytest.raises(ValidationError, match="endpoints contains duplicate name"):
        _load_cfg(tmp_path, **cat)
