"""Documenter gate (spec 171 FR-007, FR-008, FR-016): pure rules per page.

Citations are re-extracted from the written content and checked against the
tree; wiki links must resolve after the run; the page contract encodes the
documentation model (one kind, required headings, one H1, clean headings,
fenced languages, informative links, size bounds, no changelog lines).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from performer.workflows.documenter import markdown as md
from performer.workflows.documenter.inventory import extract_citations, path_like_tokens, wiki_links
from performer.workflows.documenter.models import (
    BAD_LINK_TEXT,
    KINDS,
    PAGE_MAX_CHARS,
    PAGE_MIN_CHARS,
    REQUIRED_HEADINGS,
    PagePlan,
    PageResult,
    WikiPage,
)

__all__ = [
    "citations_exist", "links_resolve", "is_changelog_heading", "contract_failures", "accept_retire", "gate_page",
]

#: A heading is a changelog heading when it names an issue-ish reference next
#: to the words that name the work ("Card #123 changes"), or when it names a
#: changelog. A bare ``#``-number ("Port #8080") is not a changelog heading:
#: the live round dropped a legitimate section to that false positive (415).
_ISSUE_REF = re.compile(r"\b(?:card|issue|pr|pull request|ticket|task)\s*#\d+", re.IGNORECASE)


def citations_exist(citations: Iterable[str], tree: set[str]) -> list[str]:
    """The citations that name nothing in the tree (file or directory prefix).

    A bare file name (``package.json``) counts as existing when any path in
    the tree ends with it -- the file named in a page can live in a package
    directory the citation does not prefix (415).
    """
    missing = []
    for c in citations:
        c = c.rstrip("/")
        if c in tree or any(t.startswith(c + "/") for t in tree):
            continue
        if "/" not in c and any(t == c or t.endswith("/" + c) or f"/{c}/" in f"/{t}/" for t in tree):
            continue
        missing.append(c)
    return missing


def links_resolve(links: Iterable[str], pages_after_run: set[str]) -> list[str]:
    """The wiki links that name no page existing after the run."""
    return [link for link in links if link not in pages_after_run]


def is_changelog_heading(text: str) -> bool:
    lowered = text.lower()
    return bool(_ISSUE_REF.search(text)) or "changelog" in lowered or "change log" in lowered


def contract_failures(kind: str | None, content: str, *, is_readme: bool = False) -> list[str]:
    """Every contract check the content fails, by name (FR-008)."""
    failures: list[str] = []
    fm, body = md.parse_frontmatter(content)
    headings = md.headings(content)
    h1 = [h for h in headings if h.level == 1]
    if len(h1) != 1:
        failures.append(f"h1_count={len(h1)}")
    if not is_readme:
        if kind not in KINDS:
            failures.append("kind_missing")
        elif str(fm.get("kind", "")).strip() != kind:
            failures.append("frontmatter_kind_mismatch")
        h2 = [h.text.strip() for h in headings if h.level == 2]
        for required in REQUIRED_HEADINGS.get(kind or "", ()):
            if required not in h2:
                failures.append(f"missing_heading:{required}")
        if kind == "reference" and not _has_cited_entries(body):
            failures.append("reference_without_cited_entries")
    texts = [h.text.strip().lower() for h in headings]
    if len(texts) != len(set(texts)):
        failures.append("duplicate_heading")
    if any(h.has_link for h in headings):
        failures.append("link_in_heading")
    if any(h.has_code for h in headings):
        failures.append("code_in_heading")
    if any(is_changelog_heading(h.text) for h in headings):
        failures.append("changelog_heading")
    if any(not f.language for f in md.fenced_blocks(content)):
        failures.append("fence_without_language")
    if any(link.text.strip().lower() in BAD_LINK_TEXT for link in md.links(content)):
        failures.append("bad_link_text")
    size = md.body_chars(content)
    if not is_readme and size < PAGE_MIN_CHARS:
        failures.append(f"too_short={size}")
    if size > PAGE_MAX_CHARS:
        failures.append(f"too_long={size}")
    return failures


def _has_cited_entries(body: str) -> bool:
    entries = [line for line in body.splitlines() if line.lstrip().startswith(("- ", "* ", "|")) and "`" in line]
    return len(entries) >= 1


def accept_retire(plan: PagePlan, page: WikiPage | None, tree: set[str]) -> bool:
    """FR-006: retire only an inventory page none of whose citations exist any more."""
    if plan.source != "inventory" or page is None:
        return False
    return len(page.citations) > 0 and len(citations_exist(page.citations, tree)) == len(page.citations)


@dataclass
class GateInput:
    plan: PagePlan
    action: str
    content: str
    reason: str
    tree: set[str]
    pages_after_run: set[str]
    inventory_page: WikiPage | None = None
    #: Where decision records live in this repository (415: the repository's
    #: ADR directory when it has one, not a fixed path).
    decisions_dir: str = "docs/wiki/decisions"
    docs_root: str = "docs/wiki"
    extra: dict = field(default_factory=dict)


def gate_page(g: GateInput) -> PageResult:
    """Apply the action rules, citations, links and the contract to one page."""
    if g.action == "unchanged":
        # A page that does not exist cannot be "already right": the model owes a write (live round).
        missing_page = not g.plan.exists and g.inventory_page is None
        return PageResult(path=g.plan.path, kind=g.plan.kind, action="unchanged", reason=g.reason[:300], dropped=missing_page,
                          drop_reason="unchanged_for_missing_page" if missing_page else None)
    if g.action == "retire":
        ok = accept_retire(g.plan, g.inventory_page, g.tree)
        return PageResult(path=g.plan.path, kind=g.plan.kind, action="retire", reason=g.reason[:300], dropped=not ok,
                          drop_reason=None if ok else "retire_not_justified")
    citations = extract_citations(g.content, g.tree | path_like_tokens(g.content, g.docs_root), g.docs_root, g.plan.path)
    missing = citations_exist(citations, g.tree)
    # A link may also point at an existing Markdown file outside the docs root
    # (root README, agent instruction files, package-level notes). Those exist
    # in the tree and are not going away, so they resolve; a retired wiki page
    # is inside the root and stays rejected because it never joins
    # pages_after_run (415 review).
    existing_md_elsewhere = {p for p in g.tree if p.endswith(".md") and not p.startswith(g.docs_root.rstrip("/") + "/")}
    links_missing = links_resolve(wiki_links(g.content, g.plan.path, g.docs_root), g.pages_after_run | existing_md_elsewhere)
    failures = contract_failures(g.plan.kind, g.content)
    if g.plan.kind == "decision" and not g.plan.path.startswith(g.decisions_dir.rstrip("/") + "/"):
        failures.append("decision_outside_decisions_dir")  # FR-010
    dropped = bool(missing or links_missing or failures)
    reason = None
    if dropped:
        parts = []
        if missing:
            parts.append("missing citations: " + ", ".join(missing[:5]))
        if links_missing:
            parts.append("unresolved links: " + ", ".join(links_missing[:5]))
        if failures:
            parts.append("contract: " + ", ".join(failures[:6]))
        reason = "; ".join(parts)[:500]
    return PageResult(
        path=g.plan.path, kind=g.plan.kind, action="write", reason=g.reason[:300], citations_checked=len(citations),
        citations_missing=missing, links_missing=links_missing, contract_failures=failures, size=md.body_chars(g.content),
        dropped=dropped, drop_reason=reason,
    )

