"""Build wiki inventory and detect repository layout (spec 171).

415: the inventory, the link resolution and the layout reading are all
docs-root aware -- the documenter serves the documentation system the
repository actually has, not only ``docs/wiki``.
"""
from __future__ import annotations

import re

from pathlib import Path
from typing import Any, Iterable

from performer.workflows.documenter.docsroot import normalize_repo_path
from performer.workflows.documenter.markdown import parse_frontmatter, backticked_tokens, links
from performer.workflows.documenter.models import WikiPage, RepositoryLayout

_MIN_SPLIT_FILES = 2  # a nested source dir needs this many files to split into its own package (415)

__all__ = [
    "extract_citations",
    "wiki_links",
    "build_inventory",
    "repository_layout",
    "layout_from_shape",
    "has_tests_in",
]




_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:(?:/[A-Za-z0-9_.-]+)+/?|/)$")  # a/b, a/b/, or a bare directory a/
#: A bare file name counts as a path only with a known source or config extension.
#: The live init round read `0.1.0` (a version) and `pkg5.x` (an attribute) as files.
_KNOWN_EXTENSIONS = (
    "py", "pyi", "js", "jsx", "ts", "tsx", "go", "rs", "rb", "java", "kt", "swift", "c", "cc", "cpp", "h", "hpp",
    "cs", "php", "scala", "ex", "exs", "sh", "bash", "zsh", "sql", "md", "rst", "txt", "toml", "yaml", "yml",
    "json", "cfg", "ini", "conf", "env", "lock", "xml", "html", "css", "scss", "csv", "proto", "gradle", "mk",
)
_FILE_WITH_EXTENSION = re.compile(r"^[A-Za-z_-][A-Za-z0-9_.-]*\.(?:" + "|".join(_KNOWN_EXTENSIONS) + r")$")


def path_like_tokens(text: str, docs_root: str = "docs/wiki") -> set[str]:
    """Backticked tokens that look like repository paths, whether or not they exist.

    A repository path is relative (``src/app.py``, ``tests/``) or a bare file
    name with an extension (``pyproject.toml``). A URL route (``/charge``), a
    decorator (``@app.route``), a URL, a shell fragment or a symbol is not a
    path: the live round showed such tokens counted as missing citations.

    The inventory keeps them so a page whose cited files were deleted still
    shows what it cited (that is how a retirement is justified); the gate
    decides which of them exist.
    """
    out: set[str] = set()
    for token in backticked_tokens(text):
        t = token.strip()
        if not t or " " in t or t.startswith(("http", "-", "$", "#", "@", "/", "./", "../")):
            continue
        if "(" in t or ")" in t or "=" in t or ":" in t:
            continue
        if t.rstrip("/") == docs_root or t.startswith(docs_root + "/"):
            continue  # a docs page or the docs root itself is a link target, checked by links_resolve, never a citation
        if _RELATIVE_PATH.match(t) or _FILE_WITH_EXTENSION.match(t):
            out.add(t.rstrip("/"))
    return out


def extract_citations(text: str, tree: set[str], docs_root: str = "docs/wiki", page_path: str | None = None) -> list[str]:
    """Extract citations from markdown content.

    Citations are backticked tokens containing "/" or a file extension,
    or link targets that resolve to repository files outside the wiki.

    Args:
        text: The markdown content.
        tree: Set of repository paths.
        docs_root: The repository's documentation root (link targets there are
            page references, not file citations).
        page_path: Path of the page the text belongs to; when given, relative
            link targets are resolved against its directory as repository
            paths rather than as the machine's absolute paths.

    Returns:
        Sorted list of unique citations that exist in tree.
    """
    citations = []

    # Extract backticked tokens
    tokens = backticked_tokens(text)
    for token in tokens:
        # Include if it contains "/" or a file extension, and is in the tree
        if ("/" in token or "." in token) and token in tree:
            if token not in citations:
                citations.append(token)
        # Check directory prefixes
        elif "/" in token:
            # Check if it's a directory prefix
            prefix = token.rstrip("/") + "/"
            for path in tree:
                if path.startswith(prefix):
                    if token not in citations:
                        citations.append(token)
                    break

    # Extract link targets that resolve to tree files outside the docs root
    link_list = links(text)
    for link in link_list:
        target = link.target
        if page_path:
            # Resolve ../ and ./ against the page's directory, lexically: this
            # is a repository path, not a filesystem path (415).
            target = normalize_repo_path(str(Path(page_path).parent), target)
        else:
            target = target.removeprefix("./")
        # Check if it's in the tree and not a documentation page
        if target in tree and not target.endswith(".md") and not target.startswith(docs_root + "/"):
            if target not in citations:
                citations.append(target)

    return citations


