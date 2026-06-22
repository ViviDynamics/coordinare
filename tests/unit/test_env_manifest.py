"""Unit tests for coordinare-derived env manifest (077)."""

from __future__ import annotations

from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.services.env_manifest import (
    STRUCTURED_SPEC_FILES,
    derive_manifest,
    render_activate_sh,
    render_verify_sh,
)


class TestDeterministicParsers:
    def test_ruby_version(self) -> None:
        m = derive_manifest("sym", {".ruby-version": "3.4.2\n"})
        ruby = [i for i in m.items if i.name == "ruby"]
        assert len(ruby) == 1
        assert ruby[0].kind == "runtime"
        assert ruby[0].version == "3.4.2"
        assert ruby[0].source == ".ruby-version"

    def test_ruby_version_strips_leading_v(self) -> None:
        m = derive_manifest("sym", {".ruby-version": "v3.4.2"})
        assert m.items[0].version == "3.4.2"

    def test_tool_versions_multi(self) -> None:
        content = "ruby 3.4.2\nnodejs 20.11.0\n# comment\npython 3.12.1\n"
        m = derive_manifest("sym", {".tool-versions": content})
        names = {i.name: i.version for i in m.items}
        assert names == {"ruby": "3.4.2", "node": "20.11.0", "python": "3.12.1"}

    def test_nvmrc(self) -> None:
        m = derive_manifest("sym", {".nvmrc": "v20.11.0\n"})
        assert m.items[0].name == "node"
        assert m.items[0].version == "20.11.0"

    def test_package_json_engines(self) -> None:
        content = '{"engines": {"node": ">=20", "npm": "10.x"}}'
        m = derive_manifest("sym", {"package.json": content})
        names = {i.name: i.version for i in m.items}
        assert names == {"node": ">=20", "npm": "10.x"}

    def test_package_json_malformed_is_ignored(self) -> None:
        m = derive_manifest("sym", {"package.json": "{not json"})
        assert m.items == []

    def test_gemfile_gems(self) -> None:
        content = (
            'source "https://rubygems.org"\n'
            'gem "rails", "~> 7.1"\n'
            "gem 'pg'\n"
            'gem "capybara"\n'
            'gem "rails"\n'  # duplicate — deduped
        )
        m = derive_manifest("sym", {"Gemfile": content})
        gems = sorted(i.name for i in m.items if i.kind == "gem")
        assert gems == ["capybara", "pg", "rails"]

    def test_gemfile_lock_bundler(self) -> None:
        content = "GEM\n  remote: https://rubygems.org/\n\nBUNDLED WITH\n   2.5.6\n"
        m = derive_manifest("sym", {"Gemfile.lock": content})
        bundler = [i for i in m.items if i.name == "bundler"]
        assert len(bundler) == 1
        assert bundler[0].version == "2.5.6"
        assert bundler[0].kind == "gem"

    def test_dedup_prefers_version_bearing(self) -> None:
        # .tool-versions has the pin; a hypothetical bare ruby should not clobber it.
        m = derive_manifest(
            "sym",
            {".tool-versions": "ruby 3.4.2\n", ".ruby-version": "3.4.2\n"},
        )
        ruby = [i for i in m.items if i.name == "ruby"]
        assert len(ruby) == 1
        assert ruby[0].version == "3.4.2"

    def test_unknown_files_ignored(self) -> None:
        m = derive_manifest("sym", {"README.md": "# install ruby and chrome"})
        assert m.items == []

    def test_empty_content_skipped(self) -> None:
        m = derive_manifest("sym", {".ruby-version": ""})
        assert m.items == []

    def test_structured_files_constant_complete(self) -> None:
        # The fetch list must cover every parser we ship.
        for fname in (".ruby-version", "Gemfile", "package.json", ".tool-versions"):
            assert fname in STRUCTURED_SPEC_FILES


