"""Plan building for documenter workflow (spec 171)."""
from __future__ import annotations

from typing import Any

from performer.workflows.documenter.docsroot import DOCS_ROOT_DEFAULT, needs_documentation
from performer.workflows.documenter.models import DOC_PATH_PREFIXES, DOC_ROOT_FILES, KINDS, POINTER_FILES, PagePlan, WikiPage, RepositoryLayout

__all__ = [
    "is_doc_path",
    "select_pages",
    "init_skeleton",
    "build_plan",
]


def is_doc_path(path: str) -> bool:
    """Check if a path is a documentation path (FR-004).

    A path is a doc path if it:
    - Starts with a DOC_PATH_PREFIXES entry, or
    - Has a basename starting with a DOC_ROOT_FILES entry, or
    - Is a POINTER_FILES entry.

    Args:
        path: The path to check.

    Returns:
        True if the path is a documentation path.
    """
    # A path with a parent segment or an absolute path is never a documentation path
    # (review finding: docs/../../etc/passwd passed the prefix check).
    if not path or path.startswith("/") or ".." in path.split("/") or "\\" in path:
        return False
    # Check for pointer files
    if path in POINTER_FILES:
        return True

    # Check for doc path prefixes
    for prefix in DOC_PATH_PREFIXES:
        if path.startswith(prefix):
            return True

    # Check for doc root file basenames
    basename = path.split("/")[-1]
    for root_file in DOC_ROOT_FILES:
        if basename.startswith(root_file):
            return True

    return False


def _plannable_brief_location(location: str, docs_root: str) -> bool:
    """A brief location is plannable only when it is path-safe and under the
    repository's resolved docs root (415 review): anything else would recreate
    a parallel documentation tree or escape it."""
    if not location or location.startswith("/") or ".." in location.split("/") or "\\" in location:
        return False
    return location.startswith(f"{docs_root.rstrip('/')}/")


def _collect_brief_entries(
    brief_docs: list[dict[str, Any]],
    inventory: list[WikiPage],
    refused: list[str],
    docs_root: str = DOCS_ROOT_DEFAULT,
) -> dict[str, dict[str, Any]]:
    """Group brief entries by location, merging say texts and modules."""
    brief_by_location = {}
    for entry in brief_docs:
        location = entry.get("location", "")
        if not location:
            continue

        if not _plannable_brief_location(location, docs_root):
            if location not in refused:
                refused.append(location)
            continue

        # Collect entry info
        if location not in brief_by_location:
            brief_by_location[location] = {
                "kind": entry.get("kind") or "reference",  # a brief entry without a kind is a reference page
                "topic": entry.get("topic", "Named in brief"),
                "say": [],
                "modules": [],
            }
        # Collect modules from every entry naming this location (review finding: only the first survived)
        for module in entry.get("modules", []) or []:
            if module not in brief_by_location[location]["modules"]:
                brief_by_location[location]["modules"].append(module)

        # Collect say texts
        say = entry.get("say", [])
        if isinstance(say, str):
            say = [say]
        elif not say:
            say = []
        brief_by_location[location]["say"].extend(say)
    return brief_by_location


