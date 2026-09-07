"""Pinning tests for the spec-171 adversarial review findings (all confirmed by execution)."""
from __future__ import annotations

import pytest
from performer.workflows.documenter import markdown as md
from performer.workflows.documenter.gate import GateInput, gate_page
from performer.workflows.documenter.gather import gather_page
from performer.workflows.documenter.index import generate_readme, readme_shape_ok
from performer.workflows.documenter.inventory import extract_citations, wiki_links
from performer.workflows.documenter.models import (
    POINTER_MARKERS,
    PagePlan,
    RepositoryLayout,
    WikiPage,
)
from performer.workflows.documenter.plan import init_skeleton, is_doc_path, select_pages
from performer.workflows.documenter.pointers import render_pointer_section, replace_between_markers


def _page(path, kind, citations):
    return WikiPage(path=path, kind=kind, title=path.rsplit("/", 1)[-1], citations=citations, links=[], size=500)


# plan: traversal, kind default, README slot, duplicate modules, init ordering
def test_brief_locations_with_parent_segments_or_absolute_paths_are_refused():
    for bad in ("docs/../../etc/passwd", "/etc/passwd", "docs/wiki/../../x.md"):
        assert not is_doc_path(bad), bad
    plans, _deferred, refused = select_pages([{"location": "docs/../../etc/passwd", "topic": "t"}], [], [], 8)
    assert plans == [] and refused == ["docs/../../etc/passwd"]


def test_a_brief_entry_without_a_kind_is_a_reference_page():
    plans, _, _ = select_pages([{"location": "docs/wiki/p.md", "topic": "t"}], [], [], 8)
    assert plans[0].kind == "reference"


def test_the_readme_keeps_its_slot_when_the_cap_is_reached():
    briefs = [{"location": f"docs/wiki/p{i}.md", "topic": f"t{i}"} for i in range(9)]
    plans, deferred, _ = select_pages(briefs, [], [], 8)
    assert len(plans) == 8 and plans[-1].path == "docs/wiki/README.md" and plans[-1].source == "index"
    assert deferred == ["docs/wiki/p7.md", "docs/wiki/p8.md"]


def test_duplicate_brief_locations_merge_their_modules_and_say_texts():
    briefs = [{"location": "docs/wiki/p.md", "say": "a", "modules": ["src/a.py"]}, {"location": "docs/wiki/p.md", "say": "b", "modules": ["src/b.py", "src/a.py"]}]
    plans, _, _ = select_pages(briefs, [], [], 8)
    assert [p.path for p in plans] == ["docs/wiki/p.md", "docs/wiki/README.md"]
    assert plans[0].say == ["a", "b"] and plans[0].modules == ["src/a.py", "src/b.py"]


def test_init_skeleton_orders_packages_largest_first_regardless_of_input_order():
    layout = RepositoryLayout(project_name="x", packages=[{"path": "small", "size": 10, "has_tests": True}, {"path": "big", "size": 900, "has_tests": True}, {"path": "notests", "size": 5000, "has_tests": False}], has_ci=False, test_command_hint="")
    plans, deferred = init_skeleton(layout, [], 8)
    tail = [p.path for p in plans if p.source == "init" and p.path.endswith(("big.md", "small.md", "notests.md"))]
    assert tail == ["docs/wiki/big.md", "docs/wiki/small.md"] and deferred == []


# index: Optional once, duplicate sections fail
def test_the_readme_lists_the_optional_section_once_and_rejects_duplicates():
    readme = generate_readme("demo", "A demo.", [_page("docs/wiki/a.md", None, [])])
    assert readme.count("## Optional") == 1 and readme.count("(a.md)") == 1
    assert readme_shape_ok(readme, [_page("docs/wiki/a.md", None, [])]) == []
    twice = readme.replace("## Reference\n- Nothing here yet.\n", "## Reference\n- Nothing here yet.\n\n## Reference\n- Nothing here yet.\n")
    assert any("Duplicate H2 section: Reference" in f for f in readme_shape_ok(twice, [_page("docs/wiki/a.md", None, [])]))


# pointers: a dangling marker never yields two pairs
def test_a_dangling_start_marker_is_removed_before_the_section_is_appended():
    section = render_pointer_section("docs/wiki/README.md", [("Architecture", "docs/wiki/architecture.md")])
    text = f"# Agents\n\n{POINTER_MARKERS[0]}\nold\n"
    out = replace_between_markers(text, section)
    assert out.count(POINTER_MARKERS[0]) == 1 and out.count(POINTER_MARKERS[1]) == 1 and out.startswith("# Agents\n")


# markdown: fenced blocks hide backticks and links
def test_fenced_blocks_hide_backticked_paths_and_links():
    text = "Real `src/app.py`.\n\n```python\nconfig = {'path': `src/fake.py`}\nsee [x](docs/wiki/fake.md)\n```\n\n~~~\n`src/other.py`\n~~~\n[Real](docs/wiki/real.md)\n"
    assert md.backticked_tokens(text) == ["src/app.py"]
    assert [link.target for link in md.links(text)] == ["docs/wiki/real.md"]