class TestVerifyRenderer:
    def _manifest(self) -> EnvManifest:
        return EnvManifest(
            symphony_name="sym",
            items=[
                ManifestItem(name="ruby", kind="runtime", version="3.4.2", source=".ruby-version"),
                ManifestItem(name="bundler", kind="gem", version="2.5.6", source="Gemfile.lock"),
                ManifestItem(name="rails", kind="gem", source="Gemfile"),
                ManifestItem(name="chromium", kind="system", source="README.md"),
            ],
        )

    def test_sources_activate_and_exits(self) -> None:
        sh = render_verify_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert "/devenv/sym/activate.sh" in sh
        assert "FAILED=0" in sh
        assert "exit 1" in sh
        assert "exit 0" in sh

    def test_ruby_version_pinned_check(self) -> None:
        sh = render_verify_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert "RUBY_VERSION" in sh
        assert "3.4.2" in sh
        assert 'want 3.4.2' in sh

    def test_gem_and_system_checks_present(self) -> None:
        sh = render_verify_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert "gem rails" in sh.lower() or "bundle show rails" in sh
        assert "chromium" in sh

    def test_system_pkg_check_is_soft_runtime_gem_hard(self) -> None:
        """077: system packages (chromium/psql) are test/qa-only — a missing one
        must WARN, never set FAILED (don't block the whole pipeline). Runtimes and
        gems stay hard (FAILED=1)."""
        sh = render_verify_sh(self._manifest(), cache_mount_path="/devenv/sym")
        # The chromium (system) line warns and does not flip FAILED.
        chromium_line = next(ln for ln in sh.splitlines() if "chromium" in ln)
        assert "WARN" in chromium_line and "FAILED=1" not in chromium_line
        # The ruby (runtime) and rails (gem) lines DO hard-fail.
        ruby_line = next(ln for ln in sh.splitlines() if "RUBY_VERSION" in ln)
        rails_line = next(ln for ln in sh.splitlines() if "gem rails" in ln)
        assert "FAILED=1" in ruby_line
        assert "FAILED=1" in rails_line

    def test_runtimes_rendered_before_gems(self) -> None:
        sh = render_verify_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert sh.index("RUBY_VERSION") < sh.index("rails")

    def test_rails_boot_smoke_test_emitted_when_rails_present(self) -> None:
        """When a ``rails`` gem is present, verify.sh must boot Rails to catch
        native-extension failures (e.g. psych without libyaml-dev) that pass a
        gem-presence check but fail at ``require`` time. The block hard-fails."""
        sh = render_verify_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert "Rails boot smoke-test" in sh
        assert "require 'rails'; require 'psych'" in sh
        # the smoke-test is a hard failure (sets FAILED), gated on the repo mount
        boot_line = next(ln for ln in sh.splitlines() if "require 'rails'" in ln)
        assert "FAILED=1" in boot_line
        assert 'if [ -d "/repo" ]; then' in sh

    def test_rails_boot_smoke_test_absent_when_no_rails(self) -> None:
        """A manifest with no ``rails`` gem must NOT emit the Rails-boot block —
        the smoke-test is Rails-specific and would fail spuriously elsewhere."""
        manifest = EnvManifest(
            symphony_name="sym",
            items=[
                ManifestItem(name="ruby", kind="runtime", version="3.4.2", source=".ruby-version"),
                ManifestItem(name="pg", kind="gem", source="Gemfile"),
            ],
        )
        sh = render_verify_sh(manifest, cache_mount_path="/devenv/sym")
        assert "Rails boot smoke-test" not in sh
        assert "require 'rails'" not in sh


