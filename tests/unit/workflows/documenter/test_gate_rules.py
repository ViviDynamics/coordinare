"""Spec 171 FR-007, FR-008, FR-016: every gate rule is a pure function with its own
test. Each test names the mutation that breaks it; the mutation table in the PR
records each one applied in the real tree."""
from __future__ import annotations

from performer.workflows.documenter.gate import (
    GateInput,
    accept_retire,
    citations_exist,
    contract_failures,
    gate_page,
    is_changelog_heading,
    links_resolve,
)
from performer.workflows.documenter.models import PagePlan, WikiPage

TREE = {"src/app.py", "src/db.py", "src/payments.py", "tests/test_app.py", "pyproject.toml", "docs/wiki/README.md", "docs/wiki/architecture.md"}
FILLER = "The code in `src/app.py` handles requests and `src/db.py` stores data. " * 6


def _page(kind: str, body: str, title: str = "Title") -> str:
    return f"---\nkind: {kind}\n---\n# {title}\n\n{body}"


def _explanation(**over) -> str:
    body = over.get("body") or f"## What it is\n{FILLER}\n\n## How it fits\n{FILLER}\n\n## Why it is this way\n{FILLER}\n\n## Where to change it\nStart from `src/app.py`.\n"
    return _page("explanation", body)


# citations_exist: mutation = always return []
def test_citations_must_name_a_file_or_a_directory_prefix_in_the_tree():
    assert citations_exist(["src/app.py", "src/", "tests"], TREE) == []
    assert citations_exist(["src/payments/ledger.py", "lib/"], TREE) == ["src/payments/ledger.py", "lib"]


# links_resolve: mutation = check the tree instead of the pages after the run
def test_links_must_resolve_to_pages_that_exist_after_the_run():
    after = {"docs/wiki/README.md", "docs/wiki/payments.md"}
    assert links_resolve(["docs/wiki/payments.md"], after) == []
    assert links_resolve(["docs/wiki/architecture.md"], after) == ["docs/wiki/architecture.md"], "a page retired this run is not a target even though it is in the tree"


# is_changelog_heading: mutation = only the word, not the issue reference
def test_changelog_headings_are_issue_references_or_the_word():
    assert is_changelog_heading("Card #123 changes")
    assert is_changelog_heading("Changelog")
    assert is_changelog_heading("Change log for June")
    assert not is_changelog_heading("Where to change it")
    assert not is_changelog_heading("C# bindings")


# contract_failures: one mutation per check named in the failure string
def test_the_contract_names_every_failed_check():
    assert contract_failures("explanation", _explanation()) == []
    assert "h1_count=2" in contract_failures("explanation", _explanation() + "\n# Second title\n")
    assert "kind_missing" in contract_failures(None, _explanation())
    assert "frontmatter_kind_mismatch" in contract_failures("how-to", _explanation())
    assert "missing_heading:Verify" in contract_failures("how-to", _page("how-to", f"## Goal\n{FILLER}\n\n## Prerequisites\n`pyproject.toml`\n\n## Steps\n1. Run\n\n```bash\npytest\n```\n"))
    assert "duplicate_heading" in contract_failures("explanation", _explanation() + "\n## What it is\nagain\n")
    assert "link_in_heading" in contract_failures("explanation", _explanation() + "\n## See [this](x.md)\n")
    assert "code_in_heading" in contract_failures("explanation", _explanation() + "\n## The `main` loop\n")
    assert "changelog_heading" in contract_failures("explanation", _explanation() + "\n## Card #7 changes\n")
    assert "fence_without_language" in contract_failures("explanation", _explanation() + "\n```\nraw\n```\n")
    assert "bad_link_text" in contract_failures("explanation", _explanation() + "\nSee [here](architecture.md).\n")
    assert any(f.startswith("too_short=") for f in contract_failures("explanation", _page("explanation", "## What it is\nx\n\n## How it fits\nx\n\n## Why it is this way\nx\n\n## Where to change it\nx\n")))
    assert any(f.startswith("too_long=") for f in contract_failures("explanation", _explanation(body=f"## What it is\n{'x' * 12100}\n\n## How it fits\nx\n\n## Why it is this way\nx\n\n## Where to change it\nx\n")))
    assert "reference_without_cited_entries" in contract_failures("reference", _page("reference", FILLER + "\n\nNo table here.\n"))
    assert contract_failures("reference", _page("reference", f"| Name | What |\n| --- | --- |\n| `src/app.py` | the app |\n\n{FILLER}")) == []
    assert "missing_heading:Consequences" in contract_failures("decision", _page("decision", f"## Context\n{FILLER}\n\n## Decision\n{FILLER}\n\n## Status\naccepted\n"))


