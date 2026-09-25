"""E2E integration tests for spec 063 Phase 2: LLM-driven service inference.

These tests stub the LLM (the agent loop itself is unit-tested in
``test_service_inference_agent.py``) and exercise the full pipeline downstream
of the agent: agent → templater → validator → artifact drop / rejected
manifest. Each fixture stands in for a "real" project shape — Rails, Go,
Snowflake — and the stubbed LLM returns the manifest a competent agent would
have produced.

The "services" inside the manifests are hermetic bash → python TCP listeners
(see :func:`_write_fake_service`) so we don't need a real postgres or redis
in CI. The templated start/stop/health scripts run against those shims using
the same code paths a real service entry would exercise.

T016: Rails + Postgres + Redis → end-to-end success.
T017: Go + Postgres → manifest's cache_inputs reflect Go file shape.
T018: Snowflake-stubbed → external_required → build fails with structured
      env-var message captured in services.json.rejected.
"""

from __future__ import annotations

import json
import socket
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from coordinare_service_inference import (
    REJECTED_FILENAME,
    InferenceFailed,
    infer_services,
)
from coordinare_service_inference.agent import LLMStep


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _write_fake_service(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "fakesvc"
    py = sys.executable
    fake.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            PORT=""
            for arg in "$@"; do
              case "$arg" in
                --port=*) PORT="${{arg#--port=}}" ;;
              esac
            done
            exec {py} -c "
            import socket, time
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', int(${{PORT}})))
            s.listen(128)
            while True:
                time.sleep(60)
            "
            """,
        ),
    )
    fake.chmod(0o755)
    return fake


class _StubClient:
    """Returns a single pre-baked manifest on the first step."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self._manifest = manifest
        self.calls = 0

    async def step(self, messages: list[dict[str, Any]]) -> LLMStep:
        self.calls += 1
        return LLMStep(manifest=self._manifest)


def _rails_fixture(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "Gemfile").write_text(
        "source 'https://rubygems.org'\ngem 'rails'\ngem 'pg'\ngem 'redis'\n",
    )
    (project / "config").mkdir()
    (project / "config" / "database.yml").write_text(
        "default: &default\n  adapter: postgresql\n  encoding: unicode\n",
    )


def _go_fixture(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "go.mod").write_text(
        "module example.com/app\n\ngo 1.22\n\nrequire github.com/lib/pq v1.10.9\n",
    )
    (project / "main.go").write_text(
        'package main\n\nimport _ "github.com/lib/pq"\n\nfunc main() {}\n',
    )


def _snowflake_fixture(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "requirements.txt").write_text("snowflake-connector-python==3.5.0\n")
    (project / ".env.example").write_text(
        "SNOWFLAKE_ACCOUNT=\nSNOWFLAKE_USER=\nSNOWFLAKE_PASSWORD=\n",
    )


# ---------------------------------------------------------------------------
# T016 — Rails fixture: full success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rails_postgres_redis_end_to_end(tmp_path: Path) -> None:
    project = tmp_path / "rails-app"
    _rails_fixture(project)

    fake_bin = _write_fake_service(tmp_path / "bin")
    pg_port = _free_port()
    redis_port = _free_port()

    manifest = {
        "services": [
            {
                "name": "postgres",
                "binary": str(fake_bin),
                "version": "16.1",
                "data_dir": str(tmp_path / "pgdata"),
                "port": pg_port,
                "why_needed": "rails ActiveRecord adapter cited in config/database.yml",
                "kind": "generic",
                "start_args": [str(fake_bin), f"--port={pg_port}"],
                "health_command": [
                    "bash",
                    "-c",
                    f"exec 3<>/dev/tcp/127.0.0.1/{pg_port}",
                ],
                "sources": ["config/database.yml", "Gemfile"],
            },
            {
                "name": "redis",
                "binary": str(fake_bin),
                "version": "7.2",
                "data_dir": str(tmp_path / "redisdata"),
                "port": redis_port,
                "why_needed": "rails session/cache store cited in Gemfile",
                "kind": "generic",
                "start_args": [str(fake_bin), f"--port={redis_port}"],
                "health_command": [
                    "bash",
                    "-c",
                    f"exec 3<>/dev/tcp/127.0.0.1/{redis_port}",
                ],
                "sources": ["Gemfile"],
            },
        ],
        "cache_inputs": ["Gemfile", "config/database.yml"],
        "agent_version": "test-rails",
    }

    env_cache = tmp_path / "env-cache"
    client = _StubClient(manifest)

    try:
        result = await infer_services(
            project_root=project,
            output_root=env_cache,
            agent_version="test-rails",
            llm_client=client,
            retry_budget=1,
        )

        assert result.attempts == 1
        services_dir = result.scripts_dir
        services_json = json.loads((services_dir / "services.json").read_text())
        names = {s["name"] for s in services_json["services"]}
        assert names == {"postgres", "redis"}
        assert services_json["cache_inputs"] == ["Gemfile", "config/database.yml"]
        # Validator must have actually passed — assert artifacts exist + .rejected absent.
        assert not (services_dir / REJECTED_FILENAME).exists()
        assert (services_dir / "services-start.sh").stat().st_mode & 0o111
    finally:
        # Defence in depth — the validator runs stop, but if it failed we may
        # still have a fakesvc listening; close ports best-effort by re-running
        # the stop script.
        stop_path = env_cache / "services" / "services-stop.sh"
        if stop_path.exists():
            import subprocess
            subprocess.run(["bash", str(stop_path)], capture_output=True, check=False)


