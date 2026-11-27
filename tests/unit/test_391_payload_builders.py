"""391: the production payload builders are callable without a service.

The persona bench builds its ``/jobs`` payloads with these functions, so the
bench sends exactly what a dispatch sends.
"""
from __future__ import annotations

from coordinare.services import http_performer_service as hps
from coordinare.workspace import WorkspaceInfo


def test_job_payload_builder_is_a_module_function(monkeypatch):
    monkeypatch.setenv("BENCH_GATEWAY_KEY", "sk-gateway")
    card_context = {
        "id": "bench-qa", "role": "qa", "backend": "nare",
        "persona_instructions": "be QA", "title": "t",
        "base_url": "https://gateway.example/v1", "auth_token_env": "BENCH_GATEWAY_KEY",
        "endpoint_kind": "litellm", "model": "m",
    }
    info = WorkspaceInfo(path=None, branch="b", repo_url="https://github.com/o/r.git", github_token="ghs")
    payload = hps.build_job_payload(card_context, info)
    assert payload.role == "qa"
    assert payload.branch == "b"
    assert payload.metadata["endpoint_kind"] == "litellm"
    assert payload.metadata["base_url"] == "https://gateway.example/v1"
    assert payload.secrets["OPENAI_API_KEY"].get_secret_value() == "sk-gateway"
    assert payload.secrets["GITHUB_TOKEN"].get_secret_value() == "ghs"


def test_bootstrap_payload_builder_is_a_module_function():
    card_context = {
        "job_type": "env_bootstrap", "symphony_name": "bench", "symphony_org": "o",
        "symphony_repo": "r", "cache_mount_path": "/devenv/bench", "_github_token": "ghs",
        "environment_manifest": {"a": 1}, "environment_assessment": {"b": 2},
        "rendered_verify_sh": "v", "rendered_activate_sh": "a",
    }
    payload = hps.build_env_bootstrap_payload(card_context)
    assert payload.role == "env_bootstrap"
    assert payload.env_cache_path == "/devenv/bench"
    assert payload.metadata["environment_manifest"] == {"a": 1}
    assert payload.bootstrap_scripts == {"verify_sh": "v", "activate_sh": "a"}
    assert "_github_token" not in payload.metadata
    # The job payload builder routes a bootstrap context the same way.
    routed = hps.build_job_payload(card_context, None)
    assert routed.role == "env_bootstrap"
