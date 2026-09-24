"""415: docs-root, decisions-dir, link-normalisation and needs-documentation policy.

Everything here is a pure function of its arguments so the workflow, the eval
harness and the unit tests can share one vocabulary for "where do this
repository's living docs live".  Resolution order for the docs root:

1. symphony configuration (`workflow_env["DOCS_ROOT"]`) -- the operator's word;
2. an existing docs system in the tree -- the documenter updates it in place
   and never forks a parallel tree beside it;
3. ``docs/wiki`` -- the coordinare-created default, used by `init`.
"""
from __future__ import annotations

DOCS_ROOT_DEFAULT = "docs/wiki"

_SYSTEM_ORDER: tuple[tuple[str, str], ...] = (
    ("mkdocs.yml", "docs"),
    ("mkdocs.yaml", "docs"),
)


_GENERIC_EXCLUDED = ("__pycache__", ".git", "node_modules", ".venv")
_GENERIC_EXCLUDED_PREFIXES = (".", "src/", "lib/", "apps/", "packages/", "tests/")
_GENERIC_EXCLUDED_TOPS = ("src", "lib", "apps", "packages", "tests")
_MIN_GENERIC_PAGES = 2
_CONF_FILES = ("conf.py", "conf.py.txt")


def _sane(path: str) -> bool:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    return bool(parts) and ".." not in parts and not path.startswith(("/", "~"))


def _override_docs_root(override: str | None) -> str | None:
    return override.rstrip("/") if override and _sane(override) else None


def _wiki_root(paths: set[str], dirs: set[str]) -> str | None:
    return "docs/wiki" if "docs/wiki" in dirs else None


def _mkdocs_root(paths: set[str], dirs: set[str]) -> str | None:
    for marker, root in _SYSTEM_ORDER:
        if marker in paths and root in dirs:
            return root
    return None


def _sphinx_root(paths: set[str], dirs: set[str]) -> str | None:
    for conf in _CONF_FILES:
        for d in dirs:
            if d.endswith("/source") and f"{d}/{conf}" in paths:
                return d
    return None


def _docusaurus_root(paths: set[str], dirs: set[str]) -> str | None:
    for d in sorted(dirs, key=len, reverse=True):
        if d.endswith(_GENERIC_EXCLUDED):
            continue
        if any(f"{d}/docusaurus.config.{ext}" in paths for ext in ("js", "ts")) and f"{d}/docs" in dirs:
            return f"{d}/docs"
    return None


def _plain_docs_root(paths: set[str], dirs: set[str]) -> str | None:
    for candidate in ("docs", "doc", "handbook"):
        if candidate in dirs and any(p.startswith(f"{candidate}/") and p.endswith(".md") for p in paths):
            return candidate
    return None


def _generic_root(paths: set[str], dirs: set[str]) -> str | None:
    for d in sorted(dirs, key=len, reverse=True):
        if d.startswith(_GENERIC_EXCLUDED_PREFIXES) or d in _GENERIC_EXCLUDED_TOPS or d.endswith(("__pycache__", "node_modules", ".venv")):
            continue
        direct_pages = [p for p in paths if p.startswith(f"{d}/") and p.endswith(".md") and p.count("/") == d.count("/") + 1]
        if len(direct_pages) >= _MIN_GENERIC_PAGES:
            return d
    return None


_DETECTORS = (_mkdocs_root, _sphinx_root, _docusaurus_root, _wiki_root, _plain_docs_root, _generic_root)


def resolve_docs_root(tree: frozenset[str] | set[str] | None, override: str | None = None) -> str:
    """Return the docs root for a repository, as a repo-relative directory path.

    ``override`` (symphony config) wins when it names a sane repo-relative
    path.  Otherwise detect the existing docs system from the tree; when none
    is found, return the coordinare default ``docs/wiki``.
    """
    overridden = _override_docs_root(override)
    if overridden:
        return overridden
    if not tree:
        return DOCS_ROOT_DEFAULT
    paths = set(tree)
    dirs = {p.rsplit("/", 1)[0] for p in paths if "/" in p}
    for detector in _DETECTORS:
        found = detector(paths, dirs)
        if found:
            return found
    return DOCS_ROOT_DEFAULT


_DECISIONS_SUFFIXES = ("adr", "decisions", "architecture-decisions", "rfcs")


def resolve_decisions_dir(tree: frozenset[str] | set[str] | None, docs_root: str) -> str:
    """Return the directory decision pages live in.

    An existing ADR-style directory in the tree wins (``docs/adr``, ``adr``,
    ``doc/decisions``, ...); otherwise decisions default to a ``decisions/``
    directory under the docs root.
    """
    default = f"{docs_root}/decisions"
    if not tree:
        return default
    dirs = {p.rsplit("/", 1)[0] for p in tree if "/" in p}
    for d in sorted(dirs, key=lambda x: (x.count("/"), len(x))):
        parts = d.lower().split("/")
        if parts and parts[-1] in _DECISIONS_SUFFIXES and any(p.startswith(f"{d}/") for p in tree):
            return d
    return default


def normalize_repo_path(page_dir: str, target: str) -> str:
    """Resolve ``target`` (possibly ``../``-relative) against ``page_dir`` lexically.

    Never touches the filesystem: ``Path.resolve()`` would bind links to the
    machine's absolute paths.  The result is a repo-relative path with no
    ``.`` or ``..`` segments, or ``""`` when ``target`` escapes the repository
    root (a leading ``..`` with nothing left to pop) -- callers treat that as
    an unresolvable link rather than silently clamping it to the root.
    """
    base = [p for p in page_dir.split("/") if p and p != "."]
    target = target.split("#", 1)[0].split("?", 1)[0]
    parts = base + [p for p in target.split("/") if p and p != "."]
    out: list[str] = []
    for part in parts:
        if part == "..":
            if out:
                out.pop()
            else:
                return ""
            continue
        out.append(part)
    return "/".join(out)


def _is_dependency_path(path: str) -> bool:
    p = path.lower()
    if p.startswith((".github/", ".gitlab/", ".circleci/", ".buildkite/")):
        return True
    name = p.rsplit("/", 1)[-1]
    if p.endswith((".lock",)) or name in ("poetry.lock", "pipfile.lock", "package-lock.json", "yarn.lock", "bun.lock", "composer.lock", "gemfile.lock", "cargo.lock", "go.sum"):
        return True
    return name in ("pyproject.toml", "package.json", "cargo.toml", "gemfile", "requirements.txt", "requirements-dev.txt", "pipfile", "go.mod", "go.work", "paket.lock", "dependencies.gradle", "build.gradle", "pom.xml", "composer.json", "bunfig.toml", "deno.json")


_NEW_FILE_MARKER = "--- /dev/null"


def has_new_files(diff_text: str) -> bool:
    """True when the diff adds at least one file (``--- /dev/null`` section)."""
    return _NEW_FILE_MARKER in diff_text


def needs_documentation(
    changed_paths: list[str],
    diff_text: str,
    brief_docs: list[dict[str, object]] | None,
    refactor_min_files: int = 10,
) -> bool:
    """Decide whether a change needs documentation at all.

    - Anything named in the brief's docs list is documented (the operator asked).
    - Dependency/lockfile/CI-only changes churn nothing.
    - A refactor-shaped change (many modified files, none new) gets no pages by
      default -- there is no new surface to document.
    """
    if brief_docs:
        return True
    if changed_paths and all(_is_dependency_path(p) for p in changed_paths):
        return False
    if len(changed_paths) >= refactor_min_files and not has_new_files(diff_text):
        return False
    return True
