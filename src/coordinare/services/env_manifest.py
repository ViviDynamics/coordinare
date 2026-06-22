"""Derive an env-bootstrap dependency manifest from a symphony's project files (077).

Coordinare parses the symphony's structured pin/dependency files deterministically
(`.ruby-version`, `.tool-versions`, `.nvmrc`, `.python-version`, `package.json`,
`Gemfile`, `Gemfile.lock`) into an explicit :class:`EnvManifest`.  An optional
LLM pass over the README (see :mod:`coordinare.services.env_manifest_llm`) layers
in system packages described only in prose.

The manifest then drives BOTH the install (injected into the agent persona as an
authoritative checklist) AND the verification (coordinare renders ``verify.sh``
from it), so a spec-pinned tool version can never be silently missed.
"""

from __future__ import annotations

import json
import re
from typing import Any

from coordinare.models.env_manifest import EnvManifest, ManifestItem

# Structured project files coordinare fetches (in addition to the README) to
# derive pinned runtimes and declared dependencies.  Missing files are skipped.
STRUCTURED_SPEC_FILES: tuple[str, ...] = (
    ".ruby-version",
    ".tool-versions",
    ".nvmrc",
    ".python-version",
    "package.json",
    "Gemfile",
    "Gemfile.lock",
)

# asdf tool name -> canonical runtime name used in the manifest / verify checks.
_ASDF_RUNTIME_ALIASES = {"nodejs": "node", "node": "node", "ruby": "ruby", "python": "python"}


def _clean_version(raw: str) -> str:
    """Normalise a version string: strip whitespace and a leading 'v'."""
    return raw.strip().lstrip("vV").strip()


def _parse_ruby_version(content: str) -> list[ManifestItem]:
    v = _clean_version(content)
    if not v:
        return []
    return [ManifestItem(name="ruby", kind="runtime", version=v, source=".ruby-version")]


def _parse_python_version(content: str) -> list[ManifestItem]:
    v = _clean_version(content)
    if not v:
        return []
    return [ManifestItem(name="python", kind="runtime", version=v, source=".python-version")]


def _parse_nvmrc(content: str) -> list[ManifestItem]:
    v = _clean_version(content)
    if not v:
        return []
    return [ManifestItem(name="node", kind="runtime", version=v, source=".nvmrc")]


def _parse_tool_versions(content: str) -> list[ManifestItem]:
    """asdf .tool-versions: lines like 'ruby 3.4.2' / 'nodejs 20.11.0'."""
    items: list[ManifestItem] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        tool = parts[0].lower()
        version = _clean_version(parts[1])
        canonical = _ASDF_RUNTIME_ALIASES.get(tool)
        if canonical and version:
            items.append(
                ManifestItem(
                    name=canonical, kind="runtime", version=version, source=".tool-versions"
                )
            )
    return items


def _parse_package_json(content: str) -> list[ManifestItem]:
    """package.json `engines` → node/npm runtime pins."""
    items: list[ManifestItem] = []
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return items
    engines = data.get("engines") if isinstance(data, dict) else None
    if isinstance(engines, dict):
        for tool in ("node", "npm"):
            spec = engines.get(tool)
            if isinstance(spec, str) and spec.strip():
                items.append(
                    ManifestItem(
                        name=tool,
                        kind="runtime",
                        # engines specs are ranges (^20, >=18); keep raw, don't over-pin.
                        version=spec.strip(),
                        source="package.json",
                    )
                )
    return items


_GEMFILE_GEM_RE = re.compile(r"""^\s*gem\s+['"]([A-Za-z0-9_.\-]+)['"]""", re.MULTILINE)


def _parse_gemfile(content: str) -> list[ManifestItem]:
    """Top-level gem declarations → gem items (names; versions live in the lock)."""
    seen: set[str] = set()
    items: list[ManifestItem] = []
    for match in _GEMFILE_GEM_RE.finditer(content):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        items.append(ManifestItem(name=name, kind="gem", source="Gemfile"))
    return items


_BUNDLED_WITH_RE = re.compile(r"BUNDLED WITH\s*\n\s*([0-9][0-9.]*)", re.MULTILINE)


