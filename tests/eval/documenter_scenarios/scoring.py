"""Scoring for one documenter run against a fixture's expectations (spec 171 SC-001 to SC-003, SC-005, SC-006)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from performer.workflows.documenter.index import readme_shape_ok
from performer.workflows.documenter.inventory import build_inventory, extract_citations
from performer.workflows.documenter.models import DocsRecord
from performer.workflows.documenter.plan import is_doc_path

from tests.eval.documenter_scenarios.fixtures import Fixture


@dataclass
class Score:
    fixture: str
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout


def score_run(fixture: Fixture, report: dict, repo: Path, head_before: str, model_calls: int, *, live: bool = False) -> Score:
    exp = fixture.expect
    s = Score(fixture=fixture.name)
    try:
        record = DocsRecord.model_validate(report.get("docs") or {})
    except Exception as exc:
        s.checks["record_valid"] = False
        s.notes.append(f"docs record invalid: {exc}")
        return s
    s.checks["record_valid"] = True
    accepted = set(exp.live_verdicts) if live else {exp.verdict}
    s.checks["verdict"] = record.verdict in accepted
    n = len(record.files_written)
    s.checks["written_count"] = (exp.min_written <= n <= exp.max_written) if not live else True
    if exp.model_calls is not None and not live:
        s.checks["model_calls"] = model_calls == exp.model_calls
    head_after = _git(repo, "rev-parse", "HEAD").strip()
    s.checks["committed"] = (head_after != head_before) == exp.committed if not live else True
    s.checks["one_commit"] = (len(_git(repo, "rev-list", f"{head_before}..HEAD").split()) <= 1)
    s.checks["only_doc_paths"] = all(is_doc_path(p) for p in _git(repo, "diff", "--name-only", head_before, "HEAD").split())
    s.checks["clean_tree"] = _git(repo, "status", "--porcelain").strip() == ""
    for p in exp.dropped_paths:
        s.checks[f"dropped:{p}"] = any(r.path == p and r.dropped for r in record.results)
    s.checks["readme_generated"] = record.readme_generated == exp.readme_generated if not live else True
    s.checks["pointers"] = (bool(record.pointers_refreshed) == exp.pointers) if not live else True
    tree = set(_git(repo, "ls-files").split())
    pages = build_inventory(repo, tree)
    if (repo / "docs/wiki/README.md").exists() and record.readme_generated:
        failures = readme_shape_ok((repo / "docs/wiki/README.md").read_text(), [p for p in pages if p.path != "docs/wiki/README.md"])
        s.checks["readme_shape"] = not failures
        if failures:
            s.notes.append("readme shape: " + ", ".join(failures))
    for page in pages:
        text = (repo / page.path).read_text()
        cited = extract_citations(text, tree | set(page.citations))
        missing = [c for c in cited if c.rstrip("/") not in tree and not any(t.startswith(c.rstrip("/") + "/") for t in tree)]
        if missing:
            s.checks[f"citations:{page.path}"] = False
            s.notes.append(f"{page.path} cites missing {missing[:3]}")
    return s
