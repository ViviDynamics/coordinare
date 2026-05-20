"""Unit tests for service_inference.templater (T003)."""

from __future__ import annotations

import pytest
from coordinare_service_inference.schema import (
    ServiceEntry,
    ServicesManifest,
)
from coordinare_service_inference.templater import render


def _redis() -> ServiceEntry:
    return ServiceEntry(
        name="redis",
        binary="redis-server",
        version="7.2",
        data_dir="/tmp/redis-data",
        port=6379,
        why_needed="Sidekiq queue backend",
        sources=["Gemfile.lock"],
    )


def _postgres() -> ServiceEntry:
    return ServiceEntry(
        name="postgres",
        binary="postgres",
        version="16",
        data_dir="/tmp/pg-data",
        port=5432,
        why_needed="Primary application database",
        sources=["config/database.yml"],
    )


def _snowflake_external() -> ServiceEntry:
    return ServiceEntry(
        name="snowflake",
        binary="(external)",
        version="cloud",
        data_dir="(external)",
        port=443,
        why_needed="Analytics warehouse — cannot run in-container",
        sources=["config/snowflake.yml"],
        external_required=True,
        required_env_vars=["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"],
    )


def test_render_single_service_produces_three_scripts():
    manifest = ServicesManifest(
        services=[_redis()],
        cache_inputs=["Gemfile.lock"],
        agent_version="test-001",
    )

    scripts = render(manifest)

    for body in (scripts.start, scripts.stop, scripts.health):
        assert body.startswith("#!/usr/bin/env bash")
        assert "test-001" in body  # agent_version stamped in
    assert "redis" in scripts.start
    assert "REDIS_PID_FILE" in scripts.start
    assert "redis" in scripts.stop
    assert "6379" in scripts.health


def test_render_multi_service_emits_each():
    manifest = ServicesManifest(
        services=[_postgres(), _redis()],
        cache_inputs=["Gemfile.lock", "config/database.yml"],
        agent_version="test-002",
    )

    scripts = render(manifest)

    assert "POSTGRES_PID_FILE" in scripts.start
    assert "REDIS_PID_FILE" in scripts.start
    assert scripts.start.count("kill -0") >= 2
    assert "5432" in scripts.health
    assert "6379" in scripts.health


def test_render_external_required_emits_env_var_check_not_pid_logic():
    manifest = ServicesManifest(
        services=[_snowflake_external()],
        cache_inputs=["config/snowflake.yml"],
        agent_version="test-003",
    )

    scripts = render(manifest)

    assert "SNOWFLAKE_ACCOUNT" in scripts.start
    assert "SNOWFLAKE_USER" in scripts.start
    assert "SNOWFLAKE_PASSWORD" in scripts.start
    assert "SNOWFLAKE_PID_FILE" not in scripts.start
    # health script asserts presence of env vars rather than port-probing
    assert "SNOWFLAKE_ACCOUNT" in scripts.health
    # stop script must skip external services entirely
    assert "snowflake" not in scripts.stop.lower()


def test_render_mixed_hosted_and_external():
    manifest = ServicesManifest(
        services=[_postgres(), _snowflake_external()],
        cache_inputs=["config/database.yml", "config/snowflake.yml"],
        agent_version="test-004",
    )

    scripts = render(manifest)

    assert "POSTGRES_PID_FILE" in scripts.start
    assert "SNOWFLAKE_ACCOUNT" in scripts.start
    # stop covers only the hosted service
    assert "postgres" in scripts.stop
    assert "snowflake" not in scripts.stop.lower()


def test_render_empty_manifest_yields_runnable_scripts():
    manifest = ServicesManifest(
        services=[],
        cache_inputs=[],
        agent_version="manual-override",
    )

    scripts = render(manifest)

    # Even with no services, scripts must be valid shebang'd files with their
    # boilerplate intact — devenv-profile.sh always invokes them.
    assert scripts.start.startswith("#!/usr/bin/env bash")
    assert "services-start: complete" in scripts.start
    assert "services-stop: complete" in scripts.stop
    assert "services-health: all ok" in scripts.health


def test_render_start_args_are_shell_quoted():
    """Shell metacharacters in start_args must be quoted, not interpolated raw."""
    entry = ServiceEntry(
        name="postgres",
        binary="postgres",
        version="16",
        data_dir="/tmp/pg",
        port=5432,
        why_needed="db",
        start_args=["postgres", "-D", "/tmp/pg; rm -rf /", "-c", "shared_buffers=128MB"],
    )
    manifest = ServicesManifest(
        services=[entry], cache_inputs=[], agent_version="test-quote"
    )

    scripts = render(manifest)

    # The dangerous argument must appear shell-quoted, not as a bare token.
    assert "'/tmp/pg; rm -rf /'" in scripts.start
    # The raw, unquoted substring must NOT appear standalone in the launch line.
    # (Confirm shlex.quote ran: a literal `; rm -rf /` outside single-quotes is the failure mode.)
    assert " /tmp/pg; rm -rf / " not in scripts.start


def test_render_synthesises_default_when_start_args_none():
    entry = ServiceEntry(
        name="redis",
        binary="redis-server",
        version="7.2",
        data_dir="/tmp/r",
        port=6379,
        why_needed="cache",
        start_args=None,
    )
    manifest = ServicesManifest(
        services=[entry], cache_inputs=[], agent_version="test-default"
    )

    scripts = render(manifest)

    assert "--port=6379" in scripts.start
    assert "--data-dir=" in scripts.start


def test_render_uses_strict_undefined(monkeypatch, tmp_path):
    # If a template references a manifest field that doesn't exist, rendering must raise
    # rather than silently emit empty strings — protects against schema/template drift.
    bad_template_dir = tmp_path / "templates"
    bad_template_dir.mkdir()
    (bad_template_dir / "services-start.sh.j2").write_text(
        "#!/usr/bin/env bash\necho {{ nonexistent_field }}\n"
    )
    (bad_template_dir / "services-stop.sh.j2").write_text("#!/usr/bin/env bash\n")
    (bad_template_dir / "services-health.sh.j2").write_text("#!/usr/bin/env bash\n")

    manifest = ServicesManifest(services=[], cache_inputs=[], agent_version="x")

    with pytest.raises(Exception, match=r"nonexistent_field|undefined"):
        render(manifest, templates_dir=bad_template_dir)
