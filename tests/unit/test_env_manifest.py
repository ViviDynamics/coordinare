"""Unit tests for coordinare-derived env manifest (077)."""

from __future__ import annotations

from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.services.env_manifest import (
    STRUCTURED_SPEC_FILES,
    derive_manifest,
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
