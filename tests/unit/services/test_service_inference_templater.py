"""Unit tests for service_inference.templater (T003)."""

from __future__ import annotations

import pytest
from coordinare_service_inference.schema import (
    ServiceEntry,
    ServiceInit,
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


# --- spec 091: postgres init / kind-aware rendering (T007-T010, T014b) ---


def _manifest(services, agent_version="test-091"):
    return ServicesManifest(
        services=services, cache_inputs=[], agent_version=agent_version
    )


def _postgres_init(**init_overrides) -> ServiceEntry:
    """A postgres entry carrying a full init block — the US1 headline case."""
    init_fields = dict(
        superuser="root",
        databases=["app_dev", "app_test"],
        password_env_var="POSTGRES_PASSWORD",
    )
    init_fields.update(init_overrides)
    return ServiceEntry(
        name="postgres",
        binary="postgres",
        version="16",
        data_dir="/tmp/pg-data",
        port=5432,
        why_needed="Primary application database",
        sources=["config/database.yml"],
        kind="postgres",
        init=ServiceInit(**init_fields),
    )


# C-1..C-3: shell-safety (T007)


def test_postgres_start_does_not_set_e():
    # C-1: no `set -e` — a create-if-missing probe intentionally returns non-zero
    # and must not abort the script.
    scripts = render(_manifest([_postgres_init()]))
    assert "\nset -e\n" not in scripts.start
    assert "set -e " not in scripts.start


def test_postgres_password_referenced_only_as_env_var():
    # C-3 / spec-110: the admin secret is referenced only via ${<password_env_var>},
    # passed to the unprivileged init shell via the environment (_PGPW), which reads
    # it through --pwfile=<(...) process substitution it creates itself — never a
    # literal in argv, logs, or on disk.
    scripts = render(_manifest([_postgres_init()]))
    s = scripts.start
    # secret handed to the dropped-privilege shell via env, not argv/disk:
    assert '_PGPW="${POSTGRES_PASSWORD}"' in s
    # that shell reads it via process substitution into --pwfile:
    assert "--pwfile=<(printf" in s and '"$_PGPW"' in s
    # The env-var NAME may appear; no literal secret value is ever emitted.
    assert "password=" not in s.lower()


def test_postgres_without_password_omits_pwfile():
    # C-3: trust-auth local init is valid; no password env var ⇒ no --pwfile.
    entry = _postgres_init(password_env_var=None)
    scripts = render(_manifest([entry]))
    assert "initdb" in scripts.start
    assert "--pwfile" not in scripts.start


# --- spec 115: service-scoped library load path (libpq.so.5) ---


def test_service_lib_loadpath_prelude_built_from_extract_tree():
    # spec 115: the script builds _SVC_LD_LIBRARY_PATH from the cache's services-extract tree
    # (where libpq.so.5 + the postgres extension libs live), prepended onto the profile-set
    # LD_LIBRARY_PATH and guarded on $DEVENV.
    s = render(_manifest([_postgres_init()])).start
    assert '_SVC_LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"' in s
    assert 'if [ -n "${DEVENV:-}" ]; then' in s
    assert '"$DEVENV"/services-extract/usr/lib/*-linux-gnu' in s
    assert '"$DEVENV"/services-extract/usr/lib/postgresql/*/lib' in s
    # additive prepend, not a clobber:
    assert '_SVC_LD_LIBRARY_PATH="$_svc_lib:$_SVC_LD_LIBRARY_PATH"' in s


def test_service_lib_loadpath_exported_for_clients():
    # The path is exported for THIS script so the root-caller clients (pg_isready/psql/
    # createdb) and the backgrounded daemons link against the cached libs. (Safe: the script
    # is a one-shot bootstrap process; the app-under-test gets a fresh shell via activate.sh.)
    s = render(_manifest([_postgres_init()])).start
    assert 'export LD_LIBRARY_PATH="$_SVC_LD_LIBRARY_PATH"' in s


def test_postgres_runuser_invocations_pass_scoped_loadpath_explicitly():
    # runuser drops the inherited env, so _pg_as (initdb/postgres) and the md5-auth init must
    # pass the scoped path explicitly rather than rely on the script-level export.
    s = render(_manifest([_postgres_init()])).start
    assert '_pg_as() { runuser -u "$_PGUSER" -- env PATH="$PATH" LD_LIBRARY_PATH="$_SVC_LD_LIBRARY_PATH"' in s
    assert 'LD_LIBRARY_PATH="$_SVC_LD_LIBRARY_PATH" _PGPW=' in s
    # the stale raw-LD_LIBRARY_PATH passthrough is gone from the managed invocations:
    assert 'LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" "$@"' not in s


# C-5..C-10: postgres init render (T008)


def test_postgres_init_runs_before_launch():
    # C-5: initdb → (superuser created atomically by initdb --username) → launch
    # → create databases. Assert ordering by offset of the EXECUTION lines (ignore
    # comments, which spec-110 added mentioning initdb/postgres).
    scripts = render(_manifest([_postgres_init()]))
    exec_lines = "\n".join(
        ln for ln in scripts.start.splitlines() if not ln.lstrip().startswith("#")
    )
    i_initdb = exec_lines.index("initdb --no-sync")
    i_launch = exec_lines.index("postgres -D")
    i_createdb = exec_lines.index("createdb")
    assert i_initdb < i_launch < i_createdb
    # superuser created atomically by initdb (no separate CREATE ROLE step).
    assert "--username=root" in exec_lines
    # spec-110: postgres runs as an unprivileged user (it refuses to run as root).
    assert "_PGUSER=pgrunner" in scripts.start and "runuser -u" in scripts.start


def test_postgres_initdb_guarded_by_pg_version_sentinel():
    # C-6: initdb is skipped when the <data_dir>/PG_VERSION sentinel exists.
    scripts = render(_manifest([_postgres_init()]))
    assert 'if [ ! -f "$_PGDATA/PG_VERSION" ]; then' in scripts.start


def test_postgres_database_creation_is_create_if_missing():
    # C-7: each database is created only if absent — convergent on partial init,
    # never short-circuited by the initdb sentinel.
    scripts = render(_manifest([_postgres_init()]))
    s = scripts.start
    assert "grep -qw app_dev" in s
    assert "grep -qw app_test" in s
    assert 'createdb -h "$_PGDATA" -p "$_PGPORT" -U root app_dev' in s
    assert 'createdb -h "$_PGDATA" -p "$_PGPORT" -U root app_test' in s


def test_postgres_createdb_failure_is_attributed_not_swallowed():
    # C-7: the script runs without `set -e`, so a failed createdb would be
    # swallowed and the service reported "started" with the database absent.
    # The createdb is gated explicitly and a failure is attributed to the
    # environment (exit 75), not misattributed to the code under test.
    scripts = render(_manifest([_postgres_init()]))
    s = scripts.start
    assert "if ! createdb" in s
    assert "ERROR: env: postgres failed to create database app_dev" in s
    assert "ERROR: env: postgres failed to create database app_test" in s
    # the failure path uses the environment-attributed exit code.
    assert "exit 75" in s


def test_postgres_init_guards_missing_password_env_var():
    # C-3/FR-005: when an init password env var is declared, a GENUINELY-UNSET var
    # (not merely empty) at init time must surface a clear environment-attributed
    # failure (exit 75) rather than a cryptic `set -u` "unbound variable" abort.
    # The unset-vs-empty distinction uses ${VAR+x} (set-test), not ${VAR:-}
    # (empty-or-unset), so a declared-but-empty password is NOT treated as unset.
    scripts = render(_manifest([_postgres_init()]))
    s = scripts.start
    assert 'if [ -z "${POSTGRES_PASSWORD+x}" ]; then' in s
    assert (
        "ERROR: env: postgres requires env var POSTGRES_PASSWORD "
        "(the admin secret) but it is unset" in s
    )


def test_postgres_declared_empty_password_falls_through_to_trust_auth():
    # spec-092/FR: a declared password env var that is SET BUT EMPTY signals an
    # intentional passwordless cluster (e.g. POSTGRESQL_PASSWORD= in .env.test).
    # The init must branch on empty-after-set and initialize with trust auth for
    # BOTH local and host connections — never reject it with exit 75, and never
    # feed an empty secret to md5 via --pwfile (which postgres would reject).
    scripts = render(_manifest([_postgres_init()]))
    s = scripts.start
    # the empty-after-set branch: unset already handled, so a plain -z on the value.
    assert 'elif [ -z "${POSTGRES_PASSWORD}" ]; then' in s
    # the passwordless init uses trust auth on both seams.
    assert "--auth-local=trust --auth-host=trust" in s
    # the non-empty branch still reaches md5 + pwfile (secret via _PGPW env).
    assert "--auth-local=trust --auth-host=md5" in s
    assert '_PGPW="${POSTGRES_PASSWORD}"' in s and "--pwfile=<(printf" in s
    # the empty case must NOT route through --pwfile (no md5 with an empty secret).
    i_empty = s.index('elif [ -z "${POSTGRES_PASSWORD}" ]; then')
    i_md5 = s.index("--auth-host=md5")
    assert i_empty < i_md5  # empty branch precedes the md5 (else) branch


def test_postgres_without_password_omits_password_guard():
    # C-3: trust-auth init declares no secret, so there is no password-presence
    # guard to render — it is bound to the --pwfile branch only.
    scripts = render(_manifest([_postgres_init(password_env_var=None)]))
    assert "the admin secret) but it is unset" not in scripts.start


def test_postgres_binds_declared_port():
    # C-8: the server listens on the declared port on loopback, not the default.
    scripts = render(_manifest([_postgres_init()]))
    s = scripts.start
    assert 'POSTGRES_PORT="5432"' in s
    assert 'postgres -D "$_PGDATA" -p "$_PGPORT"' in s
    assert "listen_addresses=127.0.0.1" in s


def test_postgres_reuses_running_and_port_guards():
    # C-9: the init+launch sits inside the existing _is_running / _port_bound
    # guard so a second invocation no-ops.
    scripts = render(_manifest([_postgres_init()]))
    s = scripts.start
    assert 'if _is_running "${POSTGRES_PID_FILE}"; then' in s
    assert 'elif _port_bound "${POSTGRES_PORT}"; then' in s


def test_postgres_data_dir_resolves_under_services_root():
    # C-10: an initializing service's data_dir is the writable services root,
    # never the declared (possibly read-only cache) path.
    scripts = render(_manifest([_postgres_init()]))
    s = scripts.start
    assert 'POSTGRES_DATA_DIR="$SERVICES_DIR/postgres-data"' in s
    # the declared data_dir is ignored for postgres state
    assert "/tmp/pg-data" not in s


# C-4: generic/redis render unchanged (T009)


def test_generic_redis_render_unchanged_by_postgres_support():
    # C-4: a manifest with no initializing kind renders as pre-091 — none of the
    # postgres-only init tokens leak in.
    scripts = render(_manifest([_redis()]))
    s = scripts.start
    for token in ("initdb", "PG_VERSION", "pg_isready", "createdb", "_PGDATA"):
        assert token not in s, f"postgres token {token!r} leaked into generic render"
    # the redis launch line is emitted verbatim (it inherits the spec-115 script-level
    # LD_LIBRARY_PATH export so the cached redis-server's libs resolve).
    assert (
        "  redis-server --port=6379 --data-dir=/tmp/redis-data "
        '>"${REDIS_DATA_DIR}/redis.log" 2>&1 &' in s
    )
    assert "pg_isready" not in scripts.health
    assert "pg_ctl" not in scripts.stop


# C-11, C-12: readiness probe (T010)


def test_postgres_health_uses_pg_isready_redis_retains_port_check():
    # C-11: postgres health is a connect-level pg_isready probe; redis keeps the
    # existing port/liveness check.
    scripts = render(_manifest([_postgres_init(), _redis()]))
    h = scripts.health
    assert "pg_isready -h 127.0.0.1 -p 5432" in h
    assert '_port_open "6379"' in h


def test_postgres_readiness_wait_is_bounded_and_attributed():
    # C-12: the readiness wait before createdb is bounded (no hang) and a timeout
    # is surfaced as an environment-attributed failure.
    scripts = render(_manifest([_postgres_init()]))
    s = scripts.start
    # spec 111: wait raised 60s→180s to cover initdb under load (ephemeral, re-run
    # every container); initdb uses --no-sync to keep that fast.
    assert 'while [ "$_pg_wait" -lt 180 ]; do' in s
    assert "ERROR: env: postgres did not become ready within 180s" in s
    assert "initdb --no-sync" in s


# C-13b: kind-aware teardown (T014b)


def test_postgres_stop_uses_pg_ctl_fast_redis_retains_stop_pid():
    # C-13b: postgres teardown is a clean `pg_ctl stop -m fast`; redis/generic
    # keep the SIGTERM→SIGKILL _stop_pid path.
    scripts = render(_manifest([_postgres_init(), _redis()]))
    st = scripts.stop
    assert "pg_ctl stop -m fast -D" in st
    assert '_stop_pid "$SERVICES_DIR/redis.pid" "redis"' in st


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