def _parse_gemfile_lock(content: str) -> list[ManifestItem]:
    """Gemfile.lock 'BUNDLED WITH' → the exact bundler version."""
    m = _BUNDLED_WITH_RE.search(content)
    if not m:
        return []
    return [
        ManifestItem(
            name="bundler", kind="gem", version=m.group(1).strip(), source="Gemfile.lock"
        )
    ]


# filename (basename) -> parser
_PARSERS = {
    ".ruby-version": _parse_ruby_version,
    ".python-version": _parse_python_version,
    ".nvmrc": _parse_nvmrc,
    ".tool-versions": _parse_tool_versions,
    "package.json": _parse_package_json,
    "Gemfile": _parse_gemfile,
    "Gemfile.lock": _parse_gemfile_lock,
}


# 091: a declared service `kind` → the system package(s) the coordinare-owned
# service recipe needs in the cache. The server package supplies the daemon +
# init/teardown tools (initdb/postgres/pg_ctl); the client package supplies the
# readiness probe (pg_isready) and the create-db tooling (psql/createdb) that
# services-start.sh and services-health.sh invoke. Only kinds coordinare knows how
# to host appear here; 'generic' services bring their own binary via the project
# spec files / start_args and derive nothing. (D3, C-14, FR-006.)
_SERVICE_KIND_PACKAGES: dict[str, tuple[str, ...]] = {
    "postgres": ("postgresql", "postgresql-client"),
    "redis": ("redis-server",),
}


def derive_service_install_items(services: list[Any]) -> list[ManifestItem]:
    """Turn declared stateful services into service-binary install items (091).

    Each :class:`~coordinare_service_inference.schema.ServiceEntry` with a coordinare-known
    ``kind`` (postgres, redis) contributes one or more ``system`` ManifestItems
    naming the deb package(s) the env-bootstrap must fetch into ``<cache>/debs/``
    — the SAME delivery the existing system-package path uses, so the base image
    gains nothing (FR-006, FR-007, SC-004). For ``kind == "postgres"`` the set
    includes the client package providing ``pg_isready`` (the readiness probe in
    services-health.sh) so that binary is guaranteed present, not assumed
    image-baked (C-11, C-14).

    ``services`` items are ServiceEntry models (duck-typed: ``external_required``,
    ``kind``, ``name``). External services host nothing in-container and derive
    nothing. Packages are de-duplicated across services, preferring the first
    service that names them.
    """
    items: list[ManifestItem] = []
    seen: set[str] = set()
    for svc in services:
        if getattr(svc, "external_required", False):
            continue
        kind = getattr(svc, "kind", "generic")
        packages = _SERVICE_KIND_PACKAGES.get(kind)
        if not packages:
            continue
        svc_name = getattr(svc, "name", kind)
        for pkg in packages:
            if pkg in seen:
                continue
            seen.add(pkg)
            items.append(
                ManifestItem(
                    name=pkg,
                    kind="system",
                    source=f"services.json:{svc_name}",
                    install_hint=(
                        f"service '{svc_name}' (kind={kind}); fetch as .deb into "
                        "<cache>/debs/ via the system-package path"
                    ),
                )
            )
    return items


def derive_manifest(
    symphony_name: str,
    file_contents: dict[str, str],
    *,
    spec_sha: str | None = None,
) -> EnvManifest:
    """Build a deterministic manifest from fetched project-file contents.

    ``file_contents`` maps relative path -> text.  Only files with a known parser
    contribute; unknown files (e.g. README.md) are ignored here and handled by
    the LLM pass.  Items are de-duplicated by (name, kind), preferring the entry
    that carries a version pin.
    """
    by_key: dict[tuple[str, str], ManifestItem] = {}
    for path, content in file_contents.items():
        if not content:
            continue
        basename = path.rsplit("/", 1)[-1]
        parser = _PARSERS.get(basename)
        if parser is None:
            continue
        for item in parser(content):
            key = (item.name, item.kind)
            existing = by_key.get(key)
            # Prefer a version-bearing item over a bare one.
            if existing is None or (existing.version is None and item.version is not None):
                by_key[key] = item
    return EnvManifest(
        symphony_name=symphony_name,
        items=list(by_key.values()),
        spec_sha=spec_sha,
    )


