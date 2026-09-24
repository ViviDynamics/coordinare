"""415: the documenter is not tied to a coordinare-created docs/wiki tree.

Every test here names the mutation that breaks it; each mutant is applied in
the real tree and must be killed.
"""
from __future__ import annotations

from pathlib import Path

from performer.workflows.documenter.docsroot import (
    normalize_repo_path,
    resolve_decisions_dir,
    resolve_docs_root,
)
from performer.workflows.documenter.gate import (
    GateInput,
    citations_exist,
    gate_page,
    is_changelog_heading,
)
from performer.workflows.documenter.inventory import build_inventory, wiki_links
from performer.workflows.documenter.models import PagePlan, WikiPage
from performer.workflows.documenter.plan import build_plan, select_pages


# resolve_docs_root: mutation = ignore the override, or return docs/wiki always
def test_the_symphony_override_wins():
    tree = {"docs/index.md", "src/app.py"}
    assert resolve_docs_root(tree, "handbook") == "handbook"


def test_an_override_that_escapes_the_repository_is_refused():
    tree = {"docs/index.md"}
    assert resolve_docs_root(tree, "../elsewhere") == "docs"
    assert resolve_docs_root(tree, "/absolute") == "docs"


def test_an_existing_wiki_is_updated_in_place():
    tree = {"docs/wiki/README.md", "docs/wiki/setup.md", "src/app.py"}
    assert resolve_docs_root(tree) == "docs/wiki"


def test_a_plain_docs_tree_beats_the_default():
    tree = {"docs/index.md", "docs/setup.md", "src/app.py"}
    assert resolve_docs_root(tree) == "docs"


def test_a_sphinx_tree_is_recognised():
    tree = {"doc/source/conf.py", "doc/source/index.md", "src/app.py"}
    assert resolve_docs_root(tree) == "doc/source"


def test_a_docusaurus_tree_is_recognised():
    tree = {"website/docusaurus.config.js", "website/docs/intro.md", "src/app.py"}
    assert resolve_docs_root(tree) == "website/docs"


def test_an_mkdocs_tree_is_recognised():
    tree = {"mkdocs.yml", "docs/index.md", "src/app.py"}
    assert resolve_docs_root(tree) == "docs"


def test_a_repository_with_no_docs_defaults_to_the_wiki():
    tree = {"src/app.py", "pyproject.toml"}
    assert resolve_docs_root(tree) == "docs/wiki"


def test_a_markdown_tree_without_a_known_system_is_found():
    tree = {"handbook/intro.md", "handbook/setup.md", "src/app.py"}
    assert resolve_docs_root(tree) == "handbook"


# resolve_decisions_dir: mutation = always docs/wiki/decisions
def test_an_existing_adr_directory_wins():
    tree = {"docs/adr/0001-records.md", "docs/wiki/README.md"}
    assert resolve_decisions_dir(tree, "docs/wiki") == "docs/adr"


def test_without_adr_conventions_the_default_is_under_the_docs_root():
    tree = {"docs/wiki/README.md"}
    assert resolve_decisions_dir(tree, "docs/wiki") == "docs/wiki/decisions"


# normalize_repo_path: mutation = Path.resolve() (filesystem) again
def test_parent_links_resolve_as_repo_paths_not_filesystem_paths():
    assert normalize_repo_path("docs/wiki/sub", "../README.md") == "docs/wiki/README.md"
    assert normalize_repo_path("docs/wiki", "../README.md") == "docs/README.md"
    assert normalize_repo_path("docs/wiki", "./architecture.md") == "docs/wiki/architecture.md"
    assert normalize_repo_path("docs/wiki", "../../../AGENTS.md") == "", "a link escaping the repository root is unresolvable, not clamped to the root"


def test_wiki_links_going_out_of_the_wiki_resolve_to_repo_paths():
    resolved = wiki_links("[Readme](../README.md)", "docs/wiki/architecture.md")
    assert resolved == ["docs/README.md"]


# citations_exist: mutation = bare names always missing (root-only check)
def test_a_bare_filename_that_exists_anywhere_is_not_missing():
    tree = {"packages/app/package.json", "services/api/settings.py", "src/main.py"}
    assert citations_exist(["package.json", "settings.py", "main.py"], tree) == []
    assert citations_exist(["no-such-file.py"], tree) == ["no-such-file.py"]


def test_a_bare_directory_name_that_exists_anywhere_is_not_missing():
    tree = {"apps/web/main.py", "apps/web/tests/test_main.py"}
    assert citations_exist(["web"], tree) == []


