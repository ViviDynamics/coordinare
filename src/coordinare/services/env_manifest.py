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


def render_verify_sh(manifest: EnvManifest, *, cache_mount_path: str) -> str:
    """Render an authoritative verify.sh from the manifest.

    Sources activate.sh first (so the installed toolchain is on PATH), runs every
    item's check, and exits non-zero if any failed — the same contract the
    clean-context verifier (daemon._verify_env_cache_clean) already runs.
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
    for item in sorted(manifest.items, key=lambda i: order.get(i.kind, 9)):
        lines.append(item.check or _default_check(item))
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