def wiki_links(text: str, page_path: str, docs_root: str = "docs/wiki") -> list[str]:
    """Extract wiki links resolved to repository paths.

    ``../`` and ``./`` targets are resolved against the page's directory
    lexically -- no ``Path.resolve()``, which bound links to the machine's
    absolute paths (415). Targets under the docs root or repo-relative targets
    pass through unchanged.

    Args:
        text: The markdown content.
        page_path: Path to the current page.
        docs_root: The repository's documentation root.

    Returns:
        List of resolved wiki link targets as repo paths.
    """
    resolved = []
    link_list = links(text)
    page_dir = str(Path(page_path).parent) if "/" in page_path else "."

    for link in link_list:
        target = link.target
        # Skip external links and anchors
        if target.startswith(("http", "#")):
            continue
        if not target.endswith(".md"):
            continue

        if target.startswith("/"):
            # Absolute from repo root
            resolved_path = target.lstrip("/")
        elif target.startswith(docs_root + "/"):
            # Repository-relative, the convention Google's docguide asks for (review finding: doubled path)
            resolved_path = target
        else:
            # Relative to the page's directory, with . and .. resolved lexically
            resolved_path = normalize_repo_path(page_dir, target)

        if resolved_path not in resolved:
            resolved.append(resolved_path)

    return resolved


def first_paragraph(text: str) -> str:
    """The first prose paragraph after the H1, one line, for the index entry."""
    _fm, body = parse_frontmatter(text)
    lines = body.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].startswith("# "):
        i += 1
    i += 1
    para: list[str] = []
    for line in lines[i:]:
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith(("#", ">", "```", "~~~", "|", "- ", "* ", "---")):
            if para:
                break
            continue
        para.append(stripped)
    return " ".join(" ".join(para).split())[:120]


def build_inventory(workspace: Path, tree: set[str], docs_root: str = "docs/wiki") -> list[WikiPage]:
    """Build inventory of all pages under the repository's docs root.

    Args:
        workspace: The repository root.
        tree: Set of all repository paths.
        docs_root: The documentation root to enumerate (415: any docs system,
            not only ``docs/wiki``).

    Returns:
        List of WikiPage objects for each page under docs_root.
    """
    pages = []
    wiki_dir = workspace / docs_root

    if not wiki_dir.exists():
        return pages

    for md_file in sorted(wiki_dir.rglob("*.md")):
        relative_path = md_file.relative_to(workspace)
        path_str = str(relative_path).replace("\\", "/")

        content = md_file.read_text(encoding="utf-8")

        # Parse frontmatter for kind
        from performer.workflows.documenter.markdown import parse_frontmatter, headings

        meta, body = parse_frontmatter(content)
        kind = meta.get("kind")

        # Extract title (first H1 or file stem)
        h_list = headings(content)
        title = next((h.text for h in h_list if h.level == 1), md_file.stem)

        # Extract citations and links
        citations = extract_citations(content, tree | path_like_tokens(content, docs_root), docs_root, path_str)
        page_links = wiki_links(content, path_str, docs_root)

        # File size
        size = len(content)

        pages.append(
            WikiPage(
                path=path_str,
                kind=kind,
                title=title,
                citations=citations,
                links=page_links,
                size=size,
                summary=first_paragraph(content),
            ),
        )

    return pages


_TEST_DIR_SEGMENTS = ("tests", "test", "spec", "specs", "__tests__", "testing")


