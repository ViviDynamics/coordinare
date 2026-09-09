"""171 end-to-end scenarios on a real temporary git repository with a stubbed
model and a local committer: feature (brief page plus the cited architecture page
plus the generated README, one commit), trivial (empty plan, no model call, no
commit), hallucinated citation (page dropped, the rest commit), unchanged, retire
rules, shape failures (essay README ignored, how-to without Verify dropped,
changelog heading dropped), pointers, dirty tree hold, commit failure hold,
step events."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.models import Stand
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.documenter import STATES, WIKI_README, DocumenterWorkflow
from performer.workflows.documenter.models import DocsRecord
from performer.workflows.toolkit import Toolkit

from tests.unit.workflows.documenter._repo import (
    add_payments,
    head,
    local_committer,
    log,
    make_repo,
    sh,
    trivial_change,
)

PAYMENTS_PAGE = """---
kind: reference
---
# Payments module

The payments module lives in `src/payments.py` and stores charges through `src/db.py`.

| Name | What it is |
| --- | --- |
| `src/payments.py` | `charge(conn, amount)` inserts a charge row and returns True. |
| `src/db.py` | The sqlite connection factory the module uses. |

See [Architecture](architecture.md) for how requests reach it.
""" + ("\nCharges are integers in the smallest currency unit; the route in `src/app.py` returns a plain confirmation string.\n" * 3)


def _reply(action, content="", reason=""):
    return {"action": action, "content": content, "reason": reason}


async def _run_command(cmd, cwd, timeout_s):
    import asyncio
    proc = await asyncio.create_subprocess_shell(cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


def _toolkit(replies_by_path: dict[str, dict]):
    calls = {"paths": []}

    async def model_call(persona, content, max_tokens):
        import re
        m = re.search(r"Write the page `([^`]+)`", persona)
        path = m.group(1) if m else None
        calls["paths"].append(path)
        reply = replies_by_path.get(path) or _reply("unchanged", reason="nothing to add")
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    events = []
    return Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_run_command, event_sink=events.append, call_limit=20), events, calls


def _score(diff, brief=None, mode="update"):
    return SimpleNamespace(pr_diff=diff, documentation_brief=brief or {}, doc_mode=mode, issue_number=7, title="Add payments",
                           description="Charge users", workflow_env={}, owner_repo=("o", "r"), effective_github_token="t")


BRIEF = {"summary": "Add the payments module", "docs": [{"topic": "Payments module", "location": "docs/wiki/payments.md", "say": "Describe charge() and where charges are stored.", "kind": "reference"}], "modules": ["src/payments.py"]}


async def _run(repo, score, replies, committer=local_committer):
    tk, events, calls = _toolkit(replies)
    result = await DocumenterWorkflow(committer=committer).run(Stand(path=repo, branch="feat/payments"), score, tk)
    return DocsRecord.model_validate(result.report["docs"]), result, events, calls


@pytest.mark.asyncio
async def test_feature_writes_the_brief_page_the_cited_page_and_the_index_in_one_commit(tmp_path):
    repo = make_repo(tmp_path)
    diff = add_payments(repo)
    before = head(repo)
    record, _result, events, calls = await _run(repo, _score(diff, BRIEF), {"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE)})
    assert [p.path for p in record.plan] == ["docs/wiki/payments.md", "docs/wiki/architecture.md", WIKI_README]
    assert [p.source for p in record.plan] == ["brief", "inventory", "index"]
    assert record.plan[0].modules == ["src/payments.py"], "the brief's modules feed a brief page that names none of its own"
    assert record.evidence["docs/wiki/payments.md"]["chars"] > 0, "the new page gathered evidence to cite (live round: none, and the model refused to invent)"
    assert calls["paths"] == ["docs/wiki/payments.md", "docs/wiki/architecture.md"], "one write call per non-index page, none for the README"
    assert record.verdict == "docs_committed" and record.readme_generated
    assert set(record.files_written) >= {"docs/wiki/payments.md", WIKI_README, "AGENTS.md", "CLAUDE.md"}
    assert head(repo) != before and log(repo)[0].startswith("docs(#7):")
    readme = (repo / WIKI_README).read_text()
    assert readme.count("(payments.md)") == 1 and readme.count("(architecture.md)") == 1 and readme.count("(setup.md)") == 1
    assert readme.splitlines()[0] == "# demo" and readme.splitlines()[2].startswith("> ")
    assert (repo / "AGENTS.md").read_text().count("coordinare:wiki-pointer:start") == 1
    steps = [e.text for e in events if e.text.startswith("documenter.")]
    assert steps[0] == "documenter.intake" and steps[1] == "documenter.plan" and steps[-1] == "documenter.report" and "documenter.commit" in steps
    assert set(STATES) >= {s.split(".", 1)[1] for s in steps}
    assert sh(["git", "status", "--porcelain"], repo).strip() == ""


@pytest.mark.asyncio
async def test_a_trivial_card_makes_no_model_call_and_no_commit(tmp_path):
    repo = make_repo(tmp_path)
    diff = trivial_change(repo)
    before = head(repo)
    record, result, _, calls = await _run(repo, _score(diff), {})
    assert record.plan == [] and record.verdict == "docs_committed" and record.files_written == []
    assert calls["paths"] == [] and result.metrics.model_calls == 0 and head(repo) == before


@pytest.mark.asyncio
async def test_a_page_citing_a_missing_module_is_dropped_and_the_rest_commit(tmp_path):
    repo = make_repo(tmp_path)
    diff = add_payments(repo)
    bad = PAYMENTS_PAGE.replace("`src/payments.py` and stores", "`src/payments/ledger.py` and stores")
    record, _, _, _ = await _run(repo, _score(diff, BRIEF), {"docs/wiki/payments.md": _reply("write", bad)})
    dropped = [r for r in record.results if r.dropped]
    assert [r.path for r in dropped] == ["docs/wiki/payments.md"] and "src/payments/ledger.py" in dropped[0].citations_missing
    assert record.files_written == [], "nothing else changed, so nothing is written and the index stays as it was"
    assert record.verdict == "docs_committed" and not (repo / "docs/wiki/payments.md").exists()


@pytest.mark.asyncio
async def test_unchanged_writes_nothing_and_records_the_reason(tmp_path):
    repo = make_repo(tmp_path)
    diff = add_payments(repo)
    record, _, _, _ = await _run(repo, _score(diff, BRIEF), {"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE), "docs/wiki/architecture.md": _reply("unchanged", reason="the module list is still right")})
    arch = next(r for r in record.results if r.path == "docs/wiki/architecture.md")
    assert arch.action == "unchanged" and not arch.dropped and "still right" in arch.reason
    assert "docs/wiki/architecture.md" not in record.files_written


@pytest.mark.asyncio
async def test_retire_is_accepted_only_for_an_inventory_page_whose_citations_are_gone(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "docs/wiki/legacy.md").write_text("---\nkind: reference\n---\n# Legacy\n\n- `src/legacy.py`: the old entry point.\n")
    sh(["git", "-c", "user.email=e@x", "-c", "user.name=t", "add", "-A"], repo)
    sh(["git", "-c", "user.email=e@x", "-c", "user.name=t", "commit", "-qm", "legacy page"], repo)
    (repo / "src/legacy.py").write_text("")  # create then remove in the feature so the diff touches it
    sh(["git", "-c", "user.email=e@x", "-c", "user.name=t", "add", "-A"], repo)
    sh(["git", "-c", "user.email=e@x", "-c", "user.name=t", "commit", "-qm", "legacy module"], repo)
    sh(["git", "checkout", "-q", "-b", "feat/remove-legacy"], repo)
    sh(["git", "-c", "user.email=e@x", "-c", "user.name=t", "rm", "-q", "src/legacy.py"], repo)
    sh(["git", "-c", "user.email=e@x", "-c", "user.name=t", "commit", "-qm", "remove legacy"], repo)
    diff = sh(["git", "diff", "main...HEAD"], repo)
    record, _, _, _ = await _run(repo, _score(diff), {"docs/wiki/legacy.md": _reply("retire", reason="the module is gone")})
    legacy = next(r for r in record.results if r.path == "docs/wiki/legacy.md")
    assert legacy.action == "retire" and not legacy.dropped and record.files_retired == ["docs/wiki/legacy.md"]
    assert not (repo / "docs/wiki/legacy.md").exists()
    # a brief-sourced page can never be retired
    repo2 = make_repo(tmp_path / "two")
    diff2 = add_payments(repo2)
    record2, _, _, _ = await _run(repo2, _score(diff2, BRIEF), {"docs/wiki/payments.md": _reply("retire", reason="no")})
    assert next(r for r in record2.results if r.path == "docs/wiki/payments.md").drop_reason == "retire_not_justified"


@pytest.mark.asyncio
async def test_shape_failures_are_dropped_with_the_check_named(tmp_path):
    repo = make_repo(tmp_path)
    diff = add_payments(repo)
    no_verify = PAYMENTS_PAGE.replace("kind: reference", "kind: how-to").replace("# Payments module", "# Charge a user\n\n## Goal\nCharge.\n\n## Prerequisites\nA connection from `src/db.py`.\n\n## Steps\n1. Call `charge`.\n")
    changelog = PAYMENTS_PAGE + "\n## Card #7 changes\n- added charge\n"
    record, _, _, _ = await _run(repo, _score(diff, {**BRIEF, "docs": [{**BRIEF["docs"][0], "kind": "how-to"}]}), {"docs/wiki/payments.md": _reply("write", no_verify)})
    r = next(x for x in record.results if x.path == "docs/wiki/payments.md")
    assert r.dropped and "missing_heading:Verify" in r.contract_failures
    repo2 = make_repo(tmp_path / "two")
    diff2 = add_payments(repo2)
    record2, _, _, _ = await _run(repo2, _score(diff2, BRIEF), {"docs/wiki/payments.md": _reply("write", changelog)})
    r2 = next(x for x in record2.results if x.path == "docs/wiki/payments.md")
    assert r2.dropped and "changelog_heading" in r2.contract_failures


@pytest.mark.asyncio
async def test_a_stale_pointer_section_is_replaced_between_markers_and_the_rest_kept(tmp_path):
    agents = "# Agents\n\nBuild with `make`.\n\n<!-- coordinare:wiki-pointer:start -->\nold stuff\n<!-- coordinare:wiki-pointer:end -->\n\nMore rules.\n"
    repo = make_repo(tmp_path, agents_md=agents)
    diff = add_payments(repo)
    record, _, _, _ = await _run(repo, _score(diff, BRIEF), {"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE)})
    text = (repo / "AGENTS.md").read_text()
    assert text.startswith("# Agents\n\nBuild with `make`.\n\n<!-- coordinare:wiki-pointer:start -->") and text.endswith("<!-- coordinare:wiki-pointer:end -->\n\nMore rules.\n")
    assert "old stuff" not in text and "docs/wiki/README.md" in text
    section = text.split("<!-- coordinare:wiki-pointer:start -->")[1].split("<!-- coordinare:wiki-pointer:end -->")[0]
    assert section.count("\n") < 40 and "AGENTS.md" in record.pointers_refreshed


@pytest.mark.asyncio
async def test_a_dirty_tree_at_start_is_a_hold(tmp_path):
    repo = make_repo(tmp_path)
    diff = add_payments(repo)
    (repo / "src/app.py").write_text("# leftover edit\n")
    record, _, _, calls = await _run(repo, _score(diff, BRIEF), {"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE)})
    assert record.verdict == "env_blocked" and "dirty" in record.hold_reason and "src/app.py" in record.hold_reason and calls["paths"] == []


@pytest.mark.asyncio
async def test_a_failed_commit_is_a_hold_with_nothing_reported_written(tmp_path):
    repo = make_repo(tmp_path)
    diff = add_payments(repo)

    async def boom(stand, files, message, deletions=None, *, score=None):
        raise RuntimeError("push rejected")

    record, _, _, _ = await _run(repo, _score(diff, BRIEF), {"docs/wiki/payments.md": _reply("write", PAYMENTS_PAGE)}, committer=boom)
    assert record.verdict == "env_blocked" and "push rejected" in record.hold_reason and record.files_written == []


@pytest.mark.asyncio
async def test_init_mode_builds_a_capped_skeleton_and_adds_pointers(tmp_path):
    repo = make_repo(tmp_path, with_wiki=False)
    for i in range(6):
        (repo / f"pkg{i}").mkdir()
        (repo / f"pkg{i}" / "__init__.py").write_text("x = 1\n" * (i + 1) * 10)
        (repo / f"pkg{i}" / "tests").mkdir()
        (repo / f"pkg{i}" / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    sh(["git", "-c", "user.email=e@x", "-c", "user.name=t", "add", "-A"], repo)
    sh(["git", "-c", "user.email=e@x", "-c", "user.name=t", "commit", "-qm", "packages"], repo)
    generic = lambda kind, title: _reply("write", f"---\nkind: {kind}\n---\n# {title}\n\n" + _kind_body(kind))  # noqa: E731
    replies = {"docs/wiki/architecture.md": generic("explanation", "Architecture"), "docs/wiki/setup.md": generic("how-to", "Set up"), "docs/wiki/testing.md": generic("how-to", "Run the tests")}
    for i in range(6):
        replies[f"docs/wiki/pkg{i}.md"] = generic("reference", f"pkg{i}")
    record, _, _, _calls = await _run(repo, _score("", None, mode="init"), replies)
    assert len(record.plan) <= 8 and record.plan[0].path == WIKI_README and record.deferred, record.deferred
    assert record.verdict == "docs_committed" and (repo / WIKI_README).exists() and (repo / "CLAUDE.md").exists()
    readme = (repo / WIKI_README).read_text()
    for r in record.results:
        if r.action == "write" and not r.dropped:
            assert readme.count(f"({Path(r.path).name})") == 1


def _kind_body(kind: str) -> str:
    filler = "The code in `src/app.py` handles requests and `src/db.py` stores data. " * 6
    if kind == "explanation":
        return f"## What it is\n{filler}\n\n## How it fits\n{filler}\n\n## Why it is this way\n{filler}\n\n## Where to change it\nStart from `src/app.py`; tests in `tests/test_app.py`.\n"
    if kind == "how-to":
        return f"## Goal\n{filler}\n\n## Prerequisites\n`pyproject.toml`.\n\n## Steps\n1. Run:\n\n```bash\npytest\n```\n\n## Verify\n{filler}\n"
    return f"| Name | What it is |\n| --- | --- |\n| `src/app.py` | the app |\n| `src/db.py` | storage |\n\n{filler}\n"


def test_the_persona_offers_retire_only_for_an_existing_inventory_page():
    """Live init round: the model answered retire for pages that did not exist yet."""
    from performer.workflows.documenter.personas import render_write_persona

    common = dict(path="docs/wiki/x.md", kind="reference", current_content="", say=[], evidence="", changed_hunks="", max_chars=12000)
    assert '"retire"' not in render_write_persona(**common) and "do not answer retire" in render_write_persona(**common)
    assert "does not exist yet" in render_write_persona(**common)
    existing = render_write_persona(**{**common, "current_content": "# X\n"}, allow_retire=True)
    assert '"retire"' in existing and "The page exists" in existing


@pytest.mark.asyncio
async def test_an_index_only_plan_regenerates_the_readme_with_no_model_call(tmp_path):
    """Review finding: pages all `unchanged` left the README stale although the plan selected it."""
    repo = make_repo(tmp_path)
    diff = add_payments(repo)
    record, _result, _, _calls = await _run(repo, _score(diff), {"docs/wiki/architecture.md": _reply("unchanged", reason="still right")})
    assert [p.path for p in record.plan] == ["docs/wiki/architecture.md", WIKI_README]
    assert record.readme_generated is False, "a page answered unchanged and nothing else changed: the index is already right"
    # an index-only plan (nothing but the README) regenerates it without a model call
    from performer.workflows.documenter import plan as plan_mod
    from performer.workflows.documenter.models import PagePlan
    original = plan_mod.build_plan
    try:
        plan_mod_build = lambda *a, **k: ([PagePlan(path=WIKI_README, kind=None, source="index", justification="Index refresh", exists=True)], [], [])  # noqa: E731
        import performer.workflows.documenter as wf
        wf.build_plan = plan_mod_build
        record2, result2, _, calls2 = await _run(repo, _score(diff), {})
        assert record2.readme_generated and calls2["paths"] == [] and result2.metrics.model_calls == 0
    finally:
        import performer.workflows.documenter as wf
        wf.build_plan = original


@pytest.mark.asyncio
async def test_175_side_integrates_structured_findings_without_root_pointer_writes(tmp_path):
    repo = make_repo(tmp_path)
    add_payments(repo)
    score = _score("", {**BRIEF, "modules": [{"path": "src/payments/ledger.py", "note": "Payment ledger"}]})
    score.documenting_side_run = True
    score.documentation_findings = {
        "reviewing": {"role": "reviewing", "source_head": "priorhead", "content_hash": "hash", "findings": {"verdict": "approved", "findings": [{"body": "Currency amounts are integer cents"}]}}
    }
    seen = []

    async def model_call(persona, content, max_tokens):
        seen.append(persona)
        if "Write the page `docs/wiki/payments.md`" in persona:
            return ModelReply(content=json.dumps(_reply("write", PAYMENTS_PAGE)), finish_reason="stop")
        return ModelReply(content=json.dumps(_reply("unchanged", reason="current")), finish_reason="stop")

    toolkit = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_run_command, call_limit=20)
    result = await DocumenterWorkflow(committer=local_committer).run(Stand(path=repo, branch="feat/payments"), score, toolkit)
    docs = result.report["docs"]
    assert docs["verdict"] == "docs_committed"
    assert docs["files_written"] and all(p.startswith("docs/") for p in docs["files_written"])
    assert docs["pointers_refreshed"] == []
    assert any("src/payments/ledger.py" in entry["modules"] for entry in docs["plan"])
    assert any("Currency amounts are integer cents" in p and "priorhead" in p for p in seen)
    assert (repo / "docs/wiki/payments.md").exists()