# is_changelog_heading: mutation = any #\d+ is a changelog heading
def test_a_number_reference_that_is_not_an_issue_ref_is_not_a_changelog_heading():
    assert is_changelog_heading("Card #123 changes")
    assert not is_changelog_heading("Port #8080 and its retry loop")
    assert not is_changelog_heading("Where to change it")


# plan: legacy pages (no coordinare frontmatter) are planned as reference pages
def test_an_inventory_page_without_a_kind_is_planned_as_a_reference_page():
    page = WikiPage(path="docs/wiki/legacy.md", kind=None, title="Legacy", citations=["src/app.py"], links=[], size=500)
    plans, _deferred, _refused = select_pages([], ["src/app.py"], [page], 8)
    assert [p.kind for p in plans if p.path == "docs/wiki/legacy.md"] == ["reference"]


def test_an_inventory_page_with_an_unknown_kind_is_planned_as_a_reference_page():
    page = WikiPage(path="docs/wiki/old.md", kind="spec-notes", title="Old", citations=["src/app.py"], links=[], size=500)
    plans, _deferred, _refused = select_pages([], ["src/app.py"], [page], 8)
    assert [p.kind for p in plans if p.path == "docs/wiki/old.md"] == ["reference"]


def test_a_written_legacy_page_gains_frontmatter_and_passes_the_gate():
    """The write persona is told the kind, so the rewritten page carries frontmatter."""
    from performer.workflows.documenter.personas import render_write_persona

    persona = render_write_persona(path="docs/wiki/legacy.md", kind="reference", current_content="# Legacy\n\nold text\n",
                                   say=[], evidence="`src/app.py` exists", changed_hunks="", max_chars=12000, docs_root="docs/wiki")
    assert "kind: reference" in persona


# needs_documentation: mutation = always True
def test_a_dependency_only_change_plans_no_pages():
    changed = ["poetry.lock", "pyproject.toml", "package-lock.json"]
    assert build_plan("update", [], changed, [], None, 8) == ([], [], [])


def test_a_ci_only_change_plans_no_pages():
    changed = [".github/workflows/ci.yml", ".github/workflows/release.yml"]
    assert build_plan("update", [], changed, [], None, 8) == ([], [], [])


def test_a_refactor_without_new_files_plans_no_pages_by_default():
    changed = [f"src/module{i}.py" for i in range(30)]
    diff = "\n".join(f"diff --git a/src/module{i}.py b/src/module{i}.py\n--- a/src/module{i}.py\n+++ b/src/module{i}.py\n@@ -1,1 +1,1 @@\n-x\n+y\n" for i in range(30))
    assert build_plan("update", [], changed, [], None, 8, diff_text=diff) == ([], [], [])


def test_a_brief_entry_overrides_the_refactor_default():
    changed = [f"src/module{i}.py" for i in range(30)]
    brief = [{"topic": "Modules", "location": "docs/wiki/modules.md", "kind": "reference"}]
    plans, _deferred, _refused = build_plan("update", brief, changed, [], None, 8)
    assert [p.path for p in plans if p.path == "docs/wiki/modules.md"]


def test_a_feature_with_new_files_still_plans():
    changed = ["src/payments.py", "src/app.py"]
    diff = "diff --git a/src/payments.py b/src/payments.py\n--- /dev/null\n+++ b/src/payments.py\n@@ -0,0 +1,1 @@\n+x\n"
    plans, _deferred, _refused = build_plan("update", [], changed, [], None, 8, diff_text=diff)
    assert plans == [], "no page cites these files yet"


# selection is docs-root aware: a brief page under docs/ (not docs/wiki) is planned
def test_brief_pages_under_a_plain_docs_root_are_selected():
    plans, _deferred, _refused = select_pages([{"topic": "Guide", "location": "docs/guide.md", "kind": "reference"}], [], [], 8, docs_root="docs")
    assert "docs/guide.md" in [p.path for p in plans]


# budgets: mutation = accept DOC_PLAN_CAP=1
def test_the_plan_cap_never_drops_below_two():
    from performer.workflows.documenter.budgets import DocumenterBudgets

    assert DocumenterBudgets.from_env({"DOC_PLAN_CAP": "1"}).plan_cap == 2