# accept_retire: mutation = allow a brief-sourced retire; ignore the citations
def test_retire_only_for_an_inventory_page_whose_citations_are_all_gone():
    plan_inv = PagePlan(path="docs/wiki/legacy.md", kind="reference", source="inventory", justification="cites src/legacy.py", exists=True)
    gone = WikiPage(path="docs/wiki/legacy.md", kind="reference", title="Legacy", citations=["src/legacy.py"], links=[], size=100)
    alive = WikiPage(path="docs/wiki/legacy.md", kind="reference", title="Legacy", citations=["src/legacy.py", "src/app.py"], links=[], size=100)
    assert accept_retire(plan_inv, gone, TREE)
    assert not accept_retire(plan_inv, alive, TREE), "one live citation keeps the page"
    assert not accept_retire(plan_inv.model_copy(update={"source": "brief"}), gone, TREE), "a brief-sourced page is never retired"
    assert not accept_retire(plan_inv, None, TREE)
    assert not accept_retire(plan_inv, gone.model_copy(update={"citations": []}), TREE), "a page citing nothing has no evidence of being dead"


# gate_page: mutation = skip the citation check; skip the contract
def test_gate_page_combines_the_rules_and_names_the_drop_reason():
    plan = PagePlan(path="docs/wiki/architecture.md", kind="explanation", source="inventory", justification="cites src/app.py", exists=True)
    after = {"docs/wiki/README.md", "docs/wiki/architecture.md"}
    ok = gate_page(GateInput(plan=plan, action="write", content=_explanation(), reason="", tree=TREE, pages_after_run=after))
    assert not ok.dropped and ok.citations_checked >= 2 and ok.action == "write"
    bad = gate_page(GateInput(plan=plan, action="write", content=_explanation().replace("`src/app.py`", "`src/nope.py`"), reason="", tree=TREE, pages_after_run=after))
    assert bad.dropped and "src/nope.py" in bad.citations_missing and "missing citations" in bad.drop_reason
    linky = gate_page(GateInput(plan=plan, action="write", content=_explanation() + "\nSee [Payments](payments.md).\n", reason="", tree=TREE, pages_after_run=after))
    assert linky.dropped and linky.links_missing == ["docs/wiki/payments.md"]
    unchanged = gate_page(GateInput(plan=plan, action="unchanged", content="", reason="still right", tree=TREE, pages_after_run=after))
    assert not unchanged.dropped and unchanged.action == "unchanged"


# path_like_tokens: mutation = accept a leading slash (routes) or an @ (decorators)
def test_only_repository_paths_count_as_citations():
    """Live round: `/charge`, `@app.route` and `charge()` were counted as missing citations."""
    from performer.workflows.documenter.inventory import path_like_tokens

    text = "Routes: `/charge` via `@app.route`; call `charge(conn, 5)`; see `src/payments.py`, `tests/`, `pyproject.toml`, `https://x.y/z`, `KEY=1`, `a:b`."
    assert path_like_tokens(text) == {"src/payments.py", "tests", "pyproject.toml"}


# gate_page unchanged: mutation = accept unchanged for a page that does not exist
def test_unchanged_is_a_drop_when_the_page_does_not_exist_yet():
    """Live round: the model answered unchanged for a brief page that had never been written."""
    new_page = PagePlan(path="docs/wiki/payments.md", kind="reference", source="brief", justification="brief", exists=False)
    r = gate_page(GateInput(plan=new_page, action="unchanged", content="", reason="looks fine", tree=TREE, pages_after_run=set()))
    assert r.dropped and r.drop_reason == "unchanged_for_missing_page"
    existing = PagePlan(path="docs/wiki/architecture.md", kind="explanation", source="inventory", justification="cites", exists=True)
    page = WikiPage(path="docs/wiki/architecture.md", kind="explanation", title="Architecture", citations=["src/app.py"], links=[], size=900)
    ok = gate_page(GateInput(plan=existing, action="unchanged", content="", reason="still right", tree=TREE, pages_after_run=set(), inventory_page=page))
    assert not ok.dropped
