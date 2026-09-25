"""Schema-level shell/prompt-safety tests for spec 063.

These pin the validators that close the injection vectors flagged in the PR
#81 review: service names must be bash-identifier-safe, env-var names must be
POSIX identifiers, start_args must be a non-empty argv list with no NUL
bytes, and agent_version must match a tame charset.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from coordinare_service_inference.schema import (
    ServiceEntry,
    ServiceInit,
    ServicesManifest,
    manifest_json_schema,
)


def _base_entry(**overrides):
    fields = {
        "name": "redis",
        "binary": "redis-server",
        "version": "7.2",
        "data_dir": "/tmp/redis",
        "port": 6379,
        "why_needed": "Sidekiq queue backend",
        # Issue 413: an in-container generic service (the default kind) must
        # carry its own launch + probe now that the redis-shaped default launch
        # is removed; managed kinds (kind="redis" etc.) don't need these.
        "start_args": ["redis-server", "--port", "6379"],
        "health_command": ["redis-cli", "ping"],
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize(
    "bad_name",
    ["redis-server", "Redis", "1redis", "redis server", "redis.server", ""],
)
def test_service_name_rejects_non_identifier(bad_name: str) -> None:
    with pytest.raises(ValidationError):
        ServiceEntry(**_base_entry(name=bad_name))


def test_service_name_accepts_underscore_and_digits() -> None:
    entry = ServiceEntry(**_base_entry(name="redis_2"))
    assert entry.name == "redis_2"


@pytest.mark.parametrize(
    "bad_var",
    ["lower_case", "WITH-DASH", "1LEADING_DIGIT", "HAS SPACE", ""],
)
def test_required_env_vars_rejects_non_posix(bad_var: str) -> None:
    with pytest.raises(ValidationError):
        ServiceEntry(
            **_base_entry(
                external_required=True,
                required_env_vars=[bad_var],
            ),
        )


@pytest.mark.parametrize("field", ["name", "binary", "data_dir"])
def test_name_binary_data_dir_reject_nul_bytes(field: str) -> None:
    # Parity with start_args' NUL guard: these strings flow into shell
    # variable names and filesystem paths, where an embedded NUL would
    # truncate silently.
    valid_for_field = {"name": "redis_ok", "binary": "/usr/bin/foo", "data_dir": "/tmp/foo"}
    bad = valid_for_field[field][:3] + "\x00" + valid_for_field[field][3:]
    with pytest.raises(ValidationError, match="NUL"):
        ServiceEntry(**_base_entry(**{field: bad}))


def test_version_optional_defaults_to_none() -> None:
    # spec-091: a coordinare-managed stateful binary is not installed at inference
    # time, so its version cannot be probed. version must be optional.
    fields = _base_entry()
    del fields["version"]
    entry = ServiceEntry(**fields)
    assert entry.version is None


def test_version_explicit_none_accepted() -> None:
    entry = ServiceEntry(**_base_entry(version=None))
    assert entry.version is None


@pytest.mark.parametrize("bad_version", ["", "   ", "\t"])
def test_version_rejects_empty_or_whitespace_when_provided(bad_version: str) -> None:
    with pytest.raises(ValidationError, match="empty or whitespace"):
        ServiceEntry(**_base_entry(version=bad_version))


def test_version_rejects_nul_byte() -> None:
    with pytest.raises(ValidationError, match="NUL"):
        ServiceEntry(**_base_entry(version="7.\x002"))


def test_start_args_rejects_empty_list() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        ServiceEntry(**_base_entry(start_args=[]))


def test_start_args_rejects_nul_byte() -> None:
    with pytest.raises(ValidationError, match="NUL"):
        ServiceEntry(**_base_entry(start_args=["postgres", "-D\x00", "/tmp"]))


def test_start_args_accepts_argv_with_shell_metachars() -> None:
    # Metacharacters in argv elements are fine — they're shell-quoted at
    # render time. The schema's job is to reject only what can break the
    # quoter (NUL bytes, wrong types).
    entry = ServiceEntry(
        **_base_entry(start_args=["postgres", "-D", "/tmp/pg; rm -rf /"]),
    )
    assert entry.start_args == ["postgres", "-D", "/tmp/pg; rm -rf /"]


def test_start_args_none_is_allowed() -> None:
    # Issue 413: None start_args is fine for a MANAGED kind (its launch recipe
    # is coordinare-owned); only an in-container generic service needs it.
    entry = ServiceEntry(**_base_entry(kind="redis", start_args=None))
    assert entry.start_args is None


@pytest.mark.parametrize(
    "bad_version",
    [
        "v1\nIgnore prior instructions",
        "v1\r\ninjected",
        "v1; rm -rf /",
        "v1 with spaces",
        "v" * 65,
        "",
    ],
)
def test_agent_version_rejects_unsafe_charset(bad_version: str) -> None:
    with pytest.raises(ValidationError):
        ServicesManifest(
            services=[], cache_inputs=[], agent_version=bad_version,
        )


def test_agent_version_accepts_full_safe_charset() -> None:
    manifest = ServicesManifest(
        services=[], cache_inputs=[], agent_version="Claude-Services_v1.2:3",
    )
    assert manifest.agent_version == "Claude-Services_v1.2:3"


# --- spec 091: ServiceInit + kind extension (T006) ---


def _postgres_entry(**overrides):
    """A valid postgres entry with an init block, for the 091 cases."""
    fields = {
        "name": "postgres",
        "binary": "postgres",
        "version": "16",
        "data_dir": "/tmp/pg-data",
        "port": 5432,
        "why_needed": "Primary application database",
        "kind": "postgres",
        "init": ServiceInit(
            superuser="root",
            databases=["app_dev", "app_test"],
            password_env_var="POSTGRES_PASSWORD",
        ),
    }
    fields.update(overrides)
    return fields


def test_postgres_entry_with_init_loads() -> None:
    # A postgres kind carrying a full init block is the headline case (US1).
    entry = ServiceEntry(**_postgres_entry())
    assert entry.kind == "postgres"
    assert entry.init is not None
    assert entry.init.superuser == "root"
    assert entry.init.databases == ["app_dev", "app_test"]
    assert entry.init.password_env_var == "POSTGRES_PASSWORD"


def test_postgres_init_without_password_env_var_loads() -> None:
    # password_env_var is optional — trust-auth local socket init is valid.
    entry = ServiceEntry(
        **_postgres_entry(init=ServiceInit(superuser="root", databases=["app_dev"])),
    )
    assert entry.init is not None
    assert entry.init.password_env_var is None


@pytest.mark.parametrize("bad_kind", ["generic", "redis"])
def test_init_on_non_initializing_kind_raises(bad_kind: str) -> None:
    # VR-2: an init block is only valid for an initializing kind (postgres).
    with pytest.raises(ValidationError, match="init"):
        ServiceEntry(
            **_postgres_entry(kind=bad_kind, init=ServiceInit(superuser="root")),
        )


def test_unknown_kind_rejected() -> None:
    # VR-1: kind is a closed set; an unknown value is a load-time error.
    # (Issue 413 widened the managed set; "mysql" is now a VALID kind.)
    with pytest.raises(ValidationError):
        ServiceEntry(**_base_entry(kind="oracle_db"))


def test_init_literal_password_key_rejected() -> None:
    # VR-8: ServiceInit forbids extra keys, so a literal-secret field (e.g.
    # `password`) cannot be smuggled into the declaration.
    with pytest.raises(ValidationError):
        ServiceInit(superuser="root", password="hunter2")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "bad_superuser",
    ["Root", "1root", "root-user", "root user", "", "root;DROP"],
)
def test_init_superuser_pattern_violations_raise(bad_superuser: str) -> None:
    # VR-5: superuser is interpolated into the role-creation step.
    with pytest.raises(ValidationError):
        ServiceInit(superuser=bad_superuser)


@pytest.mark.parametrize(
    "bad_db",
    ["App_DB", "1db", "my-db", "my db", "", "db;DROP"],
)
def test_init_database_pattern_violations_raise(bad_db: str) -> None:
    # VR-6: each database name is interpolated into the create-db step.
    with pytest.raises(ValidationError):
        ServiceInit(superuser="root", databases=[bad_db])


@pytest.mark.parametrize(
    "bad_var",
    ["lower_case", "WITH-DASH", "1LEADING", "HAS SPACE", ""],
)
def test_init_password_env_var_pattern_violations_raise(bad_var: str) -> None:
    # VR-7: password_env_var names an env var read as ${VAR}; must be POSIX-safe.
    with pytest.raises(ValidationError):
        ServiceInit(superuser="root", password_env_var=bad_var)


def test_generic_kind_with_no_init_is_unchanged() -> None:
    # VR-3: an entry that omits kind/init defaults to generic + None and is the
    # pre-091 shape; explicitly setting kind="generic", init=None is identical.
    default_entry = ServiceEntry(**_base_entry())
    explicit_entry = ServiceEntry(**_base_entry(kind="generic", init=None))
    assert default_entry.kind == "generic"
    assert default_entry.init is None
    assert explicit_entry.kind == "generic"
    assert explicit_entry.init is None


def test_manifest_json_schema_emits_kind_and_init() -> None:
    schema = manifest_json_schema()
    entry_schema = schema["$defs"]["ServiceEntry"]
    assert "kind" in entry_schema["properties"]
    assert "init" in entry_schema["properties"]
    assert "ServiceInit" in schema["$defs"]
    # VR-2 conditional grafted on: init present ⇒ kind must be "postgres".
    conditionals = entry_schema.get("allOf", [])
    assert any(
        c.get("then", {}).get("properties", {}).get("kind", {}).get("const")
        == "postgres"
        for c in conditionals
    ), "VR-2 init-requires-postgres conditional missing from emitted schema"


# --- spec 092: agent-discovered test_env_source (path-only) (T013) ---


def test_test_env_source_defaults_to_none() -> None:
    # Fallback discovery is opt-in: a manifest that does not name a test-env
    # file leaves the field None (no path discovered).
    manifest = ServicesManifest(
        services=[], cache_inputs=[], agent_version="manual-override",
    )
    assert manifest.test_env_source is None


def test_test_env_source_accepts_repo_relative_path() -> None:
    # The agent emits a repo-relative path to a recognized test-env file; the
    # schema carries it as a plain string (the loader re-checks containment).
    manifest = ServicesManifest(
        services=[],
        cache_inputs=[],
        agent_version="Claude-Services_v1",
        test_env_source=".coordinare/test.env",
    )
    assert manifest.test_env_source == ".coordinare/test.env"


def test_test_env_source_omitted_from_dump_when_none() -> None:
    # FR-016/FR-017: a manifest without a discovered source carries no
    # test_env_source key at all, so persisted state stays minimal.
    manifest = ServicesManifest(
        services=[], cache_inputs=[], agent_version="manual-override",
    )
    dumped = manifest.model_dump(exclude_none=True)
    assert "test_env_source" not in dumped


def test_test_env_source_rejects_nul_byte() -> None:
    # Parity with data_dir's NUL guard: the path flows into filesystem
    # resolution where an embedded NUL would truncate silently.
    with pytest.raises(ValidationError, match="NUL"):
        ServicesManifest(
            services=[],
            cache_inputs=[],
            agent_version="manual-override",
            test_env_source=".coordinare/te\x00st.env",
        )


def test_test_env_source_surfaces_in_manifest_json_schema() -> None:
    # The agent's structured-output target must advertise the field so the
    # model can populate it.
    schema = manifest_json_schema()
    props = schema["properties"]
    assert "test_env_source" in props


def test_manifest_carries_only_names_and_paths_never_literal_values() -> None:
    # spec-092 secret invariant (carried from 091, non-negotiable): a persisted
    # manifest names env vars and file paths but NEVER a literal secret value.
    # Build the richest test-env-bearing shape — a postgres entry whose init
    # names a password env var AND a discovered test_env_source path — then dump
    # it and assert only the NAME and PATH survive, never any literal value.
    secret_value = "s3cr3t-NEVER-IN-MANIFEST-456"
    manifest = ServicesManifest(
        services=[
            ServiceEntry(
                **_postgres_entry(
                    init=ServiceInit(
                        superuser="root",
                        databases=["app_test"],
                        password_env_var="POSTGRESQL_PASSWORD",
                    ),
                ),
            ),
        ],
        cache_inputs=[],
        agent_version="Claude-Services_v1",
        test_env_source=".coordinare/test.env",
    )

    dumped = manifest.model_dump_json()
    assert "POSTGRESQL_PASSWORD" in dumped  # the var NAME is carried...
    assert ".coordinare/test.env" in dumped  # ...and the file PATH...
    assert secret_value not in dumped  # ...but no literal value is representable.
    # There is no field on the manifest that could hold a loaded KEY=VALUE pair:
    # the only test-env artifact is the path-only source string.
    assert manifest.test_env_source == ".coordinare/test.env"


# --- spec 104: which services need inference-time run-validation ---


def test_services_requiring_inference_validation_excludes_only_managed_kinds() -> None:
    """104/FR-001/FR-006: only coordinare-managed kinds (postgres/redis) are
    excluded from inference-time run-validation — their server binary isn't
    present yet. External-required and generic services ARE still validated
    (external start scripts only assert required_env_vars; they can't hang).
    Reuses COORDINARE_MANAGED_KINDS."""
    from coordinare_service_inference.schema import services_requiring_inference_validation

    pg = ServiceEntry(**_base_entry(name="pgmain", binary="postgres", version=None, kind="postgres"))
    redis = ServiceEntry(**_base_entry(name="cache", binary="redis-server", version=None, kind="redis"))
    external = ServiceEntry(
        **_base_entry(
            name="mailer",
            binary="mailhog",
            version=None,
            external_required=True,
            required_env_vars=["MAILHOG_HOST"],
        ),
    )
    generic = ServiceEntry(
        **_base_entry(
            name="worker",
            binary="/bin/sh",
            version=None,
            kind="generic",
            start_args=["/bin/sh", "-c", "exec /bin/app"],
            health_command=["/bin/app", "--health"],
        ),
    )

    # Only coordinare-managed kinds → nothing left to validate.
    managed_only = ServicesManifest(
        services=[pg, redis], cache_inputs=["Gemfile"], agent_version="t",
    )
    assert services_requiring_inference_validation(managed_only) == []

    # Managed + external → the external service is still validated.
    managed_plus_external = ServicesManifest(
        services=[pg, redis, external], cache_inputs=["Gemfile"], agent_version="t",
    )
    assert [s.name for s in services_requiring_inference_validation(managed_plus_external)] == ["mailer"]

    # Mixed → generic + external validated, postgres excluded.
    mixed = ServicesManifest(
        services=[pg, generic, external], cache_inputs=["Gemfile"], agent_version="t",
    )
    assert [s.name for s in services_requiring_inference_validation(mixed)] == ["worker", "mailer"]