def _select_inventory_pages(
    inventory: list[WikiPage],
    changed_files: list[str],
    seen_paths: set[str],
) -> list[PagePlan]:
    """Inventory pages whose citations match changed files."""
    plans = []
    for page in inventory:
        if page.path in seen_paths:
            continue

        # Check if any citation intersects with changed files
        match = False
        for citation in page.citations:
            for changed in changed_files:
                # Citation matches if:
                # - changed file equals citation exactly
                # - changed file is under the citation directory (citation ends with /)
                # - citation is a prefix of the changed file (for partial path matches)
                if changed == citation:
                    match = True
                    break
                # Handle directory citations (e.g., "src/payments/")
                if citation.endswith("/") and changed.startswith(citation):
                    match = True
                    break
                # Handle directory citations without trailing slash
                if not citation.endswith("/") and changed.startswith(citation + "/"):
                    match = True
                    break
                # 415 review: a slashless citation names a file by basename, so
                # match it against any changed path's last segment (a monorepo's
                # "package.json" cites "apps/web/package.json")
                if "/" not in citation and changed.rsplit("/", 1)[-1] == citation:
                    match = True
                    break
            if match:
                break

        if match:
            seen_paths.add(page.path)
            # 415: a page the documenter did not write has no coordinare
            # frontmatter (or an unknown kind) -- it is still maintained, as a
            # reference page, rather than dropped for failing a vocabulary it
            # never joined.
            kind = page.kind if page.kind in KINDS else "reference"
            plans.append(
                PagePlan(
                    path=page.path,
                    kind=kind,
                    source="inventory",
                    justification=f"Cites changed file(s): {', '.join(page.citations[:2])}",
                    exists=True,
                    say=[],
                    modules=[],
                ),
            )
    return plans


def select_pages(
    brief_docs: list[dict[str, Any]],
    changed_files: list[str],
    inventory: list[WikiPage],
    cap: int,
    docs_root: str = DOCS_ROOT_DEFAULT,
) -> tuple[list[PagePlan], list[str], list[str]]:
    """Select pages for update mode (FR-003).

    Process:
    1. Brief entries (location only, dedup), doc paths only
    2. Inventory pages whose citations hit changed files
    3. README always when any page selected
    4. Slice to cap, overflow as deferred
    5. Non-doc-paths as refused

    Args:
        brief_docs: Brief docs entries with 'location' and optionally 'kind' and 'say'.
        changed_files: List of changed file paths.
        inventory: Inventory pages.
        cap: Maximum pages in the plan.
        docs_root: The repository's documentation root (415).

    Returns:
        (plan, deferred paths, refused paths)
    """
    plans = []
    deferred = []
    refused = []
    seen_paths = set()

    # Step 1: Process brief entries, collecting all say texts for duplicate locations
    brief_by_location = _collect_brief_entries(brief_docs, inventory, refused, docs_root)

    # Create plan entries from collected brief data
    for location, data in brief_by_location.items():
        seen_paths.add(location)
        plans.append(
            PagePlan(
                path=location,
                kind=data["kind"],
                source="brief",
                justification=data["topic"],
                exists=any(page.path == location for page in inventory),
                say=data["say"],
                modules=data["modules"],
            ),
        )

    # Step 2: Add inventory pages whose citations match changed files
    plans.extend(_select_inventory_pages(inventory, changed_files, seen_paths))

    # Step 3: Always append README when any page selected
    readme_path = f"{docs_root}/README.md"  # 415: the index lives under the repository's docs root
    if not plans:
        return plans, deferred, refused
    # The README always rides along when any page is selected, so it keeps a
    # reserved slot: the other pages are capped at cap - 1 (review finding: the
    # README fell off the plan when the cap was reached).
    others = [pl for pl in plans if pl.path != readme_path]
    if len(others) > cap - 1:
        deferred = [pl.path for pl in others[cap - 1:]]
        others = others[: cap - 1]
    plans = others + [PagePlan(path=readme_path, kind=None, source="index", justification="Index refresh", exists=True, say=[], modules=[])]
    return plans, deferred, refused


