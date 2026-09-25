"""Integration test for spec-092 US2: agent-discovered test-env fallback.

When no ``test_env`` block is configured, the service-inference agent emits a
path-only ``test_env_source`` on the manifest (never literal values). Coordinare
persists that PATH alongside the env-cache and reloads the same file — through the
shared :func:`coordinare.services.env_cache.resolve_test_env_vars` — for the
start-phase dry-run, the QA-runtime, and code-running performer contexts.

This exercises the real fallback seam end to end:

* the manifest carries only the PATH (``test_env_source``), never a value;
* the discovered file is reloaded via the GitHub-API content seam and parsed into
  literal ``KEY=value`` pairs, which clear the postgres unset-secret gate in the
  start-phase dry-run (proving the discovered value crossed the ``validate(..., env=)``
  seam — the same value later contexts reload from the persisted path);
* a configured ``test_env`` block always wins over a discovered path;
* with no source supplying the needed var, the dry-run still trips ``exit 75``
  (EX_TEMPFAIL) — actionable, never a silent pass.

The gate-pass assertion is "execution got *past* ``exit 75``", not full success — see
``test_test_env_injection.py`` for the rationale (the gate fires in milliseconds before
any ``initdb``; a timeout therefore unambiguously proves the gate cleared).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinare_service_inference.schema import ServiceEntry, ServiceInit, ServicesManifest
from coordinare_service_inference.templater import render
from coordinare_service_inference.validator import validate

from coordinare.config import TestEnvConfig
from coordinare.services.env_cache import resolve_test_env_vars

# The website symphony's postgres admin-secret env var name (a NAME, never a value).
PW_VAR = "POSTGRESQL_PASSWORD"
GATE_EXIT = 75  # EX_TEMPFAIL — the unset-secret gate in services-start.sh.

DISCOVERED_PATH = ".env.test"
ORG = "vivi-org"
REPO = "website"


class _FakeGitHub:
    """Minimal GitHub content seam returning a fixed file at a known repo path."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.calls: list[tuple[str, str, str]] = []

    async def get_file_content(self, org: str, repo: str, path: str) -> str | None:
        self.calls.append((org, repo, path))
        return self._files.get(path)


def _postgres_manifest(data_dir: Path, *, test_env_source: str | None = None) -> ServicesManifest:
    """A postgres entry whose start gate requires ``PW_VAR`` to be set."""
    return ServicesManifest(
        services=[
            ServiceEntry(
                name="postgres",
                binary="postgres",
                version="16",
                data_dir=str(data_dir),
                port=59432,
                why_needed="Primary application database",
                sources=["config/database.yml"],
                kind="postgres",
                init=ServiceInit(
                    superuser="root",
                    databases=["app_test"],
                    password_env_var=PW_VAR,
                ),
            ),
        ],
        cache_inputs=[],
        agent_version="test-092",
        test_env_source=test_env_source,
    )


def _validate(scripts, env, *, timeout_seconds=15.0):
    return validate(
        scripts, env=env, timeout_seconds=timeout_seconds, health_delay_seconds=0.0,
    )


def _assert_gate_cleared(scripts, env) -> None:
    """Assert the start-phase dry-run got *past* the unset-secret gate.

    The gate prints its "unset" diagnostic when it fires, so the diagnostic is
    the discriminator — NOT the exit code. A fast ``exit 75`` from the
    dead-daemon guard (a daemon that cannot start on this host) is NOT the
    gate; review 475 round 2 gave the postgres launch that guard.
    """
    try:
        result = _validate(scripts, env, timeout_seconds=5.0)
    except subprocess.TimeoutExpired:
        return  # got past the gate into initdb/readiness — gate cleared.
    assert "unset" not in (result.stderr or "")


class TestDiscoveredTestEnvFallback:
    def test_manifest_test_env_source_is_path_only(self, tmp_path: Path) -> None:
        manifest = _postgres_manifest(
            tmp_path / "pgdata", test_env_source=DISCOVERED_PATH,
        )

        assert manifest.test_env_source == DISCOVERED_PATH
        # The PATH is the only test-env artifact on the manifest — never a value.
        dumped = manifest.model_dump_json()
        assert DISCOVERED_PATH in dumped
        assert "test-pw" not in dumped  # no literal secret value leaked anywhere.

    async def test_discovered_path_reloads_and_clears_gate(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.delenv(PW_VAR, raising=False)
        github = _FakeGitHub({DISCOVERED_PATH: f"{PW_VAR}=disc-pw-789\n"})

        # No config block (test_env=None) + a persisted discovered PATH → reload it.
        # This single call models every later context (dry-run, QA-runtime, performer)
        # reloading the SAME persisted path.
        loaded = await resolve_test_env_vars(
            symphony_name="website",
            test_env=None,
            github_org=ORG,
            repo=REPO,
            github_service=github,
            fallback_source=DISCOVERED_PATH,
        )
        assert loaded == {PW_VAR: "disc-pw-789"}
        assert github.calls == [(ORG, REPO, DISCOVERED_PATH)]

        scripts = render(_postgres_manifest(tmp_path / "pgdata-disc"))
        _assert_gate_cleared(scripts, loaded)

    async def test_config_block_wins_over_discovered_path(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.delenv(PW_VAR, raising=False)
        host_file = tmp_path / "host-secrets.env"
        host_file.write_text(f"{PW_VAR}=config-pw-000\n", encoding="utf-8")
        # The discovered file carries a DIFFERENT value — it must be ignored.
        github = _FakeGitHub({DISCOVERED_PATH: f"{PW_VAR}=disc-pw-789\n"})

        loaded = await resolve_test_env_vars(
            symphony_name="website",
            test_env=TestEnvConfig(host_path=str(host_file)),
            github_org=ORG,
            repo=REPO,
            github_service=github,
            fallback_source=DISCOVERED_PATH,
        )

        assert loaded == {PW_VAR: "config-pw-000"}
        # Config-wins: the discovered path was never fetched.
        assert github.calls == []

    async def test_no_source_still_trips_exit_75(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.delenv(PW_VAR, raising=False)
        github = _FakeGitHub({})  # discovered file vanished / never existed.

        loaded = await resolve_test_env_vars(
            symphony_name="website",
            test_env=None,
            github_org=ORG,
            repo=REPO,
            github_service=github,
            fallback_source=DISCOVERED_PATH,
        )
        assert loaded == {}  # best-effort: vanished discovered file → empty.

        scripts = render(_postgres_manifest(tmp_path / "pgdata-none"))
        result = _validate(scripts, env=loaded)

        assert not result.ok
        assert result.phase == "start"
        assert result.returncode == GATE_EXIT
        assert PW_VAR in result.stderr
        assert "unset" in result.stderr