# ---------------------------------------------------------------------------
# T017 — Go fixture: cache_inputs reflect Go shape, not Ruby
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_go_postgres_cache_inputs_reflect_go_shape(tmp_path: Path) -> None:
    project = tmp_path / "go-app"
    _go_fixture(project)

    fake_bin = _write_fake_service(tmp_path / "bin")
    pg_port = _free_port()

    manifest = {
        "services": [
            {
                "name": "postgres",
                "binary": str(fake_bin),
                "version": "16.1",
                "data_dir": str(tmp_path / "pgdata"),
                "port": pg_port,
                "why_needed": "lib/pq driver imported in go.mod and main.go",
                "kind": "generic",
                "start_args": [str(fake_bin), f"--port={pg_port}"],
                "health_command": [
                    "bash",
                    "-c",
                    f"exec 3<>/dev/tcp/127.0.0.1/{pg_port}",
                ],
                "sources": ["go.mod", "main.go"],
            },
        ],
        "cache_inputs": ["go.mod", "main.go"],
        "agent_version": "test-go",
    }

    env_cache = tmp_path / "env-cache"
    client = _StubClient(manifest)

    try:
        result = await infer_services(
            project_root=project,
            output_root=env_cache,
            agent_version="test-go",
            llm_client=client,
            retry_budget=1,
        )

        payload = json.loads((result.scripts_dir / "services.json").read_text())
        # The discriminating assertion: Go-relevant paths, no Ruby paths.
        assert "go.mod" in payload["cache_inputs"]
        assert "main.go" in payload["cache_inputs"]
        assert not any(p.startswith("Gemfile") for p in payload["cache_inputs"])
        assert not any("config/database.yml" in p for p in payload["cache_inputs"])
    finally:
        stop_path = env_cache / "services" / "services-stop.sh"
        if stop_path.exists():
            import subprocess
            subprocess.run(["bash", str(stop_path)], capture_output=True, check=False)


# ---------------------------------------------------------------------------
# T018 — Snowflake-stubbed fixture: external_required → build fails structurally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snowflake_external_required_build_fails_with_env_var_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "snowflake-app"
    _snowflake_fixture(project)

    manifest = {
        "services": [
            {
                "name": "snowflake",
                "binary": "snowsql",  # never invoked; external service
                "version": "managed",
                "data_dir": "/tmp/unused-snowflake",
                "port": 443,
                "why_needed": "snowflake-connector-python in requirements.txt",
                "sources": ["requirements.txt", ".env.example"],
                "external_required": True,
                "required_env_vars": [
                    "SNOWFLAKE_ACCOUNT",
                    "SNOWFLAKE_USER",
                    "SNOWFLAKE_PASSWORD",
                ],
            },
        ],
        "cache_inputs": ["requirements.txt", ".env.example"],
        "agent_version": "test-snowflake",
    }

    # Ensure the env vars are absent so the templated start.sh exits 64.
    for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"):
        monkeypatch.delenv(v, raising=False)

    env_cache = tmp_path / "env-cache"
    client = _StubClient(manifest)

    # retry_budget=1 so the test runs fast — we want the failure, not retries.
    with pytest.raises(InferenceFailed) as excinfo:
        await infer_services(
            project_root=project,
            output_root=env_cache,
            agent_version="test-snowflake",
            llm_client=client,
            retry_budget=1,
        )

    rejected_path = excinfo.value.rejected_path
    assert rejected_path.name == REJECTED_FILENAME
    payload = json.loads(rejected_path.read_text())
    # Structured event surface: validator failed at start, with stderr naming
    # the missing env var. Operators can grep for ERROR + env var name.
    summary = payload["validation_summary"]
    assert "start" in summary
    assert "SNOWFLAKE_ACCOUNT" in summary
    assert payload["manifest"]["services"][0]["external_required"] is True
    assert "SNOWFLAKE_ACCOUNT" in payload["manifest"]["services"][0]["required_env_vars"]