def init_skeleton(
    layout: RepositoryLayout,
    inventory: list[WikiPage],
    cap: int,
    docs_root: str = DOCS_ROOT_DEFAULT,
) -> tuple[list[PagePlan], list[str]]:
    """Build skeleton for init mode (FR-003).

    Pages in order:
    1. README (index)
    2. <docs_root>/architecture.md (explanation)
    3. <docs_root>/setup.md (how-to)
    4. <docs_root>/testing.md (how-to)
    5. <docs_root>/<package>.md (reference) for each package with tests, largest first

    Existing inventory pages keep their kind. Capped at cap.

    Args:
        layout: Repository layout.
        inventory: Existing pages (to preserve kinds).
        cap: Maximum pages.
        docs_root: The documentation root the skeleton is built under (415).

    Returns:
        (plan, deferred paths)
    """
    plans = []
    seen_paths = set()
    deferred = []

    # Build a map of existing pages by path
    existing_map = {p.path: p for p in inventory}

    # 1. README
    readme_path = f"{docs_root}/README.md"
    existing = existing_map.get(readme_path)
    plans.append(
        PagePlan(
            path=readme_path,
            kind=None,
            source="index",
            justification="Project index",
            exists=existing is not None,
        ),
    )
    seen_paths.add(readme_path)

    # 2. Architecture
    arch_path = f"{docs_root}/architecture.md"
    existing = existing_map.get(arch_path)
    plans.append(
        PagePlan(
            path=arch_path,
            kind=existing.kind if existing else "explanation",
            source="init",
            justification="Architecture and design",
            exists=existing is not None,
        ),
    )
    seen_paths.add(arch_path)

    # 3. Setup
    setup_path = f"{docs_root}/setup.md"
    existing = existing_map.get(setup_path)
    plans.append(
        PagePlan(
            path=setup_path,
            kind=existing.kind if existing else "how-to",
            source="init",
            justification="Developer setup",
            exists=existing is not None,
        ),
    )
    seen_paths.add(setup_path)

    # 4. Testing
    test_path = f"{docs_root}/testing.md"
    existing = existing_map.get(test_path)
    plans.append(
        PagePlan(
            path=test_path,
            kind=existing.kind if existing else "how-to",
            source="init",
            justification="How to test",
            exists=existing is not None,
        ),
    )
    seen_paths.add(test_path)

    # 5. Package pages (by size, largest first)
    packages_with_tests = sorted((p for p in layout.packages if p.get("has_tests")), key=lambda p: -int(p.get("size") or 0))  # largest first
    for pkg in packages_with_tests:
        if len(plans) >= cap:
            deferred.append(pkg["path"])
            continue

        pkg_page = f"{docs_root}/{pkg['path'].split('/')[-1]}.md"
        existing = existing_map.get(pkg_page)
        plans.append(
            PagePlan(
                path=pkg_page,
                kind=existing.kind if existing else "reference",
                source="init",
                justification=f"Package: {pkg['path']}",
                exists=existing is not None,
            ),
        )
        seen_paths.add(pkg_page)

    # Slice to cap
    if len(plans) > cap:
        deferred.extend([p.path for p in plans[cap:]])
        plans = plans[:cap]

    return plans, deferred


def build_plan(
    mode: str,
    brief_docs: list[dict[str, Any]],
    changed_files: list[str],
    inventory: list[WikiPage],
    layout: RepositoryLayout | None,
    cap: int,
    docs_root: str = DOCS_ROOT_DEFAULT,
    diff_text: str = "",
) -> tuple[list[PagePlan], list[str], list[str]]:
    """Build the documentation plan based on mode.

    Args:
        mode: "update" or "init".
        brief_docs: Brief documentation entries.
        changed_files: Changed files in the commit.
        inventory: Current wiki inventory.
        layout: Repository layout (for init mode).
        cap: Page cap.
        docs_root: The repository's documentation root (415).
        diff_text: The raw unified diff, for new-file detection in the
            needs-documentation judgement (415).

    Returns:
        (plan, deferred, refused)
    """
    if mode == "update":
        # 415: selection first asks whether this change needs documentation at
        # all. A dependency bump or a refactor of thirty modified files churns
        # nothing; anything named in the brief is documented regardless.
        if not needs_documentation(changed_files, diff_text, brief_docs):
            return [], [], []
        return select_pages(brief_docs, changed_files, inventory, cap, docs_root)
    if mode == "init":
        plans, deferred = init_skeleton(layout or RepositoryLayout(project_name="", packages=[], has_ci=False), inventory, cap, docs_root)
        return plans, deferred, []
    return [], [], []