class TestServiceReadinessChecklist:
    """093 / US2: verify.sh must include a RUNNING/healthy readiness line for each
    coordinare-managed service (postgres, redis) — not merely "installed". The probe
    is live (pg_isready / redis PING→PONG) and a not-running service hard-FAILs so
    the dispatch gate withholds against a half-built cache."""

    @staticmethod
    def _manifest() -> EnvManifest:
        # A minimal toolchain manifest; services are threaded separately (they do
        # not live on the EnvManifest — they reach render_verify_sh via services=).
        return EnvManifest(
            symphony_name="sym",
            items=[ManifestItem(name="ruby", kind="runtime", version="3.4.2", source=".ruby-version")],
        )

    @staticmethod
    def _service(**overrides):
        from coordinare_service_inference.schema import ServiceEntry, ServiceInit

        fields = dict(
            name="postgres",
            binary="postgres",
            version="16",
            data_dir="/tmp/pg-data",
            port=5432,
            why_needed="Primary application database",
            sources=["config/database.yml"],
            kind="postgres",
            init=ServiceInit(superuser="root", databases=["app_dev"]),
        )
        fields.update(overrides)
        return ServiceEntry(**fields)

    def test_postgres_readiness_uses_pg_isready_and_hard_fails(self) -> None:
        sh = render_verify_sh(
            self._manifest(), cache_mount_path="/devenv/sym", services=[self._service()]
        )
        # Live readiness probe (RUNNING, not merely installed) on the declared port.
        assert "pg_isready" in sh
        assert "5432" in sh
        # Installed-but-not-running ⇒ FAIL line that flips FAILED.
        pg_fail = next(ln for ln in sh.splitlines() if "FAIL:" in ln and "postgres" in ln)
        assert "FAILED=1" in pg_fail
        # Running/healthy ⇒ OK line.
        assert any("OK:" in ln and "postgres" in ln for ln in sh.splitlines())

    def test_redis_readiness_uses_ping_pong_and_hard_fails(self) -> None:
        sh = render_verify_sh(
            self._manifest(),
            cache_mount_path="/devenv/sym",
            services=[self._service(name="redis", binary="redis-server", kind="redis", port=6379, init=None)],
        )
        assert "redis-cli" in sh
        assert "PING" in sh
        assert "PONG" in sh
        assert "6379" in sh
        redis_fail = next(ln for ln in sh.splitlines() if "FAIL:" in ln and "redis" in ln)
        assert "FAILED=1" in redis_fail
        assert any("OK:" in ln and "redis" in ln for ln in sh.splitlines())

    def test_generic_and_external_services_emit_no_readiness_line(self) -> None:
        """Only coordinare-MANAGED services (postgres/redis) get a readiness probe.
        A generic/externally-required service is not coordinare's to start, so it
        must not appear as a FAIL-able readiness line."""
        sh = render_verify_sh(
            self._manifest(),
            cache_mount_path="/devenv/sym",
            services=[
                self._service(
                    name="elasticsearch", binary="elasticsearch", kind="generic", port=9200, init=None
                ),
                self._service(
                    name="vault",
                    binary="vault",
                    kind="generic",
                    port=8200,
                    external_required=True,
                    init=None,
                ),
            ],
        )
        assert "elasticsearch" not in sh
        assert "vault" not in sh

    def test_services_default_empty_is_backward_compatible(self) -> None:
        """Existing callers pass no services= — render must behave exactly as before
        (no readiness section, no crash)."""
        sh = render_verify_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert "pg_isready" not in sh
        assert "redis-cli" not in sh
        assert sh.rstrip().endswith("exit 0")

    def test_service_readiness_after_toolchain_before_aggregate_exit(self) -> None:
        """Ordering: service readiness runs after the toolchain checks but still
        feeds the single aggregate exit gate at the end."""
        sh = render_verify_sh(
            self._manifest(), cache_mount_path="/devenv/sym", services=[self._service()]
        )
        assert sh.index("RUBY_VERSION") < sh.index("pg_isready")
        assert sh.index("pg_isready") < sh.rindex('if [ "$FAILED" -ne 0 ]; then')

    def test_aggregate_exit_unchanged_nonzero_iff_failure(self) -> None:
        """T012/T016: WARN-only items never flip FAILED; the aggregate gate is the
        sole exit driver and is unchanged by the service section."""
        sh = render_verify_sh(
            self._manifest(),
            cache_mount_path="/devenv/sym",
            services=[self._service()],
        )
        assert 'if [ "$FAILED" -ne 0 ]; then' in sh
        assert "exit 1" in sh
        assert sh.rstrip().endswith("exit 0")

    def test_no_secret_value_leaks_into_readiness_probe(self) -> None:
        """T013 invariant: rendered verify.sh carries only names/paths, never a
        literal secret. The readiness probe is auth-free (pg_isready / redis PING),
        so no password is interpolated even when the service declares a
        password_env_var."""
        from coordinare_service_inference.schema import ServiceInit

        svc = self._service(
            init=ServiceInit(
                superuser="root", databases=["app_dev"], password_env_var="PGPASSWORD"
            )
        )
        sh = render_verify_sh(self._manifest(), cache_mount_path="/devenv/sym", services=[svc])
        # The env-var NAME may appear, but never a password flag carrying a value,
        # and never a process-substitution pwfile (the probe authenticates nothing).
        assert "pg_isready" in sh
        assert "--pwfile" not in sh
        assert "PGPASSWORD=" not in sh  # no value-binding assignment of the secret
        assert "-W" not in sh  # pg_isready never prompts/sends a password