def _default_check(item: ManifestItem) -> str:
    """Render a shell check that prints OK/FAIL and sets FAILED=1 on failure."""
    name = item.name
    if item.kind == "runtime":
        if name == "ruby" and item.version:
            return (
                f'_got=$(ruby -e "print RUBY_VERSION" 2>/dev/null); '
                f'if [ "$_got" = "{item.version}" ]; then echo "OK: ruby {item.version}"; '
                f'else echo "FAIL: ruby version (want {item.version}, got ${{_got:-none}})"; FAILED=1; fi'
            )
        if name == "node":
            ver = item.version or ""
            if ver and re.fullmatch(r"[0-9][0-9.]*", ver):
                return (
                    f'_got=$(node -v 2>/dev/null | sed "s/^v//"); '
                    f'case "$_got" in {ver}*) echo "OK: node $_got";; '
                    f'*) echo "FAIL: node version (want {ver}, got ${{_got:-none}})"; FAILED=1;; esac'
                )
            return 'command -v node >/dev/null 2>&1 && echo "OK: node" || { echo "FAIL: node missing"; FAILED=1; }'
        if name == "python":
            return (
                'command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1'
                ' && echo "OK: python" || { echo "FAIL: python missing"; FAILED=1; }'
            )
        if name == "npm":
            return 'command -v npm >/dev/null 2>&1 && echo "OK: npm" || { echo "FAIL: npm missing"; FAILED=1; }'
        return f'command -v {name} >/dev/null 2>&1 && echo "OK: {name}" || {{ echo "FAIL: {name} missing"; FAILED=1; }}'
    if item.kind == "gem":
        if name == "bundler":
            return 'command -v bundle >/dev/null 2>&1 && echo "OK: bundler" || { echo "FAIL: bundler missing"; FAILED=1; }'
        return (
            f'( bundle show {name} >/dev/null 2>&1 || gem list -i {name} >/dev/null 2>&1 ) '
            f'&& echo "OK: gem {name}" || {{ echo "FAIL: gem {name} not installed"; FAILED=1; }}'
        )
    # system / node_pkg → SOFT check (warn, do NOT set FAILED). These are
    # consumer/test-stage services (chromium for system tests, psql for the DB):
    # the builder stages (assess/architect/implement/review/security) don't need
    # them, so a missing one must not hard-fail the whole env-cache and block the
    # entire pipeline. The qa stage handles missing browser/DB on its own.
    return (
        f'command -v {name} >/dev/null 2>&1 && echo "OK: {name}" '
        f'|| echo "WARN: {name} not on PATH (test/qa-only; non-blocking)"'
    )


_KIND_LABELS = {
    "runtime": "language runtime (install the EXACT version via a version manager / build tool, NOT apt)",
    "gem": "Ruby gem (must be installed and loadable)",
    "system": "system package / binary, best-effort (test/qa-only; not bootstrap-gating)",
    "node_pkg": "Node package (best-effort; not bootstrap-gating)",
}


def render_checklist(manifest: EnvManifest) -> str:
    """Render the manifest as an explicit install checklist for the agent persona."""
    if not manifest.items:
        return ""
    lines = [
        "AUTHORITATIVE DEPENDENCY CHECKLIST (derived by coordinare from the project files).",
        "Install EXACTLY these — each MUST be present for the bootstrap to pass:",
    ]
    order = {"runtime": 0, "gem": 1, "node_pkg": 2, "system": 3}
    for item in sorted(manifest.items, key=lambda i: order.get(i.kind, 9)):
        ver = f" =={item.version}" if item.version else ""
        label = _KIND_LABELS.get(item.kind, item.kind)
        lines.append(f"  - [{item.kind}] {item.name}{ver}  ({label}; from {item.source})")
    lines.append(
        "Install pinned language runtimes FIRST (gems/modules build against them). "
        "A coordinare-provided verify.sh checks every item above; make it pass."
    )
    return "\n".join(lines)


