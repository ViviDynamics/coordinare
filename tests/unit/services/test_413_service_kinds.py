"""Issue 413: service-kind generality — per-kind packages, launches, probes.

Covers:
- schema: the managed kind set widens beyond postgres/redis; an in-container
  generic service REQUIRES start_args and health_command (the redis-shaped
  `--port/--data-dir` default is removed).
- templater: per-kind launch lines, dead-daemon detection at start, per-kind
  readiness budgets, protocol-level health probes.
- env_manifest: per-kind package lists and readiness probes.
- prompt: the hostable set agrees with the schema.
- persona: QA text must not assert WHICH services are running.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from coordinare_service_inference.schema import (
    COORDINARE_MANAGED_KINDS,
    ServiceEntry,
    ServicesManifest,
)
from coordinare_service_inference.templater import render

from coordinare.services.env_manifest import EnvManifest

# ---------------------------------------------------------------- schema


def _entry(**overrides):
    fields = {
        "name": "mysql",
        "binary": "mysqld",
        "data_dir": "/tmp/mysql-data",
        "port": 3306,
        "why_needed": "app database",
        "kind": "mysql",
    }
    fields.update(overrides)
    return fields


def test_managed_kinds_cover_the_hostable_set():
    assert {
        "postgres", "redis", "mysql", "mongodb", "rabbitmq",
        "elasticsearch", "memcached", "minio",
    } <= COORDINARE_MANAGED_KINDS


@pytest.mark.parametrize(
    ("kind", "binary"),
    [
        ("mysql", "mysqld"),
        ("mongodb", "mongod"),
        ("rabbitmq", "rabbitmq-server"),
        ("elasticsearch", "elasticsearch"),
        ("memcached", "memcached"),
        ("minio", "minio"),
    ],
)
def test_new_kinds_are_valid_schema_values(kind, binary):
    entry = ServiceEntry(**_entry(kind=kind, binary=binary))
    assert entry.kind == kind


def test_generic_in_container_requires_start_args_and_health_command():
    # The redis-shaped default launch is REMOVED: a generic service must carry
    # its own start_args and health_command or the manifest is a load-time error.
    with pytest.raises(ValidationError, match="start_args"):
        ServiceEntry(
            **_entry(
                kind="generic", binary="/bin/app", start_args=None, health_command=None,
            ),
        )


def test_generic_with_start_args_and_health_command_is_valid():
    entry = ServiceEntry(
        **_entry(
            kind="generic",
            binary="/bin/app",
            start_args=["/bin/app", "--serve"],
            health_command=["/bin/app", "--probe"],
        ),
    )
    assert entry.health_command == ["/bin/app", "--probe"]


def test_generic_external_required_does_not_need_start_args():
    # Nothing is launched for an external service, so the launch/probe contract
    # does not apply to it.
    entry = ServiceEntry(
        **_entry(
            kind="generic",
            binary="mailhog",
            external_required=True,
            required_env_vars=["MAILHOG_HOST"],
        ),
    )
    assert entry.start_args is None


def test_health_command_must_be_non_empty_argv_without_nuls():
    with pytest.raises(ValidationError, match="health_command"):
        ServiceEntry(
            **_entry(kind="generic", binary="x", start_args=["x"], health_command=[]),
        )
    with pytest.raises(ValidationError, match="NUL"):
        ServiceEntry(
            **_entry(
                kind="generic", binary="x", start_args=["x"],
                health_command=["x\x00y"],
            ),
        )


def test_non_postgres_init_still_rejected():
    with pytest.raises(ValidationError, match="init"):
        ServiceEntry(**_entry(kind="mysql", init={"superuser": "root"}))


# ---------------------------------------------------------------- templater


def _manifest(services):
    return ServicesManifest(services=services, cache_inputs=[], agent_version="t-413")


def _generic():
    return ServiceEntry(
        **_entry(
            name="worker",
            binary="/bin/app",
            kind="generic",
            start_args=["/bin/app", "--listen", "9412"],
            health_command=["/bin/app", "--health"],
        ),
    )


@pytest.mark.parametrize(
    ("kind", "needle"),
    [
        ("redis", "--dir="),
        ("mysql", "--datadir="),
        ("mongodb", "--dbpath="),
        ("rabbitmq", "RABBITMQ_NODE_PORT="),
        ("elasticsearch", "http.port="),
        ("memcached", "-p "),
        ("minio", "server "),
        ("generic", "/bin/app --listen 9412"),
    ],
)
def test_per_kind_launch_lines(kind, needle):
    svc = ServiceEntry(
        **_entry(
            kind=kind,
            binary=kind if kind != "generic" else "/bin/app",
            start_args=["/bin/app", "--listen", "9412"] if kind == "generic" else None,
            health_command=["/bin/app", "--health"] if kind == "generic" else None,
        ),
    )
    scripts = render(_manifest([svc]))
    assert needle in scripts.start, f"kind {kind} launch missing {needle!r}"


def test_redis_launch_no_longer_uses_generic_data_dir_flag():
    scripts = render(_manifest([ServiceEntry(**_entry(kind="redis", binary="redis-server"))]))
    assert "--data-dir=" not in scripts.start


def test_generic_launch_is_verbatim_start_args_not_a_default():
    scripts = render(_manifest([_generic()]))
    assert "/bin/app --listen 9412" in scripts.start
    assert "--data-dir=" not in scripts.start


def test_generic_start_args_are_shell_quoted_against_injection():
    # start_args is an argv list, not a shell string: an element containing shell
    # metacharacters is emitted as ONE shell-quoted argument, never executed.
    scripts = render(
        _manifest(
            [
                ServiceEntry(
                    **_entry(
                        kind="generic",
                        binary="/bin/app",
                        start_args=["echo pwned; touch /tmp/x", "--listen"],
                        health_command=["/bin/app", "--health"],
                    ),
                ),
            ],
        ),
    )
    # The payload is emitted as ONE shell-quoted argument followed by the next
    # argv element — the metacharacters are inert shell text, never syntax.
    assert "'echo pwned; touch /tmp/x' --listen >" in scripts.start


def test_dead_daemon_detected_at_start_with_log_tail():
    # Every launched service gets pid-liveness detection after a short grace;
    # a dead daemon is a START failure (exit 75) with its log tail, not a
    # silent "started" followed by a health failure.
    for svc in (
        _generic(),
        ServiceEntry(**_entry(kind="redis", binary="redis-server")),
        ServiceEntry(**_entry(kind="mysql", binary="mysqld")),
    ):
        scripts = render(_manifest([svc]))
        assert "kill -0" in scripts.start
        assert "tail -n 40" in scripts.start
        assert "died at start" in scripts.start
        assert "exit 75" in scripts.start


def test_per_kind_readiness_budgets_are_bounded():
    # Timeouts are per-kind (postgres 180 init is the ceiling the outer cap
    # covers); each kind's start script polls its own probe up to that budget.
    budgets = {
        "redis": 30, "mysql": 60, "mongodb": 60, "rabbitmq": 60,
        "elasticsearch": 90, "memcached": 15, "minio": 30,
    }
    for kind, budget in budgets.items():
        svc = ServiceEntry(**_entry(kind=kind, binary="b"))
        scripts = render(_manifest([svc]))
        assert f'while [ "$_svc_wait" -lt {budget} ]' in scripts.start, kind
        assert "did not become ready within" in scripts.start, kind


def test_generic_start_polls_health_command():
    scripts = render(_manifest([_generic()]))
    assert "/bin/app --health" in scripts.start
    assert 'while [ "$_svc_wait" -lt 30 ]' in scripts.start


# ------------------------------------------------- health probes


def test_health_probes_are_protocol_level_per_kind():
    probes = {
        "redis": "redis-cli",
        "mysql": "mariadb-admin",
        "mongodb": "mongosh",
        "rabbitmq": "rabbitmq-diagnostics",
        "elasticsearch": "_cluster/health",
        "memcached": "VERSION",
        "minio": "minio/health/live",
        "generic": "/bin/app --health",
    }
    for kind, needle in probes.items():
        svc = ServiceEntry(
            **_entry(
                kind=kind,
                binary="b" if kind != "generic" else "/bin/app",
                start_args=["/bin/app", "--x"] if kind == "generic" else None,
                health_command=["/bin/app", "--health"] if kind == "generic" else None,
            ),
        )
        scripts = render(_manifest([svc]))
        assert needle in scripts.health, f"kind {kind} health probe missing {needle!r}"


def test_health_probes_are_not_tcp_only():
    # The bare TCP connect remains only as the last-resort generic fallback
    # inside memcached's text probe; no managed kind relies on it alone.
    scripts = render(
        _manifest(
            [
                ServiceEntry(**_entry(name="redis_db", kind="redis", binary="redis-server")),
                ServiceEntry(**_entry(name="mysql_db", kind="mysql", binary="mysqld")),
            ],
        ),
    )
    assert "_port_open" not in scripts.health


# ------------------------------------------------- env_manifest


def test_service_kind_packages_cover_new_kinds():
    from coordinare.services.env_manifest import _SERVICE_KIND_PACKAGES

    assert _SERVICE_KIND_PACKAGES["mysql"] == ("mariadb-server", "mariadb-client")
    assert _SERVICE_KIND_PACKAGES["mongodb"] == ("mongodb-server", "mongodb-clients")
    assert _SERVICE_KIND_PACKAGES["rabbitmq"] == ("rabbitmq-server",)
    assert _SERVICE_KIND_PACKAGES["elasticsearch"] == ("elasticsearch", "curl")
    assert _SERVICE_KIND_PACKAGES["memcached"] == ("memcached",)
    assert _SERVICE_KIND_PACKAGES["minio"] == ("minio", "curl")


def test_derive_install_items_for_mysql_and_memcached():
    from coordinare.services.env_manifest import derive_service_install_items

    items = derive_service_install_items(
        [
            ServiceEntry(**_entry(kind="mysql", binary="mysqld")),
            ServiceEntry(**_entry(kind="memcached", binary="memcached", name="mem")),
        ],
    )
    names = [i.name for i in items]
    assert names == ["mariadb-server", "mariadb-client", "memcached"]


def test_service_readiness_check_covers_new_kinds():
    from coordinare.services.env_manifest import _service_readiness_check

    for kind, needle in (
        ("redis", "redis-cli"),
        ("mysql", "mariadb-admin"),
        ("mongodb", "mongosh"),
        ("rabbitmq", "rabbitmq-diagnostics"),
        ("elasticsearch", "_cluster/health"),
        ("minio", "minio/health/live"),
    ):
        probe = _service_readiness_check(ServiceEntry(**_entry(kind=kind, binary="b")))
        assert probe is not None, kind
        assert needle in probe, kind


# ------------------------------------------------- prompt agreement


def test_prompt_hostable_set_agrees_with_schema():
    from coordinare_service_inference.prompt import render_system_prompt

    text = render_system_prompt("t-413")
    for kind in ("postgres", "redis", "mysql", "mongodb", "rabbitmq",
                 "elasticsearch", "memcached", "minio", "generic"):
        assert kind in text, f"prompt must mention kind {kind}"
    # Kafka and localstack have NO coordinare recipe: they must no longer be
    # named as hostable in-container services.
    assert "kafka" not in text.lower() or "generic" in text.lower()
    for field in ("start_args", "health_command"):
        assert field in text


# ------------------------------------------------- persona text


def test_qa_persona_does_not_assert_which_services_run():
    from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS

    qa = DEFAULT_INSTRUCTIONS["qa"]
    assert "ALREADY RUNNING" not in qa
    assert "Postgres and Redis are" not in qa
    # The persona must instead point at the declared services manifest/env.
    assert "services manifest" in qa


# ------------------------------------------------- review-475 fixes


def _render_one(**overrides):
    svc = ServiceEntry(**_entry(**overrides))
    return render(_manifest([svc]))


def test_memcached_probe_opens_fd_in_function_shell():
    # Review 475: `(exec 3<>...)` opens the fd only in a subshell, so the parent's
    # printf/read always failed and memcached readiness could never succeed. The
    # fd must be opened in the function shell in BOTH templates.
    scripts = _render_one(kind="memcached", binary="memcached", name="mem")
    for body in (scripts.start, scripts.health):
        assert "(exec 3<>/dev/tcp" not in body
        assert 'exec 3<>/dev/tcp/127.0.0.1/"$port" 2>/dev/null || return 1' in body


def test_mysql_init_failure_is_attributed_not_swallowed():
    # Review 475: mariadb-install-db is the single supported init (no MySQL-style
    # --initialize-insecure fallback), and its failure must NOT be swallowed by
    # `|| true` — it is attributed loudly (log tail + exit 75).
    scripts = _render_one(kind="mysql", binary="mysqld", name="db")
    assert "--initialize-insecure" not in scripts.start
    assert "install-db.log" in scripts.start
    assert "first-run init failed (mariadb-install-db)" in scripts.start
    assert "exit 75" in scripts.start


def test_rabbitmq_launches_as_unprivileged_user():
    # Review 475: RabbitMQ refuses to run as root — launch via runuser as a
    # dedicated user with its HOME under the data dir.
    scripts = _render_one(kind="rabbitmq", binary="rabbitmq-server", name="mq")
    assert "rabbitrunner" in scripts.start
    assert 'runuser -u "$_rabbitmquser"' in scripts.start
    assert 'HOME=' in scripts.start


def test_memcached_runs_as_nobody():
    # Review 475: the daemon is reachable by the test workload; keep it out of root.
    scripts = _render_one(kind="memcached", binary="memcached", name="mem")
    assert "-u nobody" in scripts.start
    assert "-u root" not in scripts.start


def test_env_manifest_mysql_readiness_line_is_valid_shell():
    # Review 475: `(command -v ... ) -h` is a syntax error — the binary must be
    # selected via command substitution before the ping invocation.
    from coordinare.services.env_manifest import _service_readiness_check

    probe = _service_readiness_check(ServiceEntry(**_entry(kind="mysql", binary="mysqld")))
    assert 'mariadb_bin="$(command -v mariadb-admin || command -v mysqladmin)"' in probe
    assert "[ -n \"$mariadb_bin\" ] && \"$mariadb_bin\"" in probe
    # The invalid pre-fix form invoked the command directly off the conditional.
    assert ") -h" not in probe


def test_activate_path_covers_sbin_and_elasticsearch_bins():
    # Review 475: mysqld / rabbitmq-* live in /usr/sbin and the elasticsearch
    # deb's binary lives under /usr/share/elasticsearch/bin — neither is under
    # the usr/bin glob the activate prelude already prepends.
    from coordinare.services.env_manifest import render_activate_sh

    manifest = EnvManifest(symphony_name="sym", items=[])
    text = render_activate_sh(manifest, cache_mount_path="/devenv/sym")
    assert '"$DEVENV"/*/usr/sbin' in text
    assert '"$DEVENV"/*/usr/share/elasticsearch/bin' in text


def test_vendor_repo_kinds_get_loud_install_hint():
    from coordinare.services.env_manifest import derive_service_install_items

    items = derive_service_install_items(
        [ServiceEntry(**_entry(kind="mongodb", binary="mongod"))],
    )
    hints = " ".join(i.install_hint for i in items)
    assert "vendor repository" in hints
    # postgres/redis/mariadb/memcached/minio ship from the Debian archive; no note.
    plain = derive_service_install_items(
        [ServiceEntry(**_entry(kind="memcached", binary="memcached", name="mem"))],
    )
    assert "vendor repository" not in " ".join(i.install_hint for i in plain)


# ------------------------------------------------- review-475 round-2 fixes


def test_maria_init_uses_supported_flag():
    # Review 475 r2: mariadb-install-db/mariadbd do not know
    # --skip-name-processing; the supported option is --skip-name-resolve.
    scripts = _render_one(kind="mysql", binary="mysqld", name="db")
    assert "--skip-name-resolve" in scripts.start
    assert "--skip-name-processing" not in scripts.start


def test_stop_scopes_user_sweeps_to_the_target_service():
    # Review 475 r2: `pkill -u <shared-account>` kills every service under that
    # account — two kind:mysql (or elasticsearch) entries would take each other
    # down. The sweep must be scoped to this service's argv signature.
    scripts = _render_one(kind="mysql", binary="mysqld", name="mysql")
    assert 'pkill -TERM -u mysqlrunner -f "${MYSQL_DATA_DIR}"' in scripts.stop
    scripts_es = _render_one(kind="elasticsearch", binary="elasticsearch", name="es")
    assert 'pkill -TERM -u esrunner -f "http.port=${ES_PORT}"' in scripts_es.stop


def test_rabbitmq_readiness_runs_as_service_user_with_broker_home():
    # Review 475 r2: rabbitmq-diagnostics reads the Erlang cookie from HOME —
    # run as the service user with the broker's HOME in BOTH scripts.
    scripts = _render_one(kind="rabbitmq", binary="rabbitmq-server", name="mq")
    needle = 'runuser -u rabbitrunner -- env HOME="${MQ_DATA_DIR}" rabbitmq-diagnostics'
    assert needle in scripts.start
    assert needle in scripts.health


def test_postgres_launch_has_the_dead_daemon_guard():
    # Review 475 r2 (previously missed): a postgres that exits at start must be
    # attributed with its log tail, not misreported as a readiness timeout.
    scripts = _render_one(kind="postgres", binary="postgres", name="pg")
    assert "daemon died at start" in scripts.start
    assert "tail -n 40" in scripts.start
