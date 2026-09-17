"""Shared pytest fixtures for the coordinare unit suite (spec 081-config-ui).

Provides representative temp ``config.yaml`` and routing-table YAML fixtures plus
a performer-endpoint config that mounts the routing YAML, so the descriptor /
write-service / routing-service tests run hermetically against on-disk files.

Coordinare-suite isolation: this conftest deliberately imports nothing from the
performer test tree (the two suites run separately — conftest collision).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

# A representative multi-symphony config.yaml exercising the full editable
# surface: global tuning, personas, spec-080 catalogs (endpoints/model_endpoints/
# modes), performers, and a performer endpoint that mounts a routing YAML.
REPRESENTATIVE_CONFIG: dict = {
    "github_org": "acme",
    "github_token": "${COORDINARE_GITHUB_TOKEN}",
    "human_reviewers": ["alice"],
    "poll_interval_seconds": 30,
    "log_level": "info",
    "max_concurrent_cards": 3,
    "personas": {
        "reviewer": {"instructions": "Review carefully."},
    },
    "endpoints": [
        {"name": "local-vllm", "kind": "vllm", "base_url": "http://localhost:8000"},
        {"name": "openai-native", "kind": "openai", "auth_env": "OPENAI_API_KEY"},
    ],
    "model_endpoints": [
        {"name": "qwen-coder", "endpoint": "local-vllm", "model": "qwen2.5-coder"},
    ],
    "modes": [
        {"name": "single-qwen", "strategy": "single", "tool": "qwen-coder"},
    ],
    "symphonies": [
        {"name": "demo", "github_project_number": 100},
    ],
    "orchestra": {"mode": "shared_pool"},
    "performer_endpoints": [
        {
            "id": "vllm-box",
            "mode": "ephemeral",
            "roles": ["implementer"],
            "image": "performer:full",
            "volumes": [
                {
                    "host_path": "/host/routing.yaml",
                    "container_path": "/devenv/routing.yaml",
                    "mode": "ro",
                },
            ],
            "env": {"SELFHOSTED_ROUTING_CONFIG": "/devenv/routing.yaml"},
        },
    ],
}

REPRESENTATIVE_ROUTING: dict = {
    "entries": [
        {
            "backend": "vllm",
            "model": "qwen2.5-coder",
            "target": {
                "base_url": "http://localhost:8000/v1",
                "wire_format": "openai",
                "strategy": "normalize",
                "normalizers": ["harmony_tool_calls"],
            },
        },
    ],
}


@pytest.fixture
def temp_config_path(tmp_path: Path) -> Path:
    """Write a representative ``config.yaml`` to a temp dir and return its path."""
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(REPRESENTATIVE_CONFIG, sort_keys=False))
    return p


@pytest.fixture
def temp_routing_path(tmp_path: Path) -> Path:
    """Write a representative routing-table YAML to a temp dir and return its path."""
    p = tmp_path / "routing.yaml"
    p.write_text(yaml.safe_dump(REPRESENTATIVE_ROUTING, sort_keys=False))
    return p


@pytest.fixture
def temp_config_with_routing(tmp_path: Path) -> tuple[Path, Path]:
    """Write a config.yaml whose performer endpoint mounts a sibling routing.yaml.

    Returns ``(config_path, routing_host_path)`` with the volume mount's
    ``host_path`` rewritten to the real temp routing file so location resolution
    in :func:`coordinare.routing_config_service.locate_routing_file` succeeds.
    """
    routing_path = tmp_path / "routing.yaml"
    routing_path.write_text(yaml.safe_dump(REPRESENTATIVE_ROUTING, sort_keys=False))

    cfg = {**REPRESENTATIVE_CONFIG}
    cfg["performer_endpoints"] = [
        {
            "id": "vllm-box",
            "mode": "ephemeral",
            "roles": ["implementer"],
            "image": "performer:full",
            "volumes": [
                {
                    "host_path": str(routing_path),
                    "container_path": "/devenv/routing.yaml",
                    "mode": "ro",
                },
            ],
            "env": {"SELFHOSTED_ROUTING_CONFIG": "/devenv/routing.yaml"},
        },
    ]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return config_path, routing_path


@pytest.fixture
def coordinare_config():
    """Build a :class:`CoordinareConfiguration` from :data:`REPRESENTATIVE_CONFIG`.

    The validated config object can never hold an unresolved ``${VAR}`` for the
    ``pat`` github_token (a model validator rejects it — expansion happens at load
    via ``from_yaml``/``os.path.expandvars``). So this fixture substitutes a real
    token for construction; the descriptor layer sources display values (including
    ``${VAR}`` literals) from the raw on-disk YAML, not from this object (research
    D3). The raw on-disk form with ``${VAR}`` lives in :data:`REPRESENTATIVE_CONFIG`
    and the ``temp_config_path`` fixture.
    """
    from coordinare.config import CoordinareConfiguration
    from coordinare.config_validation import coerce_multi_symphony_raw

    raw = {**REPRESENTATIVE_CONFIG, "github_token": "ghp_fixturetoken"}
    return CoordinareConfiguration(**coerce_multi_symphony_raw(raw))


@pytest.fixture
def invalid_config_path(tmp_path: Path) -> Path:
    """Write a config.yaml with one field that fails validation on load (research D8)."""
    text = textwrap.dedent(
        """\
        github_org: "acme"
        github_token: "${COORDINARE_GITHUB_TOKEN}"
        human_reviewers: ["alice"]
        poll_interval_seconds: 999999   # out of range (le=3600)
        symphonies:
          - name: demo
            github_project_number: 100
        orchestra:
          mode: shared_pool
        """,
    )
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p