def has_tests_in(paths: Iterable[str]) -> bool:
    """Do these paths include tests, whatever convention names them (415)?

    Recognises test directories at any depth (``tests/``, ``test/``,
    ``spec/``, ``__tests__/``, ``testing/``) and the filename conventions of
    the common runners: ``test_*.py`` (pytest), ``*_test.go`` (go test),
    ``*_test.rb`` (minitest), ``*.spec.*``/``*.test.*`` (jest/vitest).
    """
    for p in paths:
        parts = Path(p).parts
        if any(seg in _TEST_DIR_SEGMENTS for seg in parts[:-1]):
            return True
        name = parts[-1]
        if name.startswith("test_") or name.endswith(("_test.go", "_test.rb", "_test.py", "_test.ts", "_test.js")):
            return True
        if ".spec." in name or ".test." in name:
            return True
    return False


def _package(named: str, files: list[str], workspace: Path) -> dict[str, Any]:
    size = 0
    for f in files:
        try:
            size += (workspace / f).stat().st_size
        except OSError:
            continue
    return {"path": named, "size": size, "has_tests": has_tests_in(files)}


def _split_packages(named: str, tree: set[str], workspace: Path) -> list[dict[str, Any]]:
    """One model-named source dir as one or more packages, never a flattened blob.

    A monorepo whose source dirs are nested (``apps/web`` and ``apps/api``
    under a model-named ``apps``) becomes one package per nested directory
    that holds real content, not a single undifferentiated ``apps`` (415).
    """
    prefix = named.rstrip("/") + "/"
    files = sorted(p for p in tree if p.startswith(prefix))
    if not files:
        return []  # the model named a directory the tree does not have
    subdirs: dict[str, list[str]] = {}
    for p in files:
        rel = p[len(prefix):]
        if "/" in rel:
            subdirs.setdefault(rel.split("/", 1)[0], []).append(p)
    standalone = {d: fs for d, fs in subdirs.items() if len(fs) >= _MIN_SPLIT_FILES and len(subdirs) > 1}
    if not standalone:
        return [_package(named, files, workspace)]
    out: list[dict[str, Any]] = []
    direct = [p for p in files if p[len(prefix):].split("/", 1)[0] not in standalone]
    if len(direct) >= _MIN_SPLIT_FILES:
        out.append(_package(named, direct, workspace))
    for d, fs in sorted(standalone.items()):
        out.append(_package(f"{named}/{d}", fs, workspace))
    return out


def layout_from_shape(shape: Any, workspace: Path, tree: set[str]) -> RepositoryLayout:
    """The mechanical half of the layout reading: arithmetic over the paths the model named.

    What stays mechanical here is counting: how big a directory is and whether
    tests live in it. The judgement about what counts as source is the model's.
    """
    packages: list[dict[str, Any]] = []
    for named in shape.source_dirs:
        packages.extend(_split_packages(named, tree, workspace))
    packages.sort(key=lambda p: p["size"], reverse=True)

    # GitHub's workflow path, which is knowledge of the platform coordinare runs
    # on rather than of the project's stack.
    has_ci = any(p.startswith(".github/workflows/") for p in tree)

    return RepositoryLayout(
        project_name=shape.project_name or workspace.name,
        packages=packages,
        has_ci=has_ci,
        test_command_hint=shape.test_command,
    )


async def repository_layout(toolkit: Any, workspace: Path, tree: set[str]) -> RepositoryLayout:
    """What this repository is, as the model read it (#367).

    This used to probe for ``pyproject.toml``, ``package.json``, ``pytest.ini``
    and ``setup.cfg``, map the first hit to ``pytest`` or ``npm test``, and pick
    out source directories with an 18-entry file-extension table and a hardcoded
    list of directory names to skip. It therefore understood Python and Node
    repositories, and produced an empty picture for everything else -- silently,
    which is how a documenter came to write about projects it could not read.

    The model reads the layout and the root files and says what the project is.

    Raises:
        ProjectShapeUnknown: the model could not characterise the repository, so
            the card stops rather than documenting a blank picture.
    """
    from performer.workflows.project_shape import detect_shape

    shape = await detect_shape(toolkit, workspace, tree)
    return layout_from_shape(shape, workspace, tree)
