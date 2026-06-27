"""083 — the `security` role must route gpt-oss Ollama-DIRECT, not via LiteLLM.

Background (verified live, 083): the prior `security` route was `backend: pi` +
`mode: single-gptoss120-spark`, i.e. gpt-oss:120b served through LiteLLM. LiteLLM's
streaming path hits the harmony tool_calls leak (bugs #17246 / #13300): gpt-oss's
content lands in the reasoning/tool_calls channel, leaving the `final` channel
EMPTY. The performer then retried and rubber-stamped the vulnerable PR
(`security_passed`, `findings: []`) — the worst possible failure for the security
gate. Re-routing the SAME model Ollama-direct via `openclaw` (the proven reviewer
route) neutralizes the leak: the role correctly FAILs the vuln PR and flags both
the SQLi f-string and the eval() as critical.

The model-name prefix is the routing switch: ``spark/gpt-oss:120b`` → the LiteLLM
``spark/*`` wildcard (leaky streaming); bare ``gpt-oss:120b`` → the Spark's Ollama
directly (clean). And ``backend: pi`` hardwires PI_PROVIDER_BASE_URL to LiteLLM, so
only ``openclaw`` (OPENCLAW_PROVIDER_BASE_URL → Ollama) can take the clean route.

Two layers of coverage:
  * ``test_resolver_*`` — a self-contained synthetic config proving the resolver
    distinguishes the two gpt-oss routes by model-name prefix. Always runs (CI-safe;
    no dependency on the gitignored production ``config.yaml``).
  * ``test_live_config_*`` — pins the SHIPPED ``config.yaml`` onto the clean route.
    Skipped when ``config.yaml`` is absent (e.g. CI), where it cannot apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinare.config import ProjectConfiguration

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "config.yaml"


# --------------------------------------------------------------------------- #
# Layer 1 — CI-safe resolver invariant (synthetic config, always runs).
# --------------------------------------------------------------------------- #
def _resolver_cfg(tmp_path: Path, *, security_mode: str) -> ProjectConfiguration:
    """A minimal config carrying BOTH gpt-oss routes; security uses ``security_mode``."""
    base = {
        "project_name": "t", "github_org": "o", "github_project_number": 1,
        "github_token": "ghp_x", "human_reviewers": ["a"],
        "endpoints": [{"name": "spark-modelname", "kind": "openai"}],
        "model_endpoints": [
            {"name": "gptoss120-ollama", "endpoint": "spark-modelname", "model": "gpt-oss:120b"},
            {"name": "gptoss120-spark", "endpoint": "spark-modelname", "model": "spark/gpt-oss:120b"},
        ],
        "modes": [
            {"name": "single-gptoss120-ollama", "strategy": "single", "tool": "gptoss120-ollama"},
            {"name": "single-gptoss120-spark", "strategy": "single", "tool": "gptoss120-spark"},
        ],
        "performers": {"security": {"backend": "openclaw", "mode": security_mode}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(base))
    return ProjectConfiguration.from_yaml(p)


def test_resolver_ollama_mode_yields_unprefixed_model(tmp_path) -> None:
    """single-gptoss120-ollama → bare 'gpt-oss:120b' (the clean Ollama-direct leg)."""
    cfg = _resolver_cfg(tmp_path, security_mode="single-gptoss120-ollama")
    model = cfg.resolve_performer_dispatch_model("security")["model"]
    assert model == "gpt-oss:120b"
    assert not model.startswith("spark/")


def test_resolver_spark_mode_yields_prefixed_model(tmp_path) -> None:
    """Guard the discriminator: single-gptoss120-spark → 'spark/gpt-oss:120b' (leaky leg)."""
    cfg = _resolver_cfg(tmp_path, security_mode="single-gptoss120-spark")
    assert cfg.resolve_performer_dispatch_model("security")["model"] == "spark/gpt-oss:120b"


# --------------------------------------------------------------------------- #
# Layer 2 — pin the SHIPPED config onto the clean route (skips if absent).
# --------------------------------------------------------------------------- #
_live = pytest.mark.skipif(
    not CONFIG_PATH.exists(),
    reason="production config.yaml absent (gitignored; not present in CI)",
)


def _raw_live() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


@_live
def test_live_config_security_uses_openclaw_backend() -> None:
    """`pi` hardwires LiteLLM (leaky); the shipped security role must be on openclaw."""
    assert _raw_live()["performers"]["security"]["backend"] == "openclaw"


@_live
def test_live_config_security_resolves_litellm_gptoss() -> None:
    """The shipped security mode resolves to the LiteLLM-served 'spark/gpt-oss:120b'.

    083 originally pinned this to bare 'gpt-oss:120b' (Ollama-direct) because the
    LiteLLM harmony→tool_calls handling leaked raw harmony text (#17246/#13300).
    Spec 122 supersedes that: LiteLLM now returns clean structured tool_calls
    server-side, so security (openclaw) routes DIRECT to LiteLLM on the 'spark/'
    model — validated live (openclaw on spark/gpt-oss:120b emits a valid verdict).
    """
    cfg = _raw_live()
    mode = next(m for m in cfg["modes"] if m["name"] == cfg["performers"]["security"]["mode"])
    me = next(m for m in cfg["model_endpoints"] if m["name"] == mode["tool"])
    assert me["model"] == "spark/gpt-oss:120b", (
        f"shipped security resolves to {me['model']!r}; expected the LiteLLM-served "
        "'spark/gpt-oss:120b' (122: the harmony leak that forced Ollama-direct is "
        "fixed upstream)"
    )