def _service_readiness_check(svc: Any) -> str | None:
    """093 / US2: render a LIVE readiness probe for a coordinare-managed service.

    Returns a shell line that hard-FAILs (sets FAILED=1) when the service is
    installed-but-not-running, or ``None`` for services coordinare does not manage
    (generic kinds, externally-required services) — those are not ours to start,
    so they get no FAIL-able readiness line.

    The probe is auth-free by construction: ``pg_isready`` and ``redis-cli PING``
    check liveness without authenticating, so no secret value is ever interpolated
    (only the schema-validated service name and integer port reach the script).
    """
    if getattr(svc, "external_required", False):
        return None
    name = svc.name
    port = svc.port
    if svc.kind == "postgres":
        return (
            f"if pg_isready -h 127.0.0.1 -p {port} >/dev/null 2>&1; then "
            f'echo "OK: service {name} accepting connections on {port}"; '
            f'else echo "FAIL: service {name} not accepting connections on {port} (pg_isready)"; '
            f"FAILED=1; fi"
        )
    if svc.kind == "redis":
        return (
            f'if [ "$(redis-cli -h 127.0.0.1 -p {port} PING 2>/dev/null)" = "PONG" ]; then '
            f'echo "OK: service {name} responding to PING on {port}"; '
            f'else echo "FAIL: service {name} not responding to PING on {port}"; '
            f"FAILED=1; fi"
        )
    # generic / unknown kind → coordinare doesn't manage its lifecycle; no probe.
    return None


def render_verify_sh(
    manifest: EnvManifest,
    *,
    cache_mount_path: str,
    services: list[Any] | None = None,
) -> str:
    """Render an authoritative verify.sh from the manifest.

    Sources activate.sh first (so the installed toolchain is on PATH), runs every
    item's check, and exits non-zero if any failed — the same contract the
    clean-context verifier (daemon._verify_env_cache_clean) already runs.

    ``services`` are the declared service descriptors (ServiceEntry). For each
    coordinare-managed one (postgres/redis), a LIVE readiness probe is appended that
    hard-FAILs when the service is installed but not running — so the dispatch gate
    withholds against a cache whose service isn't actually up yet.
    """
    lines = [
        "#!/usr/bin/env bash",
        "# AUTHORITATIVE verify.sh — generated by coordinare from the env manifest.",
        "# Do NOT edit by hand; the bootstrap agent must make this pass, not rewrite it.",
        "set +e",
        "FAILED=0",
        f'if [ -f "{cache_mount_path}/activate.sh" ]; then . "{cache_mount_path}/activate.sh"; fi',
        "",
    ]
    # Runtimes first (everything else depends on them), then gems, then system.
    order = {"runtime": 0, "gem": 1, "node_pkg": 2, "system": 3}
    has_rails = any(item.kind == "gem" and item.name == "rails" for item in manifest.items)
    for item in sorted(manifest.items, key=lambda i: order.get(i.kind, 9)):
        lines.append(item.check or _default_check(item))
    # If Rails is present, boot the app to catch native-extension failures (e.g. psych/libyaml)
    # that pass gem-presence checks but fail at require time.
    if has_rails:
        lines += [
            "",
            "# Rails boot smoke-test: catches native extension failures (e.g. psych without libyaml-dev)",
            'if [ -d "/repo" ]; then',
            "  BUNDLE_GEMFILE=/repo/Gemfile bundle exec ruby -e \"require 'rails'; require 'psych'\" "
            '2>/dev/null && echo "OK: rails/psych native extensions load" || '
            '{ echo "FAIL: rails/psych failed to load (native extension or config error)" >&2; FAILED=1; }',
            "fi",
        ]
    # Coordinare-managed services must be RUNNING, not merely installed: probe each
    # live (pg_isready / redis PING). A not-running managed service hard-fails so the
    # dispatch gate withholds against a half-built cache.
    service_checks = [
        line for svc in (services or []) if (line := _service_readiness_check(svc)) is not None
    ]
    if service_checks:
        lines.append("")
        lines.append("# Coordinare-managed service readiness (RUNNING + healthy, not just installed)")
        lines += service_checks
    lines += [
        "",
        'if [ "$FAILED" -ne 0 ]; then',
        '  echo "========= Bootstrap verification FAILED =========" >&2',
        "  exit 1",
        "fi",
        'echo "========= Bootstrap verification PASSED ========="',
        "exit 0",
        "",
    ]
    return "\n".join(lines)


