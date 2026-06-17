"""Integration test for spec-092 US1: configured test-env unblocks the dry-run.

The headline repro is the ``website`` symphony's postgres "Connection refused": the
service-inference start-phase dry-run rejected the manifest because ``POSTGRESQL_PASSWORD``
was unset, so the postgres gate tripped ``exit 75`` (EX_TEMPFAIL) and the manifest was
never accepted — leaving a stale ``services-start.sh`` that never booted postgres.

This exercises the real loader → validator seam end to end:

* render a postgres-init manifest (the gate at ``services-start.sh`` requires the admin
  secret env var to be present);
* with the var absent, the start-phase dry-run trips ``exit 75`` with the actionable
  "requires env var ... unset" message;
* with the var supplied through ``load_test_env`` (both ``repo_path`` and ``host_path``
  sources), the same dry-run clears the gate (no ``exit 75``) — proving the loaded value
  crossed the ``validate(..., env=)`` seam into the dry-run subprocess.

The gate-pass assertion is "execution got *past* ``exit 75``", not full success. The
``exit 75`` gate fires (or doesn't) in milliseconds, before any ``initdb``; everything
after it is slow and host-dependent. On a host *without* ``postgres``/``initdb`` the run
fails fast at ``initdb`` with a return code that is simply not ``75``; on a host *with*
postgres installed the script proceeds into ``initdb`` + the 60s ``pg_isready`` readiness
loop and is SIGKILLed by the short ``validate`` timeout. A ``subprocess.TimeoutExpired`` is
therefore unambiguous proof the gate cleared — the gate could never itself cause a timeout.
So either outcome (a returned result whose ``returncode != 75``, or a timeout) confirms the
manifest was accepted, deterministically and regardless of whether postgres is installed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinare_service_inference.schema import ServiceEntry, ServiceInit, ServicesManifest
from coordinare_service_inference.templater import render
from coordinare_service_inference.validator import validate

from coordinare.config import TestEnvConfig
from coordinare.services.test_env_loader import load_test_env

# The website symphony's postgres admin-secret env var name (a NAME, never a value).
PW_VAR = "POSTGRESQL_PASSWORD"
GATE_EXIT = 75  # EX_TEMPFAIL — the unset-secret gate in services-start.sh.


def _postgres_manifest(data_dir: Path) -> ServicesManifest:
    """A postgres entry whose start gate requires ``PW_VAR`` to be set."""
    return ServicesManifest(
        services=[
            ServiceEntry(
                name="postgres",
                binary="postgres",
                version="16",
                data_dir=str(data_dir),
                # A high, unlikely-bound port: if the port is already bound the start
                # script short-circuits ("assuming external instance") and never reaches
                # the admin-secret gate we are exercising.
                port=59432,
                why_needed="Primary application database",
                sources=["config/database.yml"],
                kind="postgres",
                init=ServiceInit(
                    superuser="root",
                    databases=["app_test"],
                    password_env_var=PW_VAR,
                ),
            )
        ],
        cache_inputs=[],
        agent_version="test-092",
    )


def _validate(scripts, env, *, timeout_seconds=15.0):
    # health_delay_seconds=0 keeps the run fast: the absent case returns in the start
    # phase before any readiness pause.
    return validate(
        scripts, env=env, timeout_seconds=timeout_seconds, health_delay_seconds=0.0
    )


def _assert_gate_cleared(scripts, env) -> None:
    """Assert the start-phase dry-run got *past* the unset-secret gate.

    Uses a short timeout: the ``exit 75`` gate fires in milliseconds, so a returned
    result must show ``returncode != 75`` (and no "unset" diagnostic). A timeout means
    execution proceeded into the slow ``initdb`` / readiness path — itself proof the gate
    cleared, since the gate can never cause a timeout. ``validate`` does not catch
    ``TimeoutExpired``, so we catch it here.
    """
    try:
        result = _validate(scripts, env, timeout_seconds=5.0)
    except subprocess.TimeoutExpired:
        return  # got past the gate into initdb/readiness — gate cleared.
    assert result.returncode != GATE_EXIT
    assert "unset" not in result.stderr


class TestConfiguredTestEnvInjection:
    def test_absent_var_trips_exit_75_with_actionable_message(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Ensure the secret is genuinely absent from the inherited environment, since
        # validate() merges os.environ into the dry-run subprocess.
        monkeypatch.delenv(PW_VAR, raising=False)
        scripts = render(_postgres_manifest(tmp_path / "pgdata"))

        result = _validate(scripts, env={})

        assert not result.ok
        assert result.phase == "start"
        assert result.returncode == GATE_EXIT
        assert PW_VAR in result.stderr
        assert "unset" in result.stderr

    def test_repo_path_source_clears_the_gate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv(PW_VAR, raising=False)
        repo_root = tmp_path / "symphony_repo"
        repo_root.mkdir()
        (repo_root / ".env.test").write_text(
            f"{PW_VAR}=test-pw-123\n", encoding="utf-8"
        )

        test_env = load_test_env(
            TestEnvConfig(repo_path=".env.test"), repo_root=repo_root
        )
        assert test_env == {PW_VAR: "test-pw-123"}

        scripts = render(_postgres_manifest(tmp_path / "pgdata-repo"))
        _assert_gate_cleared(scripts, test_env)

    def test_host_path_source_clears_the_gate_equivalently(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv(PW_VAR, raising=False)
        host_file = tmp_path / "host-secrets.env"
        host_file.write_text(f"{PW_VAR}=host-pw-456\n", encoding="utf-8")

        test_env = load_test_env(
            TestEnvConfig(host_path=str(host_file)),
            repo_root=tmp_path / "unrelated_repo",
        )
        assert test_env == {PW_VAR: "host-pw-456"}

        scripts = render(_postgres_manifest(tmp_path / "pgdata-host"))
        _assert_gate_cleared(scripts, test_env)