class TestActivateRenderer:
    """render_activate_sh: coordinare owns activate.sh (like verify.sh), generating
    an auto-discovering, POSIX-safe activation from the manifest's runtime pins —
    so a forgetful agent can't fumble the activation paths (the .rbenv-vs-rbenv
    dot-prefix bug that broke every bootstrap)."""

    def _manifest(self) -> EnvManifest:
        return EnvManifest(
            symphony_name="sym",
            items=[
                ManifestItem(name="ruby", kind="runtime", version="3.4.2", source=".ruby-version"),
                ManifestItem(name="node", kind="runtime", version="18.12.1", source=".nvmrc"),
                ManifestItem(name="rails", kind="gem", source="Gemfile"),
                ManifestItem(name="chromium", kind="system", source="README.md"),
            ],
        )

    def test_sets_devenv_to_cache_mount_path(self) -> None:
        sh = render_activate_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert 'DEVENV="/devenv/sym"' in sh
        assert "export DEVENV" in sh

    def test_is_posix_safe_for_sourced_shells(self) -> None:
        """activate.sh is sourced into EVERY shell (bash AND dash) via BASH_ENV /
        profile.d, so it must never abort the calling shell."""
        sh = render_activate_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert "set -e" not in sh
        assert "set -u" not in sh
        # No bare `exit`/`return` that would kill a sourcing shell.
        for line in sh.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("exit ")
            assert not stripped.startswith("return ")
        # rbenv init emits shell-specific code and has deadlocked sourced dash
        # shells — discovery must be plain PATH prepends, not `eval "$(rbenv init)"`.
        assert "rbenv init" not in sh

    def test_ruby_discovery_covers_dot_and_nondot_rbenv(self) -> None:
        """The bug: agent built Ruby under .rbenv (dot) but pointed RBENV_ROOT at
        rbenv (no dot). Discovery must probe BOTH layouts (+ asdf) for the pinned
        version and guard on the real ruby binary."""
        sh = render_activate_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert "$DEVENV/.rbenv/versions/3.4.2" in sh
        assert "$DEVENV/rbenv/versions/3.4.2" in sh
        assert "$DEVENV/.asdf/installs/ruby/3.4.2" in sh
        assert '-x "$_r/bin/ruby"' in sh
        assert "RBENV_ROOT=" in sh

    def test_node_discovery_covers_nvm_and_tarball(self) -> None:
        sh = render_activate_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert "$DEVENV/.nvm/versions/node/v18.12.1/bin" in sh
        assert "$DEVENV/nvm/versions/node/v18.12.1/bin" in sh
        assert "node-v18.12.1-" in sh  # extracted-tarball glob fallback
        assert '-x "$_n/node"' in sh

    def test_non_exact_versions_skipped(self) -> None:
        """package.json engines are ranges (^20, >=18) with no deterministic
        install path — emitting `v^20` paths is useless. Skip non-exact pins."""
        manifest = EnvManifest(
            symphony_name="sym",
            items=[ManifestItem(name="node", kind="runtime", version="^20", source="package.json")],
        )
        sh = render_activate_sh(manifest, cache_mount_path="/devenv/sym")
        assert "^20" not in sh
        assert "node/v^20" not in sh

    def test_no_runtime_pins_still_valid_script(self) -> None:
        """A manifest with no pinned runtimes still yields a sourceable script
        (DEVENV + deb-bin glob), never a crash or empty file."""
        manifest = EnvManifest(
            symphony_name="sym",
            items=[ManifestItem(name="rails", kind="gem", source="Gemfile")],
        )
        sh = render_activate_sh(manifest, cache_mount_path="/devenv/sym")
        assert 'DEVENV="/devenv/sym"' in sh
        assert "/usr/bin" in sh  # extracted-deb best-effort prepend

    def test_extracted_deb_bins_best_effort(self) -> None:
        sh = render_activate_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert '"$DEVENV"/*/usr/bin' in sh

    def test_postgres_server_bin_dir_on_path(self) -> None:
        """103: Debian's postgresql-NN ships initdb/pg_ctl/postgres under
        /usr/lib/postgresql/<NN>/bin (NOT /usr/bin); activate.sh must surface that
        versioned server bin dir so services-start.sh + spec-101 resolve the daemon.
        Version-agnostic glob (the * matches the NN)."""
        sh = render_activate_sh(self._manifest(), cache_mount_path="/devenv/sym")
        assert '"$DEVENV"/*/usr/lib/postgresql/*/bin' in sh
        # no hard-pinned major version in the discovery glob
        import re
        assert not re.search(r"usr/lib/postgresql/\d", sh)