# Only an EXACT, fully-resolved version (e.g. "3.4.2") yields a deterministic
# install path. Range specs from package.json `engines` ("^20", ">=18.0.0") do
# not, so activation discovery skips them.
_EXACT_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


def _ruby_activation_block(version: str) -> list[str]:
    """rbenv (.rbenv OR rbenv), asdf, or a source-compile prefix — probe each for
    the pinned ruby and put its bin (+ rbenv shims) on PATH."""
    return [
        f"# --- runtime: ruby {version} ---",
        f'for _r in "$DEVENV/.rbenv/versions/{version}" "$DEVENV/rbenv/versions/{version}" '
        f'"$DEVENV/.asdf/installs/ruby/{version}" "$DEVENV/ruby-{version}"; do',
        '  if [ -x "$_r/bin/ruby" ]; then',
        "    case \"$_r\" in",
        '      */.rbenv/*|*/rbenv/*) RBENV_ROOT="${_r%/versions/*}"; export RBENV_ROOT; '
        'export PATH="$_r/bin:$RBENV_ROOT/shims:$RBENV_ROOT/bin:$PATH";;',
        '      *) export PATH="$_r/bin:$PATH";;',
        "    esac",
        "    break",
        "  fi",
        "done",
    ]


def _node_activation_block(version: str) -> list[str]:
    """nvm (.nvm OR nvm), asdf, or an extracted node tarball — probe each for the
    pinned node and put its bin on PATH."""
    return [
        f"# --- runtime: node {version} ---",
        f'for _n in "$DEVENV/.nvm/versions/node/v{version}/bin" '
        f'"$DEVENV/nvm/versions/node/v{version}/bin" '
        f'"$DEVENV/.asdf/installs/nodejs/{version}/bin" "$DEVENV"/node-v{version}-*/bin; do',
        '  if [ -x "$_n/node" ]; then export PATH="$_n:$PATH"; break; fi',
        "done",
    ]


def _python_activation_block(version: str) -> list[str]:
    """pyenv (.pyenv OR pyenv) or asdf — probe each for the pinned python."""
    return [
        f"# --- runtime: python {version} ---",
        f'for _p in "$DEVENV/.pyenv/versions/{version}/bin" "$DEVENV/pyenv/versions/{version}/bin" '
        f'"$DEVENV/.asdf/installs/python/{version}/bin"; do',
        '  if [ -x "$_p/python" ] || [ -x "$_p/python3" ]; then export PATH="$_p:$PATH"; break; fi',
        "done",
    ]


_RUNTIME_ACTIVATION = {
    "ruby": _ruby_activation_block,
    "node": _node_activation_block,
    "python": _python_activation_block,
}