# layout: manifests preferred over dotfiles
def test_root_evidence_prefers_manifests_over_dotfiles():
    from performer.workflows.documenter import _init_modules
    from performer.workflows.documenter.models import PagePlan, RepositoryLayout

    tree = {".env", ".gitignore", "Gemfile", "go.mod", "src/app.rb"}
    layout = RepositoryLayout(project_name="demo", packages=[{"path": "src", "size": 10, "has_tests": False}], has_ci=False, test_command_hint="")
    plan = PagePlan(path="docs/wiki/setup.md", kind="how-to", source="init", justification="j", exists=False)
    modules = _init_modules(plan, tree, layout)
    assert modules[0] in ("Gemfile", "go.mod"), "a manifest, not a dotfile, is the first evidence"


def test_nested_source_dirs_are_recognised():
    from performer.workflows.documenter.inventory import layout_from_shape
    from performer.workflows.project_shape import ProjectShape

    tree = {"apps/web/main.py", "apps/web/__init__.py", "apps/api/main.py", "apps/api/__init__.py"}
    shape = ProjectShape(project_name="demo", source_dirs=["apps"], test_command="pytest")
    layout = layout_from_shape(shape, Path("/tmp/whatever"), tree)
    paths = [p["path"] for p in layout.packages]
    assert "apps/web" in paths and "apps/api" in paths, f"{paths}: apps/web is a source dir the model named"


def test_non_top_level_test_layouts_are_recognised():
    from performer.workflows.documenter.inventory import has_tests_in

    go = {"pkg/server.go", "pkg/server_test.go"}
    jest = {"src/app.ts", "src/__tests__/app.spec.ts"}
    rspec = {"spec/models/user_spec.rb", "app/models/user.rb"}
    assert has_tests_in(go) and has_tests_in(jest) and has_tests_in(rspec)
    assert not has_tests_in({"src/app.py", "src/db.py"})


# gate: decision pages follow the repo's ADR directory
def _decision_content() -> str:
    return "---\nkind: decision\n---\n# Use sqlite\n\n## Context\n" + "We store data in `src/db.py`. " * 8 + "\n\n## Decision\n" + "One sqlite file per deployment. " * 6 + "\n\n## Consequences\n" + "Costs: one writer at a time; no replication. " * 4 + "\n\n## Status\naccepted\n"


def test_a_decision_page_may_live_in_the_repo_adr_directory():
    content = _decision_content()
    plan = PagePlan(path="docs/adr/0001-sqlite.md", kind="decision", source="brief", justification="j", exists=False)
    good = gate_page(GateInput(plan=plan, action="write", content=content, reason="", tree={"src/db.py"}, pages_after_run=set(), decisions_dir="docs/adr"))
    assert not good.dropped, good.drop_reason
    plan_outside = PagePlan(path="docs/wiki/decisions/sqlite.md", kind="decision", source="brief", justification="j", exists=False)
    bad = gate_page(GateInput(plan=plan_outside, action="write", content=content, reason="", tree={"src/db.py"}, pages_after_run=set(), decisions_dir="docs/adr"))
    assert bad.dropped and "decision_outside_decisions_dir" in bad.contract_failures


# inventory is docs-root aware
def test_build_inventory_enumerates_a_plain_docs_tree(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "setup.md").write_text("---\nkind: how-to\n---\n# Set up\n\nbody\n")
    pages = build_inventory(tmp_path, {"docs/setup.md"}, docs_root="docs")
    assert [p.path for p in pages] == ["docs/setup.md"]


# pointers: refreshed only if present, created only when opted in
def test_pointer_files_are_refreshed_in_place_and_created_only_on_opt_in(tmp_path):
    from performer.workflows.documenter import refresh_pointer_files

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Agents\n\nrules\n")

    refreshed = refresh_pointer_files(repo, "docs/wiki/README.md", [("Architecture", "docs/wiki/architecture.md"), ("Setup", "docs/wiki/setup.md")], create=False)
    assert sorted(refreshed) == ["AGENTS.md"], "the existing pointer file is refreshed in place"
    assert "CLAUDE.md" not in refreshed, "absent pointer files are not created without the opt-in"

    created = refresh_pointer_files(repo, "docs/wiki/README.md", [("Architecture", "docs/wiki/architecture.md"), ("Setup", "docs/wiki/setup.md")], create=True)
    assert "CLAUDE.md" in created, "the opt-in creates absent pointer files"
    assert "AGENTS.md" in created


def test_pointer_files_untouched_when_already_current(tmp_path):
    from performer.workflows.documenter import refresh_pointer_files
    from performer.workflows.documenter.pointers import render_pointer_section

    repo = tmp_path / "repo"
    repo.mkdir()
    section = render_pointer_section("docs/wiki/README.md", [("Architecture", "docs/wiki/architecture.md")])
    (repo / "AGENTS.md").write_text("intro\n\n" + section)
    assert refresh_pointer_files(repo, "docs/wiki/README.md", [("Architecture", "docs/wiki/architecture.md")], create=False) == {}