class TestServiceInstallDerivation:
    """091: a declared stateful service contributes a service-binary install item."""

    @staticmethod
    def _entry(**overrides):
        from coordinare_service_inference.schema import ServiceEntry, ServiceInit

        fields = dict(
            name="postgres",
            binary="postgres",
            version="16",
            data_dir="/tmp/pg-data",
            port=5432,
            why_needed="Primary application database",
            sources=["config/database.yml"],
            kind="postgres",
            init=ServiceInit(superuser="root", databases=["app_dev"]),
        )
        fields.update(overrides)
        return ServiceEntry(**fields)

    def test_postgres_derives_server_and_client_packages(self) -> None:
        from coordinare.services.env_manifest import derive_service_install_items

        items = derive_service_install_items([self._entry()])
        names = {i.name for i in items}
        # C-14/FR-006: the server package is fetched into the cache...
        assert "postgresql" in names
        # ...and C-11/T018: the client package providing pg_isready is included too.
        assert "postgresql-client" in names
        assert all(i.kind == "system" for i in items)
        assert all(i.source == "services.json:postgres" for i in items)

    def test_redis_derives_its_server_package(self) -> None:
        from coordinare_service_inference.schema import ServiceEntry

        from coordinare.services.env_manifest import derive_service_install_items

        redis = ServiceEntry(
            name="redis",
            binary="redis-server",
            version="7.2",
            data_dir="/tmp/redis-data",
            port=6379,
            why_needed="queue backend",
            sources=["Gemfile.lock"],
            kind="redis",
        )
        items = derive_service_install_items([redis])
        assert {i.name for i in items} == {"redis-server"}

    def test_generic_service_derives_no_install_item(self) -> None:
        # FR-013 / US2 acceptance #3: no stateful kind ⇒ behavior unchanged.
        from coordinare_service_inference.schema import ServiceEntry

        from coordinare.services.env_manifest import derive_service_install_items

        generic = ServiceEntry(
            name="widget",
            binary="widgetd",
            version="1.0",
            data_dir="/tmp/widget",
            port=9000,
            why_needed="bespoke daemon",
            sources=["docker-compose.yml"],
        )
        assert derive_service_install_items([generic]) == []
        assert derive_service_install_items([]) == []

    def test_external_service_derives_nothing(self) -> None:
        from coordinare_service_inference.schema import ServiceEntry

        from coordinare.services.env_manifest import derive_service_install_items

        external = ServiceEntry(
            name="snowflake",
            binary="(external)",
            version="cloud",
            data_dir="(external)",
            port=443,
            why_needed="warehouse",
            sources=["config/snowflake.yml"],
            external_required=True,
            required_env_vars=["SNOWFLAKE_ACCOUNT"],
        )
        assert derive_service_install_items([external]) == []

    def test_packages_deduped_across_services(self) -> None:
        from coordinare.services.env_manifest import derive_service_install_items

        items = derive_service_install_items([self._entry(), self._entry(name="pg2")])
        # Two postgres services name the same packages once each, not twice.
        assert len(items) == 2
        assert {i.name for i in items} == {"postgresql", "postgresql-client"}