def render_activate_sh(manifest: EnvManifest, *, cache_mount_path: str) -> str:
    """Render an authoritative, auto-discovering activate.sh from the manifest.

    Coordinare owns activate.sh the same way it owns verify.sh: the bootstrap agent
    installs the pinned toolchain, but it no longer hand-writes the activation
    paths — a forgetful model writing those by hand fumbled the .rbenv-vs-rbenv
    (and .nvm-vs-nvm) dot-prefix every run, leaving a fully-built cache that
    verify.sh couldn't see. This script *discovers* the toolchain at source time
    (after install) wherever the agent placed it, so it is correct regardless.

    CRITICAL: activate.sh is sourced into EVERY shell (bash AND dash) via
    BASH_ENV / /etc/profile.d, so it MUST NOT ``exit``/``return`` non-zero or use
    ``set -e``/``set -u`` (that terminates the calling shell), and MUST stay POSIX
    (no ``eval "$(rbenv init)"`` — it emits shell-specific code and has deadlocked
    sourced dash shells). Discovery is plain, guarded PATH prepends only.
    """
    lines = [
        "# AUTHORITATIVE activate.sh — generated by coordinare from the env manifest.",
        "# Discovers the installed toolchain under the cache and puts it on PATH.",
        "# Do NOT edit by hand; the bootstrap agent installs the toolchain, coordinare",
        "# owns the activation. Sourced into EVERY shell (bash AND dash): must never",
        "# abort the caller (no errexit/nounset, no non-zero exit) — plain guarded",
        "# PATH prepends only.",
        f'DEVENV="{cache_mount_path}"',
        "export DEVENV",
        "",
    ]
    for item in manifest.runtime_pins():
        version = item.version or ""
        if not _EXACT_VERSION_RE.match(version):
            continue
        builder = _RUNTIME_ACTIVATION.get(item.name)
        if builder is None:
            continue
        lines += builder(version)
        lines.append("")
    # Extracted-deb binaries (e.g. a headless browser) — best-effort, test/qa-only
    # and non-blocking. SHARED LIBS are handled separately by the coordinare-owned
    # profile (LD_LIBRARY_PATH); this only surfaces extracted BINARIES on PATH.
    lines += [
        "# --- extracted-deb binaries (best-effort; test/qa-only, non-blocking) ---",
        'for _b in "$DEVENV"/*/usr/bin; do',
        '  [ -d "$_b" ] && export PATH="$_b:$PATH"',
        "done",
        "",
        # Coordinare-managed stateful service servers (spec-091/102): on Debian the
        # postgresql-NN server ships initdb/pg_ctl/postgres under
        # /usr/lib/postgresql/<NN>/bin, NOT /usr/bin — the glob above misses it, so
        # services-start.sh (which calls bare `initdb`/`postgres`) and spec-101's
        # readiness gate would not resolve them. Surface these versioned server bin
        # dirs too (version-agnostic via the * for <NN>).
        "# --- coordinare-managed service server binaries (e.g. postgresql-NN) ---",
        'for _s in "$DEVENV"/*/usr/lib/postgresql/*/bin; do',
        '  [ -d "$_s" ] && export PATH="$_s:$PATH"',
        "done",
        "",
    ]
    # Service-host aliasing (spec 105): coordinare hosts declared services on
    # 127.0.0.1 IN THIS container, but the app's test-env names them by their
    # docker-compose hostnames (e.g. POSTGRESQL_HOST=db, REDIS_HOST=redis, and
    # redis://redis:.../ embedded in a URL). There is no compose network here, so
    # those names don't resolve. Map each single-label *_HOST/*_HOSTNAME value to
    # loopback so the app connects — aliasing the NAME fixes standalone vars AND
    # URL-embedded uses of the same name. Reads the LIVE env at runtime (no test-env
    # value is baked into this script — secret-free); best-effort and non-fatal so a
    # non-writable hosts file never aborts the sourced shell.
    lines += [
        "# --- service-host aliasing (spec 105): declared service hostnames -> loopback ---",
        '_HOSTS="${COORDINARE_HOSTS_FILE:-/etc/hosts}"',
        # `while read` (not `for $(...)`) so a value is taken whole — never word-split
        # into bogus fragments if it ever contains whitespace.
        "env | sed -n 's/^[A-Za-z0-9_]*_HOSTNAME=//p; s/^[A-Za-z0-9_]*_HOST=//p' "
        "| while IFS= read -r _hv; do",
        # Single-label hostname only: skip empty / localhost / FQDN (dot) / IP or
        # host:port (colon), AND anything with a char outside a hostname label
        # (spaces, shell metachars, slashes) — defensive, never trust the value.
        '  case "$_hv" in',
        "    ''|localhost|*.*|*:*) continue ;;",
        '    *[!A-Za-z0-9_-]*) continue ;;',
        "  esac",
        # Idempotent: skip only when our exact alias line already exists (fixed-string,
        # whole-line — avoids `grep -w` matching `db` inside `postgres-db`).
        '  grep -qxF "127.0.0.1 $_hv" "$_HOSTS" 2>/dev/null && continue',
        '  echo "127.0.0.1 $_hv" >> "$_HOSTS" 2>/dev/null || true',  # best-effort, non-fatal
        "done",
        "unset _HOSTS",
        "",
    ]
    return "\n".join(lines)
