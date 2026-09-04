"""083 — the `security` role must route through the gateway, not Ollama-direct.

Background (verified live, 083): the prior `security` route was `backend: pi` +
`mode: single-gptoss120-local-ollama`, i.e. gpt-oss:120b served through LiteLLM. LiteLLM's
streaming path hits the harmony tool_calls leak (bugs #17246 / #13300): gpt-oss's
content lands in the reasoning/tool_calls channel, leaving the `final` channel
EMPTY. The performer then retried and rubber-stamped the vulnerable PR
(`security_passed`, `findings: []`) — the worst possible failure for the security
gate. Re-routing the SAME model Ollama-direct via `openclaw` (the proven reviewer
route) neutralizes the leak: the role correctly FAILs the vuln PR and flags both
the SQLi f-string and the eval() as critical.

The model-name prefix is the routing switch: a prefixed ``<provider>/<model>`` goes
through the LiteLLM gateway, a bare ``<model>`` goes to the model host's Ollama
directly. Spec 122 reversed which of those is correct: LiteLLM returns clean
structured tool_calls server-side now, so the gateway leg is the one to be on, and
``backend: openclaw`` is what can take it.

Two layers of coverage:
  * ``test_resolver_*`` — a self-contained synthetic config proving the resolver
    distinguishes the two gpt-oss routes by model-name prefix. Always runs (CI-safe;
    no dependency on the gitignored production ``config.yaml``).
  * ``test_live_config_*`` — pins the SHIPPED ``config.yaml`` onto the gateway route.
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
        "endpoints": [{"name": "local-modelname", "kind": "openai"}],
        "model_endpoints": [
            {"name": "gptoss120-ollama", "endpoint": "local-modelname", "model": "gpt-oss:120b"},
            {"name": "gptoss120-local-ollama", "endpoint": "local-modelname", "model": "local/gpt-oss:120b"},
        ],
        "modes": [
            {"name": "single-gptoss120-ollama", "strategy": "single", "tool": "gptoss120-ollama"},
            {"name": "single-gptoss120-local-ollama", "strategy": "single", "tool": "gptoss120-local-ollama"},
        ],
        "performers": {"security": {"backend": "openclaw", "mode": security_mode}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(base))
    return ProjectConfiguration.from_yaml(p)


def test_resolver_ollama_mode_yields_unprefixed_model(tmp_path) -> None:
    """single-gptoss120-ollama → bare 'gpt-oss:120b' (the Ollama-direct leg)."""
    cfg = _resolver_cfg(tmp_path, security_mode="single-gptoss120-ollama")
    model = cfg.resolve_performer_dispatch_model("security")["model"]
    assert model == "gpt-oss:120b"
    assert not model.startswith("local/")


def test_resolver_selfhosted_mode_yields_prefixed_model(tmp_path) -> None:
    """Guard the discriminator: single-gptoss120-local-ollama → 'local/gpt-oss:120b' (gateway leg)."""
    cfg = _resolver_cfg(tmp_path, security_mode="single-gptoss120-local-ollama")
    assert cfg.resolve_performer_dispatch_model("security")["model"] == "local/gpt-oss:120b"


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
    """`pi` hardwires its own base URL; only openclaw can be pointed at either leg."""
    assert _raw_live()["performers"]["security"]["backend"] == "openclaw"


@_live
def test_live_config_security_routes_through_the_gateway() -> None:
    """The shipped security mode resolves to a gateway-served model, not Ollama-direct.

    083 originally pinned this to a bare 'gpt-oss:120b' because LiteLLM's
    harmony→tool_calls handling leaked raw harmony text (#17246/#13300), which
    left the `final` channel empty and let the security role rubber-stamp a
    vulnerable PR. Spec 122 fixed that upstream and moved security onto the
    gateway, so the leg under test flipped.

    What is pinned is the leg, not the model. The prefix IS the switch: a
    prefixed model resolves through LiteLLM, a bare one goes to Ollama directly.
    Naming the model as well pinned a fleet. This asserted 'gpt-oss:120b' and
    began failing the day the operator's fleet consolidated onto a single
    self-hosted model, reporting a routing regression that had not happened,
    on a config that is gitignored and therefore invisible to CI.
    """
    cfg = _raw_live()
    mode = next(m for m in cfg["modes"] if m["name"] == cfg["performers"]["security"]["mode"])
    me = next(m for m in cfg["model_endpoints"] if m["name"] == mode["tool"])
    provider, sep, model = me["model"].partition("/")
    assert sep and provider and model, (
        f"shipped security resolves to {me['model']!r}, which carries no provider "
        "prefix and so routes Ollama-direct. 122 requires the gateway leg: expected "
        "'<provider>/<model>'."
    )
