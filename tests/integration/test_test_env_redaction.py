"""Integration test for spec-092 US3: the secret invariant under redaction.

Carried verbatim from spec 091 and non-negotiable: across a full configured run, the
literal test-env VALUE must never appear in any captured log line, in the manifest, or in
persisted env-cache state. The values are secret-like and ride only the redacted
``secrets`` channel; structured log events emit only the var KEYS and a SOURCE label
(``host_path:<p>`` / ``repo_path:<p>`` / ``test_env_source:<p>``).

FR-017: no loaded test-env value is baked into persisted env-cache state. The persisted
state stores only the discovered PATH (``test_env_source``), so a reused cache reloads
the values from the source file at runtime rather than carrying them along.

This drives the shared :func:`coordinare.services.env_cache.resolve_test_env_vars`
resolver — the single production point where the values are loaded and logged — across all
three source kinds (host_path, repo_path, discovered fallback), plus the manifest and the
persisted-state models, and asserts the value leaks nowhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog.testing
from coordinare_service_inference.schema import ServiceEntry, ServiceInit, ServicesManifest

from coordinare.config import TestEnvConfig
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import resolve_test_env_vars
from coordinare.state_store import EnvCacheStateSnapshot

PW_VAR = "POSTGRESQL_PASSWORD"
# A distinctive literal value: if this string appears in any log/manifest/state dump the
# invariant is broken. Chosen to be unmistakable in a substring scan.
SECRET_VALUE = "s3cr3t-NEVER-LOG-THIS-789"
ORG = "vivi-org"
REPO = "website"
DISCOVERED_PATH = ".env.test"


class _FakeGitHub:
    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    async def get_file_content(self, org: str, repo: str, path: str) -> str | None:
        return self._files.get(path)


def _assert_logs_redacted(cap_logs: list[dict], *, expected_source: str) -> None:
    """The load event carries keys + source only; no value anywhere in the capture."""
    loaded_events = [e for e in cap_logs if e["event"] == "env_cache.test_env_loaded"]
    assert len(loaded_events) == 1, f"expected one load event, got {cap_logs}"
    ev = loaded_events[0]
    assert ev["keys"] == [PW_VAR]  # var NAME only.
    assert ev["source"] == expected_source  # path/source label, never a value.
    # Belt and braces: serialise EVERY captured event and confirm zero value leakage.
    assert SECRET_VALUE not in json.dumps(cap_logs)


class TestTestEnvRedaction:
    async def test_host_path_source_logs_keys_and_source_only(
        self, tmp_path: Path
    ) -> None:
        host_file = tmp_path / "host-secrets.env"
        host_file.write_text(f"{PW_VAR}={SECRET_VALUE}\n", encoding="utf-8")

        with structlog.testing.capture_logs() as cap_logs:
            loaded = await resolve_test_env_vars(
                symphony_name="website",
                test_env=TestEnvConfig(host_path=str(host_file)),
                github_org=ORG,
                repo=REPO,
                github_service=_FakeGitHub({}),
            )

        assert loaded == {PW_VAR: SECRET_VALUE}  # value reaches the redacted channel...
        _assert_logs_redacted(cap_logs, expected_source=f"host_path:{host_file}")

    async def test_repo_path_source_logs_keys_and_source_only(self) -> None:
        github = _FakeGitHub({DISCOVERED_PATH: f"{PW_VAR}={SECRET_VALUE}\n"})

        with structlog.testing.capture_logs() as cap_logs:
            loaded = await resolve_test_env_vars(
                symphony_name="website",
                test_env=TestEnvConfig(repo_path=DISCOVERED_PATH),
                github_org=ORG,
                repo=REPO,
                github_service=github,
            )

        assert loaded == {PW_VAR: SECRET_VALUE}
        _assert_logs_redacted(cap_logs, expected_source=f"repo_path:{DISCOVERED_PATH}")

    async def test_discovered_fallback_logs_keys_and_source_only(self) -> None:
        github = _FakeGitHub({DISCOVERED_PATH: f"{PW_VAR}={SECRET_VALUE}\n"})

        with structlog.testing.capture_logs() as cap_logs:
            loaded = await resolve_test_env_vars(
                symphony_name="website",
                test_env=None,
                github_org=ORG,
                repo=REPO,
                github_service=github,
                fallback_source=DISCOVERED_PATH,
            )

        assert loaded == {PW_VAR: SECRET_VALUE}
        _assert_logs_redacted(
            cap_logs, expected_source=f"test_env_source:{DISCOVERED_PATH}"
        )

    def test_manifest_carries_path_only_never_value(self, tmp_path: Path) -> None:
        manifest = ServicesManifest(
            services=[
                ServiceEntry(
                    name="postgres",
                    binary="postgres",
                    version="16",
                    data_dir=str(tmp_path / "pgdata"),
                    port=59432,
                    why_needed="Primary application database",
                    sources=["config/database.yml"],
                    kind="postgres",
                    init=ServiceInit(
                        superuser="root",
                        databases=["app_test"],
                        password_env_var=PW_VAR,  # a NAME reference, never a value.
                    ),
                )
            ],
            cache_inputs=[],
            agent_version="test-092",
            test_env_source=DISCOVERED_PATH,
        )

        dumped = manifest.model_dump_json()
        assert DISCOVERED_PATH in dumped  # path is present...
        assert PW_VAR in dumped  # var NAME is present...
        assert SECRET_VALUE not in dumped  # ...but never the literal value.

    def test_fr017_persisted_state_stores_path_only_never_value(self) -> None:
        # The live state and the on-disk snapshot model both carry the discovered PATH
        # and provide NO field for loaded values — so a reused cache cannot resurface a
        # baked-in secret; it must reload from the source file.
        state = EnvCacheState(
            symphony_name="website",
            sanitised_name="website",
            cache_dir=Path("/tmp/env-cache/website"),
            test_env_source=DISCOVERED_PATH,
        )
        snapshot = EnvCacheStateSnapshot(
            symphony_name="website",
            sanitised_name="website",
            cache_dir="/tmp/env-cache/website",
            test_env_source=DISCOVERED_PATH,
        )

        for model in (state, snapshot):
            dumped = model.model_dump_json()
            assert DISCOVERED_PATH in dumped
            assert SECRET_VALUE not in dumped
            # No loaded KEY=VALUE pair is representable on the persisted state at all.
            assert PW_VAR not in dumped