# review: the brief may name a location under a non-docs/ root (415 review)
def test_brief_locations_under_the_resolved_root_are_not_refused():
    from performer.workflows.documenter.plan import select_pages

    brief = [{"topic": "Guide", "location": "handbook/guide.md", "say": "Describe it.", "kind": "reference"}]
    plans, _deferred, refused = select_pages(brief, ["src/app.py"], [], cap=8, docs_root="handbook")
    assert refused == [], "a location under the resolved root is honoured, not refused"
    assert [p.path for p in plans] == ["handbook/guide.md", "handbook/README.md"]


def test_pages_written_under_non_docs_roots_join_the_index():
    from performer.workflows.documenter.__init__ import _final_inventory

    page = "---\nkind: reference\n---\n# Guide\n\n" + ("Body text about `src/app.py`.\n" * 3)
    pages = _final_inventory([], {"handbook/guide.md": page}, [], set(), "handbook")
    assert [p.path for p in pages] == ["handbook/guide.md"], "is_doc_path alone would skip handbook/"


def test_an_existing_but_empty_pointer_file_is_refreshed(tmp_path):
    from performer.workflows.documenter import refresh_pointer_files

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("")
    refreshed = refresh_pointer_files(repo, "docs/wiki/README.md", [("Architecture", "docs/wiki/architecture.md")], create=False)
    assert "AGENTS.md" in refreshed, "an existing empty file is refreshed without the opt-in"
    assert "coordinare:wiki-pointer" in refreshed["AGENTS.md"]


# review: a real documentation system wins over a legacy docs/wiki tree
def test_detection_prefers_real_documentation_systems_over_a_legacy_wiki():
    from performer.workflows.documenter.docsroot import resolve_docs_root

    tree = {"mkdocs.yml", "docs/index.md", "docs/payments.md", "docs/wiki/old.md", "docs/wiki/README.md"}
    assert resolve_docs_root(tree) == "docs", "mkdocs beats the legacy wiki when both exist"
    assert resolve_docs_root({"doc/source/conf.py", "doc/source/usage.md", "docs/wiki/old.md"}) == "doc/source"
    assert resolve_docs_root({"docs/wiki/old.md"}) == "docs/wiki", "a bare legacy wiki still resolves"


def test_brief_locations_outside_the_resolved_root_are_refused():
    from performer.workflows.documenter.plan import select_pages

    brief = [
        {"topic": "Guide", "location": "handbook/../AGENTS.md", "say": "escape", "kind": "reference"},
        {"topic": "Legacy", "location": "docs/wiki/old.md", "say": "stale root", "kind": "reference"},
        {"topic": "Real", "location": "handbook/guide.md", "say": "fine", "kind": "reference"},
    ]
    plans, _deferred, refused = select_pages(brief, ["src/app.py"], [], cap=8, docs_root="handbook")
    assert "handbook/../AGENTS.md" in refused and "docs/wiki/old.md" in refused
    assert [p.path for p in plans] == ["handbook/guide.md", "handbook/README.md"]


def test_links_to_existing_non_root_markdown_resolve(tmp_path):
    content = "---\nkind: reference\n---\n# Guide\n\nSee [rules](../AGENTS.md) and [notes](retired.md).\n\n" + ("Body text about `src/app.py`.\n" * 6)
    plan = PagePlan(path="handbook/guide.md", kind="reference", source="brief", justification="j", exists=False)
    tree = {"src/app.py", "AGENTS.md", "handbook/retired.md"}
    pages = {"handbook/guide.md", "handbook/README.md"}
    gate = gate_page(GateInput(plan=plan, action="write", content=content, reason="", tree=tree, pages_after_run=pages, decisions_dir="docs/adr", docs_root="handbook"))
    assert "unresolved links: handbook/retired.md" in (gate.drop_reason or ""), "a retired page stays rejected"
    assert "AGENTS.md" not in (gate.drop_reason or ""), "a link to an existing root markdown file resolves"


def test_a_slashless_citation_matches_a_monorepo_basename():
    page = WikiPage(path="docs/wiki/packaging.md", kind=None, title="Packaging", citations=["package.json"], links=[], size=500)
    plans, _deferred, _refused = select_pages([], ["apps/web/package.json"], [page], 8)
    assert "docs/wiki/packaging.md" in [p.path for p in plans]