# inventory: repository-relative wiki links, directory boundary for citations
def test_repository_relative_wiki_links_are_not_doubled_and_prefixes_need_a_boundary():
    assert wiki_links("[A](docs/wiki/architecture.md) [B](setup.md)", "docs/wiki/guide.md") == ["docs/wiki/architecture.md", "docs/wiki/setup.md"]
    tree = {"src/payments.py", "src/payroll.py", "src/pay/x.py"}
    assert extract_citations("`src/pay` and `src/payments.py`", tree) == ["src/pay", "src/payments.py"], "src/pay is a real directory here"
    assert extract_citations("`src/pa`", tree) == [], "a partial name is not a directory citation"


# gate: decision pages live under docs/wiki/decisions/
def test_a_decision_page_outside_the_decisions_directory_is_dropped():
    body = "---\nkind: decision\n---\n# Use sqlite\n\n## Context\n" + "We store data in `src/db.py`. " * 8 + "\n\n## Decision\n" + "One sqlite file per deployment. " * 6 + "\n\n## Consequences\n" + "Costs: one writer at a time; no replication. " * 4 + "\n\n## Status\naccepted\n"
    tree = {"src/db.py"}
    good = gate_page(GateInput(plan=PagePlan(path="docs/wiki/decisions/sqlite.md", kind="decision", source="brief", justification="j", exists=False), action="write", content=body, reason="", tree=tree, pages_after_run=set()))
    bad = gate_page(GateInput(plan=PagePlan(path="docs/wiki/sqlite.md", kind="decision", source="brief", justification="j", exists=False), action="write", content=body, reason="", tree=tree, pages_after_run=set()))
    assert not good.dropped and bad.dropped and "decision_outside_decisions_dir" in bad.contract_failures


# gather: paths are shell-quoted and the evidence header names the whole path
@pytest.mark.asyncio
async def test_gather_quotes_paths_with_spaces(tmp_path):
    seen = []

    async def runner(cmd, cwd, timeout_s):
        seen.append(cmd)
        return 0, "content"

    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.toolkit import Toolkit

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=None, command_runner=runner, call_limit=5)
    plan = PagePlan(path="docs/wiki/my api.md", kind="reference", source="brief", justification="j", exists=True, modules=["src/my module.py"])
    _evidence, _current, text = await gather_page(tk, tmp_path, plan, max_commands=6, max_output_chars=4000)
    assert seen[0] == "git show HEAD:'docs/wiki/my api.md'" and seen[1] == "sed -n '1,200p' 'src/my module.py'"
    assert "### src/my module.py" in text



# --- second live init round --------------------------------------------------------

def test_wiki_self_references_are_links_not_citations():
    """Live init round: pages citing `docs/wiki` or `docs/wiki/architecture.md` were dropped as missing citations."""
    from performer.workflows.documenter.inventory import path_like_tokens

    assert path_like_tokens("See `docs/wiki` and `docs/wiki/architecture.md` and `src/app.py`.") == {"src/app.py"}


def test_first_paragraph_feeds_the_index_entry():
    from performer.workflows.documenter.inventory import first_paragraph

    text = "---\nkind: explanation\n---\n# Architecture\n\n> not this\n\nThe service is a small Flask app in `src/app.py`.\nIt stores data in `src/db.py`.\n\n## What it is\nmore\n"
    assert first_paragraph(text) == "The service is a small Flask app in `src/app.py`. It stores data in `src/db.py`."
    readme = generate_readme("demo", "d", [WikiPage(path="docs/wiki/architecture.md", kind="explanation", title="Architecture", citations=[], links=[], size=1, summary="The service is a small Flask app.")])
    assert "- [Architecture](architecture.md): The service is a small Flask app." in readme


def test_init_pages_gather_evidence_from_their_own_files():
    """Live init round: package pages had no evidence and the model refused to write them."""
    from performer.workflows.documenter import _init_modules
    from performer.workflows.documenter.models import PagePlan, RepositoryLayout

    tree = {"pyproject.toml", "pkg2/__init__.py", "pkg2/core.py", "pkg2/tests/test_x.py", "tests/test_app.py", "src/app.py", ".github/workflows/ci.yml"}
    layout = RepositoryLayout(project_name="demo", packages=[{"path": "src", "size": 100, "has_tests": True}, {"path": "pkg2", "size": 50, "has_tests": True}], has_ci=True, test_command_hint="pytest")
    mk = lambda path: PagePlan(path=path, kind="reference", source="init", justification="j", exists=False)  # noqa: E731
    assert _init_modules(mk("docs/wiki/pkg2.md"), tree, layout) == ["pkg2/__init__.py", "pkg2/core.py"], "the package's own files, tests excluded"
    assert _init_modules(mk("docs/wiki/setup.md"), tree, layout) == ["pyproject.toml", ".github/workflows/ci.yml"]
    assert "tests/test_app.py" in _init_modules(mk("docs/wiki/testing.md"), tree, layout)
    arch = _init_modules(mk("docs/wiki/architecture.md"), tree, layout)
    assert arch[0] == "pyproject.toml" and "src/app.py" in arch and "pkg2/__init__.py" in arch
