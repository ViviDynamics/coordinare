"""Build wiki inventory and detect repository layout (spec 171)."""
from __future__ import annotations

import re

from pathlib import Path
from typing import Any

from performer.workflows.documenter.markdown import parse_frontmatter, backticked_tokens, links
from performer.workflows.documenter.models import WikiPage, RepositoryLayout

__all__ = [
    "extract_citations",
    "wiki_links",
    "build_inventory",
    "repository_layout",
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


def path_like_tokens(text: str) -> set[str]:
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
        if t.rstrip("/") == "docs/wiki" or t.startswith("docs/wiki/"):
            continue  # a wiki page or the wiki itself is a link target, checked by links_resolve, never a citation
        if _RELATIVE_PATH.match(t) or _FILE_WITH_EXTENSION.match(t):
            out.add(t.rstrip("/"))
    return out


def extract_citations(text: str, tree: set[str]) -> list[str]:
    """Extract citations from markdown content.

    Citations are backticked tokens containing "/" or a file extension,
    or link targets that resolve to repository files outside the wiki.

    Args:
        text: The markdown content.
        tree: Set of repository paths.

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

    # Extract link targets that resolve to tree files outside wiki
    link_list = links(text)
    for link in link_list:
        target = link.target
        # Normalize the path
        target = target.removeprefix("./")
        if target.startswith("../"):
            # Resolve relative paths (basic implementation)
            pass
        # Check if it's in the tree and not a markdown file in wiki
        if target in tree and not target.endswith(".md"):
            if target not in citations:
                citations.append(target)

    return citations


def wiki_links(text: str, page_path: str) -> list[str]:
    """Extract wiki links that resolve to .md files in docs/wiki/.

    Args:
        text: The markdown content.
        page_path: Path to the current page.

    Returns:
        List of resolved wiki link targets as repo paths.
    """
    resolved = []
    link_list = links(text)
    page_dir = Path(page_path).parent

    for link in link_list:
        target = link.target
        # Skip external links and anchors
        if target.startswith(("http", "#")):
            continue
        if not target.endswith(".md"):
            continue

        # Resolve relative paths
        if target.startswith("/"):
            # Absolute from repo root
            resolved_path = target.lstrip("/")
        elif target.startswith("docs/wiki/"):
            # Repository-relative, the convention Google's docguide asks for (review finding: doubled path)
            resolved_path = target
        elif target.startswith("./"):
            # Relative to current dir
            resolved_path = str(page_dir / target[2:])
        elif target.startswith("../"):
            # Parent directory
            resolved_path = str((page_dir / target).resolve())
        else:
            # Relative to current dir
            resolved_path = str(page_dir / target)

        # Normalize path
        resolved_path = str(Path(resolved_path)).replace("\\", "/")
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


def build_inventory(workspace: Path, tree: set[str]) -> list[WikiPage]:
    """Build inventory of all pages in docs/wiki/.

    Args:
        workspace: The repository root.
        tree: Set of all repository paths.

    Returns:
        List of WikiPage objects for each page in docs/wiki/.
    """
    pages = []
    wiki_dir = workspace / "docs" / "wiki"

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
        citations = extract_citations(content, tree | path_like_tokens(content))
        page_links = wiki_links(content, path_str)

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


async def repository_layout(toolkit: Any, workspace: Path, tree: set[str]) -> RepositoryLayout:
    """What this repository is, as the model read it (#367).

    This used to probe for ``pyproject.toml``, ``package.json``, ``pytest.ini``
    and ``setup.cfg``, map the first hit to ``pytest`` or ``npm test``, and pick
    out source directories with an 18-entry file-extension table and a hardcoded
    list of directory names to skip. It therefore understood Python and Node
    repositories, and produced an empty picture for everything else -- silently,
    which is how a documenter came to write about projects it could not read.

    The model reads the layout and the root files and says what the project is.
    What stays mechanical here is arithmetic over paths the model named: how big
    a directory is and whether tests live in it. That is counting, not judgement.

    Raises:
        ProjectShapeUnknown: the model could not characterise the repository, so
            the card stops rather than documenting a blank picture.
    """
    from performer.workflows.project_shape import detect_shape

    shape = await detect_shape(toolkit, workspace, tree)

    by_dir: dict[str, list[str]] = {}
    for path_str in tree:
        if "/" not in path_str:
            continue
        by_dir.setdefault(path_str.split("/", 1)[0], []).append(path_str)

    repo_has_tests = any(p.split("/", 1)[0] in ("tests", "test") for p in tree if "/" in p)
    packages = []
    for top in shape.source_dirs:
        files = by_dir.get(top)
        if not files:
            continue  # the model named a directory the tree does not have
        size = 0
        for f in files:
            try:
                size += (workspace / f).stat().st_size
            except OSError:
                continue
        inner_tests = any(part in ("tests", "test") for f in files for part in Path(f).parts[1:-1])
        packages.append({"path": top, "size": size, "has_tests": repo_has_tests or inner_tests})
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
