"""412: explicit empty/non-source verdicts, argv scoping, exit-code tolerance.

The security workflow used to run its whole sequence on a binary-only or
lockfile-only diff and pass on zero evidence, and a scanner that printed
nothing after exit 0 was a crash. These tests hold the fixes: code-decided
short-circuits, mechanical argv scoping, declared exit codes, clamped evidence
and non-fatal malformed scanner rows.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.workflows.reviewer.diffparse import _UNNAMED_TAIL_PATH
from performer.workflows.security.budgets import SecurityBudgets
from performer.workflows.security.gate import scanner_to_findings
from performer.workflows.security.models import SECURITY_CATEGORIES
from performer.workflows.security.scanner import (
    FILES_TOKEN,
    NothingToScan,
    ScannerUnavailable,
    _option_takes_value,
    _scoped_argv,
    default_runner,
    has_scannable_source,
    is_scannable_source,
    run_scan,
)

# --- the source classifier --------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("src/app/vuln.py", True),
    ("app.py", True),
    ("lib/impl.rs", True),
    ("Dockerfile", True),
    ("docker/Dockerfile.prod", True),
    ("Dockerfile.dev", True),
    ("Makefile", True),
    ("Dockerfile.md", False),
    ("Makefile.txt", False),
    ("Jenkinsfile.rst", False),
    ("deploy/infra/main.tf", True),
    ("config/settings.yaml", True),
    ("package-lock.json", False),
    ("npm-shrinkwrap.json", False),
    ("yarn.lock", False),
    ("Cargo.lock", False),
    ("go.sum", False),
    ("static/logo.png", False),
    ("docs/site.js.map", False),
    ("", False),
    (".env", True),
    (".env.example", True),
    ("config/.env.production", True),
    ("requirements.txt", True),
    ("requirements-dev.txt", True),
    ("requirements.md", False),
    ("requirements-notes.md", False),
    ("pyproject.toml", True),
    ("Gemfile", True),
    ("go.mod", True),
    ("package.json", True),
    ("pom.xml", True),
    ("Gemfile.lock", False),
])
def test_is_scannable_source(path: str, expected: bool) -> None:
    assert is_scannable_source(path) is expected


def test_has_scannable_source_is_any() -> None:
    assert has_scannable_source(["a.png", "b.py"])
    assert not has_scannable_source(["a.png", "package-lock.json"])
    assert has_scannable_source(["a.png", "requirements.txt"]), "412 round 5: a dependency manifest is scannable -- supply-chain categories stay reachable"
    assert not has_scannable_source([])


# --- argv scoping -----------------------------------------------------------

def test_scoped_argv_substitutes_the_token() -> None:
    argv = _scoped_argv(["semgrep", "--json", FILES_TOKEN], ["a.py", "b.py"])
    assert argv == ["semgrep", "--json", "a.py", "b.py"]


def test_scoped_argv_appends_without_the_token() -> None:
    assert _scoped_argv(["bandit", "-f", "json"], ["a.py"]) == ["bandit", "-f", "json", "a.py"]


def test_scoped_argv_keeps_the_token_literal_in_value_position() -> None:
    """412 round 17: ``--exclude {{files}}`` would pass the changed files as
    exclusion values -- the scan reads clean on zero evidence.
    412 round 33: the literal-stays fallback is gone -- round 17 assumed the
    tool fails on the nonexistent path, but a tool that ignores an unknown
    value scans its default tree unscoped -- so the malformed plan is
    refused and the card holds."""
    with pytest.raises(ScannerUnavailable):
        _scoped_argv(["semgrep", "--exclude", FILES_TOKEN], ["a.py", "b.py"])


def test_scoped_argv_replaces_broad_operands() -> None:
    """412 round 2: a plan like ``semgrep .`` or ``gosec ./...`` scans the
    whole workspace; the operand is replaced by the changed files.
    412 round 29: that holds even after an unknown flag (``--json``) -- a
    broad operand has no scoped reading, and a mangled option that makes
    the tool fail holds the card, which is the safe side."""
    assert _scoped_argv(["semgrep", "--json", "."], ["a.py", "b.py"]) == ["semgrep", "--json", "a.py", "b.py"]
    assert _scoped_argv(["gosec", "./..."], ["a.py"]) == ["gosec", "a.py"]
    assert _scoped_argv(["trivy", "fs", "pkg/..."], ["a.py"]) == ["trivy", "fs", "a.py"]


def test_scoped_argv_replaces_workspace_directory_operands(tmp_path) -> None:
    """412 round 4: ``semgrep src`` or ``trivy fs <root>`` scans every
    pre-existing file under the named directory -- replaced by the changed
    files exactly like the broad literals. 412 round 15: an under-root
    absolute SOURCE operand is replaced too; a rules file rides an option
    value (`bandit -c rules.yml`), which stays alone."""
    (tmp_path / "src").mkdir()
    assert _scoped_argv(["semgrep", "src"], ["a.py"], tmp_path) == ["semgrep", "a.py"]
    assert _scoped_argv(["trivy", "fs", str(tmp_path)], ["a.py"], tmp_path) == ["trivy", "fs", "a.py"]
    rules = tmp_path / "custom.yml"
    rules.write_text("rules: []\n")
    assert _scoped_argv(["bandit", "-c", str(rules)], ["a.py"], tmp_path) == ["bandit", "-c", str(rules), "a.py"]


def test_scoped_argv_replaces_literal_workspace_file_operands(tmp_path) -> None:
    """412 round 13: a literal file operand kept from the model's plan must
    not scan out-of-diff code beside the changed set."""
    (tmp_path / "old.py").write_text("X = 1\n")
    assert _scoped_argv(["bandit", "old.py"], ["a.py"], tmp_path) == ["bandit", "a.py"]


def test_scoped_argv_replaces_relative_glob_operands() -> None:
    """412 round 14: a relative glob expands to pre-existing files outside
    the diff -- replaced by the changed set like any broad operand. After an
    unknown option the glob is ambiguous (filter value or unscoped operand?)
    and 412 round 44 refuses the plan rather than preserving the expansion;
    report paths ride inline option values, which stay preserved."""
    assert _scoped_argv(["semgrep", "src/**/*.py"], ["a.py"]) == ["semgrep", "a.py"]
    with pytest.raises(ScannerUnavailable):
        _scoped_argv(["semgrep", "--json", "src/**/*.py"], ["a.py"])
    assert _scoped_argv(["gitleaks", "report", "--path=/tmp/out/x.json"], ["a.py"]) == [
        "gitleaks", "report", "--path=/tmp/out/x.json", "a.py",
    ]


def test_scoped_argv_replaces_operands_naming_changed_paths() -> None:
    """412 round 15: a literal operand naming a changed path is replaced even
    when the path does not exist on disk (a deletion, or a named phantom
    beyond the truncation cap) -- the workflow deliberately excluded it, so
    it cannot re-enter the argv beside the changed set."""
    assert _scoped_argv(["bandit", "gone.py"], ["gone.py", "x.py"]) == ["bandit", "gone.py", "x.py"]


def test_scoped_argv_refuses_narrow_paths_after_unknown_flags() -> None:
    """412 round 28: ``--check`` is not a known value-taker, so a NARROW
    path after it is ambiguous -- value or operand? 412 round 44: the plan
    is refused rather than preserved -- preserving let an operand-shaped
    token read repository code beside the changed set, and replacing risked
    the false-pass direction (a replaced value, a scan on nothing reading as
    clean). 412 round 29: a BROAD operand after the same flag has no scoped
    reading and is still replaced."""
    with pytest.raises(ScannerUnavailable):
        _scoped_argv(["semgrep", "--check", "old.py"], ["a.py"])
    assert _scoped_argv(["semgrep", "--check", "."], ["a.py"]) == ["semgrep", "--check", "a.py"]


def test_scoped_argv_treats_tool_specific_option_values_as_ambiguous(tmp_path) -> None:
    """412 round 28: the whitelist cannot know every tool's options --
    ``semgrep --json-output results.json`` carries an existing workspace
    file the old classification replaced with the change set, overwriting
    the output. 412 round 44: the spaced form is refused (the model
    re-plans); the inline spelling is a single token and stays preserved,
    as does the broad ``.`` after ``--source``, replaced like any bare
    broad operand (if the tool cannot take the changed list there, it fails
    and the card holds -- the safe side)."""
    (tmp_path / "results.json").write_text("{}\n")
    assert _scoped_argv(["gitleaks", "detect", "--source", "."], ["a.py"], tmp_path) == [
        "gitleaks", "detect", "--source", "a.py",
    ]
    with pytest.raises(ScannerUnavailable):
        _scoped_argv(["semgrep", "scan", "--json-output", "results.json"], ["a.py"], tmp_path)
    assert _scoped_argv(["semgrep", "scan", "--json-output=results.json"], ["a.py"], tmp_path) == [
        "semgrep", "scan", "--json-output=results.json", "a.py",
    ]


def test_scoped_argv_treats_the_options_delimiter_as_a_delimiter(tmp_path) -> None:
    """412 round 30: ``--`` ends the options -- it is not an unknown
    value-taking flag, so the operand after it is a plain positional and
    keeps the ordinary target rules (an out-of-diff existing file stays;
    the change set is scoped like any bare operand)."""
    (tmp_path / "old.py").write_text("x = 1\n")
    assert _scoped_argv(["bandit", "--", "old.py"], ["a.py"], tmp_path) == ["bandit", "--", "a.py"]
    assert _scoped_argv(["bandit", "--", "a.py"], ["a.py"], tmp_path) == ["bandit", "--", "a.py"]


def test_scoped_argv_replaces_absolute_operands_under_the_workspace_root(tmp_path) -> None:
    """412 round 15: an absolute operand under the workspace root scans
    pre-existing code exactly like its relative spelling; absolute paths
    outside the workspace (report paths, caches) stay alone."""
    (tmp_path / "old.py").write_text("X = 1\n")
    assert _scoped_argv(["bandit", str(tmp_path / "old.py")], ["a.py"], tmp_path) == ["bandit", "a.py"]


def test_scoped_argv_replaces_absolute_globs_under_the_workspace_root(tmp_path) -> None:
    """412 round 43: an absolute glob under the workspace root expands to
    pre-existing files outside the diff exactly like its relative spelling,
    so it is a scan target -- existence is not the trigger for globs,
    expansion is."""
    assert _scoped_argv(["semgrep", f"{tmp_path}/**/*.py", "."], ["a.py"], tmp_path) == [
        "semgrep", "a.py",
    ]


def test_scoped_argv_refuses_an_absolute_operand_after_an_unknown_flag(tmp_path) -> None:
    """412 round 43: ``gitleaks --no-banner /etc/passwd`` -- the flag is not
    in the value whitelist, so the following absolute path is ambiguous
    (value or read-target). Preserving it let a model-authored plan scan and
    report content outside the workspace; the plan is refused and the model
    re-plans (the inline ``--path=/tmp/out.json`` spelling stays preserved)."""
    with pytest.raises(ScannerUnavailable):
        _scoped_argv(["gitleaks", "--no-banner", "/etc/passwd"], ["a.py"], tmp_path)


def test_scoped_argv_leaves_subcommands_and_plumbing_alone() -> None:
    """412 round 13: nonexistent relative tokens (trivy's ``fs``) keep the
    old behavior; report paths ride inline option values (412 round 43: the
    spaced absolute form is refused -- read-target or value is
    indistinguishable after an unknown flag)."""
    assert _scoped_argv(["trivy", "fs", "pkg/..."], ["a.py"]) == ["trivy", "fs", "a.py"]
    assert _scoped_argv(["gitleaks", "--report-path=/tmp/out.json", "--json", "."], ["a.py"]) == [
        "gitleaks",
        "--report-path=/tmp/out.json",
        "--json",
        "a.py",
    ]


def test_option_takes_value_rejects_inline_values() -> None:
    """412 round 13: ``--opt=value`` carries its value inline -- the NEXT
    element is not consumed, so a broad operand after it is still a scan
    target."""
    assert _option_takes_value("--config=rules") is False
    assert _option_takes_value("--config") is True
    assert _scoped_argv(["semgrep", "--config=rules", "."], ["a.py"]) == ["semgrep", "--config=rules", "a.py"]


def test_scoped_argv_never_replaces_an_option_value(tmp_path) -> None:
    """412 round 9: a workspace directory as an option VALUE is not a scan
    operand -- ``semgrep --config rules/`` must keep its config and get the
    changed files appended. The same directory as a bare operand is still
    replaced."""
    (tmp_path / "rules").mkdir()
    assert _scoped_argv(["semgrep", "--config", "rules/", "-f", "json"], ["a.py"], tmp_path) == [
        "semgrep", "--config", "rules/", "-f", "json", "a.py",
    ]
    assert _scoped_argv(["semgrep", "rules/", "-f", "json"], ["a.py"], tmp_path) == ["semgrep", "a.py", "-f", "json"]


def test_scoped_argv_option_value_guard_covers_broad_operands() -> None:
    """412 round 11: broad-operand replacement must not fire on option
    values -- ``--exclude pkg/...`` replaced by the changed files would
    exclude the changed files themselves and read as a clean pass on zero
    evidence. 412 round 17: ``{{files}}`` is guarded the same way -- a value
    position is not substituted, because passing the changed files as
    exclusion values excludes the scan itself; the literal stays (the tool
    fails on the nonexistent path) and the files are appended as targets."""
    assert _scoped_argv(["semgrep", "--exclude", "pkg/..."], ["a.py"]) == [
        "semgrep", "--exclude", "pkg/...", "a.py",
    ]
    assert _scoped_argv(["gosec", "-exclude-dir", "./..."], ["a.py"]) == [
        "gosec", "-exclude-dir", "./...", "a.py",
    ]
    assert _scoped_argv(["semgrep", "--exclude", "."], ["a.py"]) == ["semgrep", "--exclude", ".", "a.py"]
    # 412 round 33: the token in a value position is a malformed plan.
    with pytest.raises(ScannerUnavailable):
        _scoped_argv(["semgrep", "--exclude", FILES_TOKEN], ["a.py"])


def test_scoped_argv_refuses_an_inline_broad_operand() -> None:
    """412 round 32: ``gitleaks --source=.`` scans the whole repository behind
    the appended change set, and the inline form cannot be rewritten safely
    (splitting ``--exclude=vendor/...`` would exclude the changed files
    themselves) -- so an inline value naming a broad workspace path on a
    non-whitelisted option is refused and the card holds."""
    for element in ("--source=.", "--path=./", "-source=./...", "--src=."):
        with pytest.raises(ScannerUnavailable):
            _scoped_argv(["gitleaks", element], ["src/app.py"])
    # Whitelisted value-takers keep the rounds-9-13 contract: the inline
    # value is plumbing (config, exclusions, output), not a scan operand.
    assert _scoped_argv(["semgrep", "--exclude=vendor/..."], ["a.py"]) == [
        "semgrep", "--exclude=vendor/...", "a.py",
    ]
    assert _scoped_argv(["semgrep", "--config=rules/"], ["a.py"]) == ["semgrep", "--config=rules/", "a.py"]
    # An inline token naming a changed-set element is not broad, so it is
    # preserved with the change set appended (the pre-round-32 behavior).
    assert _scoped_argv(["tool", "--path=./a.py"], ["a.py"]) == ["tool", "--path=./a.py", "a.py"]


def test_scoped_argv_refuses_operands_outside_the_workspace(tmp_path) -> None:
    """412 round 41: an operand outside repo_root is a scan target the
    scoping cannot bound -- the tool would read it beside the changed set
    and its findings can carry the file's contents into the report -- so
    the plan is refused, superseding the round-13-era ignore-and-append."""
    other = tmp_path.parent / "elsewhere-not-created"
    with pytest.raises(ScannerUnavailable):
        _scoped_argv(["scan", str(other)], ["a.py"], tmp_path)


def test_scoped_argv_token_and_broad_operand_inject_once(tmp_path) -> None:
    """412 round 6: ``semgrep . {{files}}`` must not scan the whole workspace
    beside the changed files -- the broad operand is replaced, the token
    expands, and one copy of the list survives. 412 round 29: a broad
    operand after an unknown flag (``--baseline``) is replaced too -- it
    has no scoped reading."""
    assert _scoped_argv(["semgrep", ".", "{{files}}"], ["a.py"], tmp_path) == ["semgrep", "a.py"]
    assert _scoped_argv(["semgrep", "{{files}}", "--baseline", "."], ["a.py"], tmp_path) == ["semgrep", "a.py", "--baseline"]
    assert _scoped_argv(["gosec", "./...", "{{files}}"], ["a.py", "b.py"], tmp_path) == ["gosec", "a.py", "b.py"]


# --- run_scan boundaries ----------------------------------------------------

@pytest.mark.asyncio
async def test_run_scan_raises_nothing_to_scan_on_empty_files(tmp_path: Path) -> None:
    with pytest.raises(NothingToScan):
        await run_scan([], tmp_path, tools=[], runner=default_runner, budgets=SecurityBudgets())


@pytest.mark.asyncio
async def test_empty_stdout_with_ok_exit_is_a_clean_run(tmp_path: Path) -> None:
    """412: empty output after a declared-ok exit is quiet, not broken -- the
    reading's per-file coverage is what holds the card."""
    async def runner(argv, cwd, timeout_s):
        return 0, "", ""

    async def read(*a, **k):
        return _reading(examined=True)

    findings, results = await run_scan(
        ["a.py"], tmp_path,
        tools=[SimpleNamespace(name="quiet", argv=["quiet", "a.py"], why="w", ok_exit_codes=[0, 1])],
        read=read, runner=runner, budgets=SecurityBudgets(),
    )
    assert findings == [] and results[0].exit_code == 0


@pytest.mark.asyncio
async def test_declared_exit_codes_union_the_defaults(tmp_path: Path) -> None:
    """412 round 3: a plan declaring only trivy's 2 still reads a clean 0 as
    'the tool ran' -- declarations are extra, they do not replace {0, 1}."""
    async def runner(argv, cwd, timeout_s):
        return 0, "", ""

    async def read(*a, **k):
        return _reading(examined=True)

    findings, results = await run_scan(
        ["a.py"], tmp_path,
        tools=[SimpleNamespace(name="trivy", argv=["trivy", "a.py"], why="w", ok_exit_codes=[2])],
        read=read, runner=runner, budgets=SecurityBudgets(),
    )
    assert findings == [] and results[0].exit_code == 0


@pytest.mark.asyncio
async def test_undeclared_exit_code_fails_closed(tmp_path: Path) -> None:
    async def runner(argv, cwd, timeout_s):
        return 3, "usage error", ""

    async def read(*a, **k):
        return _reading(examined=True)

    with pytest.raises(ScannerUnavailable):
        await run_scan(
            ["a.py"], tmp_path,
            tools=[SimpleNamespace(name="x", argv=["x", "a.py"], why="w", ok_exit_codes=[0, 1])],
            read=read, runner=runner, budgets=SecurityBudgets(),
        )


@pytest.mark.asyncio
async def test_declared_extra_exit_code_runs(tmp_path: Path) -> None:
    """pylint's bitmask: exit 2 means 'the tool ran', declared by the model."""
    async def runner(argv, cwd, timeout_s):
        return 2, "no findings", ""

    async def read(*a, **k):
        return _reading(examined=True)

    _findings, results = await run_scan(
        ["a.py"], tmp_path,
        tools=[SimpleNamespace(name="lint", argv=["lint", FILES_TOKEN, "a.py"], why="w", ok_exit_codes=[0, 1, 2])],
        read=read, runner=runner, budgets=SecurityBudgets(),
    )
    assert results[0].exit_code == 2


@pytest.mark.asyncio
async def test_os_failure_exit_codes_are_never_ok(tmp_path: Path) -> None:
    """412 round 33: a model-declared ok code is an application code, never
    an OS process failure -- 126 (permission denied), 127 (not found) or a
    signal death (>= 128) declared ok would read a scanner that never ran
    as a clean pass. Application extras (trivy's 2) stay accepted.
    412 round 35: a signal kill surfaces as a NEGATIVE asyncio return code
    (-9 for SIGKILL), so a negative declared code is refused the same way."""
    async def runner_never_runs(argv, cwd, timeout_s):  # pragma: no cover
        raise AssertionError("the tool must be rejected before it runs")

    async def read(*a, **k):
        raise AssertionError("no reading without a run")

    with pytest.raises(ScannerUnavailable):
        await run_scan(
            ["a.py"], tmp_path,
            tools=[SimpleNamespace(name="x", argv=["x", "a.py"], why="w", ok_exit_codes=[0, 126, 137])],
            read=read, runner=runner_never_runs, budgets=SecurityBudgets(),
        )
    with pytest.raises(ScannerUnavailable):
        await run_scan(
            ["a.py"], tmp_path,
            tools=[SimpleNamespace(name="x", argv=["x", "a.py"], why="w", ok_exit_codes=[0, -9])],
            read=read, runner=runner_never_runs, budgets=SecurityBudgets(),
        )


@pytest.mark.asyncio
async def test_a_refused_plan_still_carries_the_completed_tools(tmp_path: Path) -> None:
    """412 round 33: when tool two is refused by the scoper (an unscoped
    inline operand), the ScanResults of the tool that already completed ride
    the exception instead of silently vanishing from the record."""
    async def runner(argv, cwd, timeout_s):
        return 0, "no findings", ""

    async def read(*a, **k):
        return _reading(examined=True)

    with pytest.raises(ScannerUnavailable) as caught:
        await run_scan(
            ["a.py"], tmp_path,
            tools=[
                SimpleNamespace(name="first", argv=["first", "a.py"], why="w"),
                SimpleNamespace(name="second", argv=["second", "--source=."], why="w"),
            ],
            read=read, runner=runner, budgets=SecurityBudgets(),
        )
    assert [r.tool for r in caught.value.results] == ["first"]


def _reading(*, examined: bool):
    from performer.workflows.security.tooling import ScanReading

    return ScanReading(findings=[], coverage=[{"path": "a.py", "examined": examined, "reason": ""}], summary="")


# --- gate: clamped evidence, non-fatal rows ---------------------------------

def test_scanner_to_findings_clamps_evidence() -> None:
    raw = [{"tool": "bandit", "category": "injection", "description": "d", "file": "a.py", "line": 3, "evidence": "x" * 900}]
    out = scanner_to_findings(raw)
    assert out[0].evidence == "x" * 200


def test_scanner_to_findings_survives_malformed_rows() -> None:
    raw = [
        {"tool": "t", "category": "injection", "description": "d", "file": "a.py", "line": "not-a-number", "evidence": "e"},
        {"tool": "t", "category": "injection", "description": "d", "file": "b.py", "line": 7},
        "a bare string row",
        {"tool": "t", "category": "unknown_category_xyz", "description": "d", "file": "c.py", "line": 1},
    ]
    out = scanner_to_findings(raw)
    assert len(out) == 2, "the malformed row is skipped, not fatal -- and an unparseable line is a malformed row"
    assert out[0].line == 7
    assert out[1].category == "other_insecure_pattern", "an unknown category lands in the fallback"


def test_scanner_finding_with_a_missing_line_is_kept_at_zero() -> None:
    """A MISSING line key is unknown, not malformed: the row survives anchored
    at line 0. Only an unparseable line value drops the row (round 8) -- a
    line-0 finding on any surveyed file would otherwise block the PR."""
    raw = [{"tool": "t", "category": "injection", "description": "d", "file": "a.py"}]
    out = scanner_to_findings(raw)
    assert len(out) == 1 and out[0].line == 0


def test_new_categories_are_high_and_blocking() -> None:
    from performer.workflows.security.gate import routing_for, severity_for

    for category in ("vulnerable_dependency", "supply_chain", "insecure_configuration"):
        assert category in SECURITY_CATEGORIES
        assert severity_for(category) == "high"
    assert routing_for("insecure_configuration") == "architect"
    assert routing_for("vulnerable_dependency") == "implementer"


# --- the workflow short-circuits -------------------------------------------

class FakeGitHub:
    def __init__(self):
        self.reviews = []

    async def post(self, owner, repo, number, *, event, body, comments, token):
        self.reviews.append({"event": event})
        return {"html_url": "https://github.com/o/r/pull/1#x"}


def _fake_scanner_runner():
    async def runner(argv, cwd, timeout_s):
        raise AssertionError("no scanner runs on a short-circuit")

    return runner


def _score(**over):
    base = {"pr_diff": "", "implementation_brief": {"work_kind": "feature"}, "title": "t", "description": "d",
                "pr_url": "https://github.com/o/r/pull/7", "owner_repo": ("o", "r"), "effective_github_token": "tok",
                "backend": "codex", "model": "m", "workflow_env": {}}
    base.update(over)
    return SimpleNamespace(**base)


def _toolkit():
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.toolkit import Toolkit

    return Toolkit(metrics=WorkflowMetrics(), model_call=_refuse, command_runner=_clean, event_sink=lambda e: None, call_limit=4)


async def _refuse(persona, content, max_tokens):  # pragma: no cover
    raise AssertionError("model call on a short-circuit path")


async def _clean(cmd, cwd, timeout_s):
    return 0, "" if "git status" in cmd else "x"


@pytest.mark.asyncio
async def test_truncated_docs_diff_holds_on_the_unnamed_tail_instead_of_not_applicable() -> None:
    """412 round 13: a truncated diff whose visible subset is all
    non-source must hold on the unnamed tail, the way the reviewer workflow
    does -- not advance as not_applicable on the visible subset alone."""
    diff = (
        "diff --git a/docs/usage.md b/docs/usage.md\n"
        "--- a/docs/usage.md\n"
        "+++ b/docs/usage.md\n"
        "@@ -1,2 +1,3 @@\n"
        " # Usage\n"
        "+Call div.\n"
        "\n"
        "[coordinare: diff truncated to 60000 chars; run `gh pr diff <pr_url>` for the full changes]\n"
    )
    result = await security_run(pr_diff=diff)
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert record["unread_files"] == ["<unnamed files beyond the truncated diff>"]
    assert record["covered_files"] == ["docs/usage.md"]
    assert result.report["workflow_metrics"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_empty_diff_is_nothing_to_scan() -> None:
    result = await security_run(pr_diff="")
    record = result.report["security"]
    assert record["verdict"] == "nothing_to_scan"
    assert result.report["workflow_metrics"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_all_deletion_diff_is_not_applicable_before_the_model() -> None:
    """412 round 12: an all-deletion diff short-circuits in code BEFORE the
    tooling model is asked to plan a scan of nothing, and the record reads the
    deletions as covered (they are never on disk) rather than unread.
    412 round 19: the verdict is not_applicable -- files were parsed;
    nothing_to_scan is the zero-parsed verdict."""
    diff = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-old\n"
    )
    result = await security_run(pr_diff=diff)
    record = result.report["security"]
    assert record["verdict"] == "not_applicable"
    assert result.report["workflow_metrics"]["model_calls"] == 0
    assert record["covered_files"] == ["gone.py"]
    assert record["unread_files"] == []


@pytest.mark.asyncio
async def test_non_source_diff_is_not_applicable() -> None:
    diff = (
        "diff --git a/yarn.lock b/yarn.lock\n--- a/yarn.lock\n+++ b/yarn.lock\n@@ -1,2 +1,3 @@\n old\n+new\n"
        "diff --git a/static/logo.png b/static/logo.png\n--- a/static/logo.png\n+++ b/static/logo.png\n@@ -1,1 +1,2 @@\n png\n+bits\n"
    )
    result = await security_run(pr_diff=diff)
    record = result.report["security"]
    assert record["verdict"] == "not_applicable"
    assert result.report["workflow_metrics"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_no_scan_verdicts_never_post_a_review() -> None:
    """412 round 20: the code-decided no-scan verdicts short-circuit before
    the posting branch -- no public 'Bot Security Review: PASSED' may appear
    for a diff that was never scanned."""
    from performer.workflows.security import SecurityWorkflow

    gh = FakeGitHub()
    for diff in (
        "",
        "diff --git a/yarn.lock b/yarn.lock\n--- a/yarn.lock\n+++ b/yarn.lock\n@@ -1,2 +1,3 @@\n old\n+new\n",
    ):
        await SecurityWorkflow(poster=gh.post, scan_runner=_fake_scanner_runner()).run(
            SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), _toolkit(),
        )
    assert gh.reviews == [], "nothing posts for nothing_to_scan / not_applicable"


@pytest.mark.asyncio
async def test_named_phantom_blocks_not_applicable() -> None:
    """412 round 17: a named file whose section was cut before any hunk
    header has no hunks and no full-diff coverage -- it is unread by name,
    and the not_applicable shortcut must not advance past its name."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    async def model_call(persona, content, max_tokens):
        plan = {"tools": [{"name": "scan", "argv": ["scan", FILES_TOKEN], "why": "w"}], "nothing_applies": ""}
        return ModelReply(content=json.dumps(plan), finish_reason="stop")

    async def scan_runner(argv, cwd, timeout_s):
        raise ScannerUnavailable("scan", "control stub: not a real scanner")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    diff = (
        "diff --git a/docs/usage.md b/docs/usage.md\n"
        "--- a/docs/usage.md\n"
        "+++ b/docs/usage.md\n"
        "@@ -1,2 +1,3 @@\n"
        "# Usage\n"
        "+Call div.\n"
        "diff --git a/unread.py b/unread.py\n"
        "--- a/unread.py\n"
        "+++ b/unread.py\n"
    )
    gh = FakeGitHub()
    result = await SecurityWorkflow(poster=gh.post, scan_runner=scan_runner).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), tk,
    )
    record = result.report["security"]
    assert record["verdict"] != "not_applicable"
    assert "unread.py" in record["unread_files"]


@pytest.mark.asyncio
async def test_cut_through_file_with_hunks_still_blocks_not_applicable() -> None:
    """412 round 18: a cut-through file keeps the hunks it had when the cap
    hit -- fully_in_diff=False alone marks it unread, docs-only or not. The
    old hunks-and-not-unread test let the not_applicable shortcut advance a
    partial diff."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    async def model_call(persona, content, max_tokens):
        plan = {"tools": [{"name": "scan", "argv": ["scan", FILES_TOKEN], "why": "w"}], "nothing_applies": ""}
        return ModelReply(content=json.dumps(plan), finish_reason="stop")

    async def scan_runner(argv, cwd, timeout_s):
        raise ScannerUnavailable("scan", "control stub: not a real scanner")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    diff = (
        "diff --git a/docs/usage.md b/docs/usage.md\n"
        "--- a/docs/usage.md\n"
        "+++ b/docs/usage.md\n"
        "@@ -1,2 +1,3 @@\n"
        "# Usage\n"
        "+Call div.\n"
        "\n"
        "[coordinare: diff truncated to 60000 chars — run `gh pr diff <pr_url>` for the full changes; cut off inside: 1 file(s)]\n"
        "coordinare-cut: docs/usage.md\n"
    )
    gh = FakeGitHub()
    result = await SecurityWorkflow(poster=gh.post, scan_runner=scan_runner).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), tk,
    )
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert "docs/usage.md" in record["unread_files"]


@pytest.mark.asyncio
async def test_complete_rename_only_source_entry_holds() -> None:
    """412 round 24 (pinned -- the behaviour is deliberate, not a bug): a
    complete diff's scannable-CLASSIFIED entry whose content never arrives
    holds the round, exactly as round 19 chose. A 100%-similar rename
    introduces nothing new, but the workflow cannot verify that from a diff
    that showed no content, so it may not issue a clean bill either."""
    diff = (
        "diff --git a/src/old.py b/src/new.py\n"
        "similarity index 100%\n"
        "rename from src/old.py\n"
        "rename to src/new.py\n"
    )
    result = await security_run(pr_diff=diff)
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert record["unread_files"] == ["src/new.py"]


@pytest.mark.asyncio
async def test_complete_no_hunk_non_source_entries_are_not_applicable() -> None:
    """412 round 18: no-scannable-path is only a truncation claim when the
    diff actually declares one -- a complete diff's mode-only entry is just
    a non-source change. 412 round 26: the diff is complete, so the entry is
    covered."""
    diff = (
        "diff --git a/README.md b/README.md\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    result = await security_run(pr_diff=diff)
    record = result.report["security"]
    assert record["verdict"] == "not_applicable"
    assert record["covered_files"] == ["README.md"]


@pytest.mark.asyncio
async def test_scan_argv_is_filtered_to_scannable_sources() -> None:
    """412 round 19: the source classifier gates the scanner argv -- a
    lockfile that rode along in the change set is not a scan target."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    async def model_call(persona, content, max_tokens):
        if model_call.seen:
            return ModelReply(content=json.dumps({"findings": []}), finish_reason="stop")
        model_call.seen = True
        plan = {"tools": [{"name": "scan", "argv": ["scan", FILES_TOKEN], "why": "w"}], "nothing_applies": ""}
        return ModelReply(content=json.dumps(plan), finish_reason="stop")

    model_call.seen = False

    seen: list = []

    async def scan_runner(argv, cwd, timeout_s):
        seen.append(list(argv))
        return (0, json.dumps({"results": [], "errors": []}), "")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,1 +1,2 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -1,2 +1,3 @@\n"
        "-old\n"
        "+new\n"
    )
    gh = FakeGitHub()
    await SecurityWorkflow(poster=gh.post, scan_runner=scan_runner).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), tk,
    )
    assert seen, "the scan ran"
    assert all("package-lock.json" not in argv for argv in seen), seen
    assert any("src/app.py" in argv for argv in seen), seen


@pytest.mark.asyncio
async def test_fetch_outage_holds_instead_of_nothing_to_scan() -> None:
    """412 round 2: a fetch outage is not an empty diff -- hold, never advance."""
    result = await security_run(pr_diff="", pr_diff_status="failed")
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert "could not be fetched" in record["hold_reason"]


@pytest.mark.asyncio
async def test_truncated_before_first_header_holds_instead_of_nothing_to_scan() -> None:
    """412 round 5: a truncated diff with no machine names hides an unknown
    unread set -- hold, never a vacuous empty-diff advance."""
    result = await security_run(
        pr_diff="[coordinare: diff truncated to 10 chars — run `gh pr diff <pr_url>` for the full changes]",
    )
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert "truncated" in record["hold_reason"]


@pytest.mark.asyncio
async def test_unread_overflow_holds_instead_of_scanning_a_partial_set() -> None:
    """412 round 6: paths beyond the note budget are declared by count; an
    unknown-size unread set holds, it cannot be scanned as complete."""
    result = await security_run(
        pr_diff=(
            "[coordinare: diff truncated to 10 chars — run `gh pr diff <pr_url>` for the full changes; "
            "unread beyond this point: 5 file(s)]\ncoordinare-unread-overflow: 5\n"
        ),
    )
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert "could not name 5 more unread file(s)" in record["hold_reason"]


def test_git_show_candidates_anchor_to_the_bare_path() -> None:
    """412 round 2: ``git show REV:path`` reads the object at *path* -- the
    revision prefix must not ride along, or the anchor check never matches."""
    from performer.workflows.security import _opened_unchanged

    outcome = SimpleNamespace(records=lambda: [SimpleNamespace(command="git show HEAD:Dockerfile", allowed=True, exit_code=0)])
    assert _opened_unchanged(outcome, []) == ["Dockerfile"]
    # 412 round 6: the revision itself may contain a slash.
    outcome = SimpleNamespace(records=lambda: [SimpleNamespace(command="git show origin/main:Dockerfile", allowed=True, exit_code=0)])
    assert _opened_unchanged(outcome, []) == ["Dockerfile"]
    # 412 round 19: revision-prefix stripping is a ``git show`` rule -- a
    # cat of a path containing ":" anchors the real path, not its tail.
    outcome = SimpleNamespace(records=lambda: [SimpleNamespace(command="cat config/v1:prod.py", allowed=True, exit_code=0)])
    assert _opened_unchanged(outcome, []) == ["config/v1:prod.py"]
    outcome = SimpleNamespace(records=lambda: [SimpleNamespace(command="git show HEAD:config/v1:prod.py", allowed=True, exit_code=0)])
    assert _opened_unchanged(outcome, []) == ["config/v1:prod.py"], "git show still strips the rev"
    # 412 round 20: git flag forms between the command and ``show`` keep the
    # git-show window open -- ``git --no-pager show`` is a common survey read.
    outcome = SimpleNamespace(records=lambda: [SimpleNamespace(command="git --no-pager show HEAD:Dockerfile", allowed=True, exit_code=0)])
    assert _opened_unchanged(outcome, []) == ["Dockerfile"]
    # A flag behind a non-git command opens no git-show window.
    outcome = SimpleNamespace(records=lambda: [SimpleNamespace(command="cat --no-pager Dockerfile", allowed=True, exit_code=0)])
    assert _opened_unchanged(outcome, []) == ["Dockerfile"]


@pytest.mark.asyncio
async def test_planning_prompt_carries_the_tool_inventory() -> None:
    """412 round 2: the inventory is wired into the planning prompt, so the
    model stops naming tools that are not installed."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    seen: dict = {}

    async def model_call(persona, content, max_tokens):
        seen["persona"] = persona
        return ModelReply(content=json.dumps({"tools": [], "nothing_applies": "n"}), finish_reason="stop")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    wf = SecurityWorkflow(poster=None, scan_runner=_fake_scanner_runner())
    await wf._determine_tools(tk, SimpleNamespace(changed_paths=["a.py"]), Path("/tmp/x"), SecurityBudgets())
    assert "Executables available on PATH" in seen["persona"]


@pytest.mark.asyncio
async def test_source_diff_still_runs_the_scan() -> None:
    """A control: a scannable diff does not hit either short-circuit. The
    tooling plan is modelled, the scan runs, and the run holds for other
    reasons (here: the stub scanner is unavailable)."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    model_calls = {"n": 0}

    async def model_call(persona, content, max_tokens):
        model_calls["n"] += 1
        plan = {"tools": [{"name": "scan", "argv": ["scan", FILES_TOKEN], "why": "w"}], "nothing_applies": ""}
        return ModelReply(content=json.dumps(plan), finish_reason="stop")

    async def scan_runner(argv, cwd, timeout_s):
        raise ScannerUnavailable("scan", "control stub: not a real scanner")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    diff = "diff --git a/src/db.py b/src/db.py\n--- a/src/db.py\n+++ b/src/db.py\n@@ -1,1 +1,2 @@\n old\n+new\n"
    gh = FakeGitHub()
    result = await SecurityWorkflow(poster=gh.post, scan_runner=scan_runner).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), tk,
    )
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert model_calls["n"] > 0


@pytest.mark.asyncio
async def test_truncated_diff_scan_never_sees_the_phantom_path() -> None:
    """412 round 14: the synthetic tail is a coverage marker, not a file --
    the argv a scanner receives lists only real changed paths."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    async def model_call(persona, content, max_tokens):
        plan = {"tools": [{"name": "scan", "argv": ["scan", FILES_TOKEN], "why": "w"}], "nothing_applies": ""}
        return ModelReply(content=json.dumps(plan), finish_reason="stop")

    seen: list[list[str]] = []

    async def scan_runner(argv, cwd, timeout_s):
        seen.append(list(argv))
        raise ScannerUnavailable("scan", "control stub: not a real scanner")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    diff = (
        "diff --git a/src/db.py b/src/db.py\n--- a/src/db.py\n+++ b/src/db.py\n@@ -1,1 +1,2 @@\n old\n+new\n\n"
        "[coordinare: diff truncated to 60000 chars; run `gh pr diff <pr_url>` for the full changes]\n"
    )
    gh = FakeGitHub()
    result = await SecurityWorkflow(poster=gh.post, scan_runner=scan_runner).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), tk,
    )
    record = result.report["security"]
    assert record["verdict"] == "env_blocked", "the unnamed tail holds the round"
    assert sorted(record["unread_files"]) == sorted([_UNNAMED_TAIL_PATH, "src/db.py"])
    assert seen, "the scanner ran on the real changed file"
    assert all(_UNNAMED_TAIL_PATH not in argv for argv in seen), (
        "scanners never receive the synthetic tail as a path"
    )


@pytest.mark.asyncio
async def test_scan_argv_never_sees_a_cut_through_deletion() -> None:
    """412 round 20: a truncation cut clears ``deleted`` so a cut-through
    deletion holds as unread -- and ``deleted_before_cut`` keeps it out of
    the scanner argv, because the file does not exist on the worktree."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    async def model_call(persona, content, max_tokens):
        if getattr(model_call, "seen", False):
            return ModelReply(content=json.dumps({"findings": []}), finish_reason="stop")
        model_call.seen = True
        plan = {"tools": [{"name": "scan", "argv": ["scan", FILES_TOKEN], "why": "w"}], "nothing_applies": ""}
        return ModelReply(content=json.dumps(plan), finish_reason="stop")

    seen: list[list[str]] = []

    async def scan_runner(argv, cwd, timeout_s):
        seen.append(list(argv))
        return (0, json.dumps({"results": [], "errors": []}), "")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    diff = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,1 +1,2 @@\n-old\n+new\n\n"
        "diff --git a/src/gone.py b/src/gone.py\n--- a/src/gone.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-gone\n"
        "[coordinare: diff truncated to 60000 chars; run `gh pr diff <pr_url>` for the full changes]\n"
        "coordinare-cut: src/gone.py\n"
    )
    gh = FakeGitHub()
    result = await SecurityWorkflow(poster=gh.post, scan_runner=scan_runner).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), tk,
    )
    record = result.report["security"]
    assert seen, "the scan ran on the real changed file"
    assert all("src/gone.py" not in argv for argv in seen), (
        "scanners never receive a cut-through deletion -- the file is not on disk"
    )
    assert record["verdict"] == "env_blocked", "the cut-through deletion still holds as unread"
    assert "src/gone.py" in record["unread_files"]


@pytest.mark.asyncio
async def test_named_phantom_only_diff_holds_without_scanning() -> None:
    """412 round 15: when the truncation cap hid every file, the note names
    phantoms with no hunks -- nothing exists on disk, so the record holds on
    the named set instead of passing nothing_to_scan."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=_refuse, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    diff = (
        "[coordinare: diff truncated to 60000 chars; run `gh pr diff <pr_url>` for the full changes]\n"
        "coordinare-cut: src/gone.py\n"
    )
    gh = FakeGitHub()
    result = await SecurityWorkflow(poster=gh.post, scan_runner=_fake_scanner_runner()).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), tk,
    )
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert record["unread_files"] == ["src/gone.py"]


@pytest.mark.asyncio
async def test_truncated_hold_reports_per_file_coverage() -> None:
    """412 round 34: the truncated no-target hold derives coverage per file
    exactly like the gate -- a visible file that was fully in the diff (or
    opened, or deleted) is covered; only the entries the truncation hid are
    unread, so the hold report does not claim it never saw content it had."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=_refuse, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    diff = (
        "diff --git a/docs/usage.md b/docs/usage.md\n--- a/docs/usage.md\n+++ b/docs/usage.md\n@@ -1,1 +1,2 @@\n-old\n+new\n\n"
        "[coordinare: diff truncated to 60000 chars; run `gh pr diff <pr_url>` for the full changes]\n"
        "coordinare-cut: src/gone.py\n"
    )
    gh = FakeGitHub()
    result = await SecurityWorkflow(poster=gh.post, scan_runner=_fake_scanner_runner()).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), tk,
    )
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert record["covered_files"] == ["docs/usage.md"]
    assert record["unread_files"] == ["src/gone.py"]


@pytest.mark.asyncio
async def test_truncated_named_source_holds_even_when_others_are_scannable() -> None:
    """412 round 21: a truncated diff that names scannable files it never
    showed holds even when visible sources keep the scanner busy -- the
    coverage pass can OPEN those files, but open is not scanned."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=_refuse, command_runner=_clean, event_sink=lambda e: None, call_limit=4)
    diff = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,1 +1,2 @@\n-old\n+new\n\n"
        "[coordinare: diff truncated to 60000 chars; run `gh pr diff <pr_url>` for the full changes]\n"
        "coordinare-cut: src/hidden.py\n"
    )
    gh = FakeGitHub()
    result = await SecurityWorkflow(poster=gh.post, scan_runner=_fake_scanner_runner()).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(pr_diff=diff), tk,
    )
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert record["unread_files"] == ["src/hidden.py"]
    assert gh.reviews == [], "nothing posts for a code-decided hold"


def _findings_reply():
    from performer.workflows.budget import ModelReply

    return ModelReply(content=json.dumps({"findings": []}), finish_reason="stop")


@pytest.mark.asyncio
async def test_path_list_overflow_holds_the_empty_diff() -> None:
    """412 round 39: the dispatch caps the changed-path list. An overflow
    means paths were cut, so the empty-diff classification would judge a
    partial list -- a scannable path could hide in the tail and read as
    not_applicable. The overflow holds even when the delivered partial list
    looks entirely non-scannable."""
    result = await security_run(pr_diff="", pr_changed_paths=["package-lock.json"], pr_changed_paths_overflow=True)
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert record["hold_reason"]
    assert record["covered_files"] == []


@pytest.mark.asyncio
async def test_sanitized_empty_diff_with_non_scannable_paths_is_not_applicable() -> None:
    """412 round 25: sanitization stripped every section (a binary-only
    change set), but the GitHub-side changed-path list survives — all paths
    non-scannable means nothing a static scanner can read, which is
    not_applicable, not nothing_to_scan."""
    result = await security_run(pr_diff="", pr_changed_paths=["package-lock.json", "docs/img.png"])
    record = result.report["security"]
    assert record["verdict"] == "not_applicable"
    assert record["covered_files"] == ["package-lock.json", "docs/img.png"]


@pytest.mark.asyncio
async def test_sanitized_empty_diff_with_scannable_paths_holds() -> None:
    """412 round 25: a scannable-classified path named in the change set
    while the delivered diff is empty means content was lost between fetch
    and intake -- hold instead of advancing on a change never seen."""
    result = await security_run(pr_diff="", pr_changed_paths=["src/app.py", "package-lock.json"])
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert record["unread_files"] == ["src/app.py"]


@pytest.mark.asyncio
async def test_a_sanitizer_omitted_path_does_not_hold_the_empty_diff() -> None:
    """412 round 31: the sanitizer deliberately drops ``.codex/``-style
    sections, so an empty diff carrying only those paths is COMPLETE -- the
    raw scannable-looking path is explained by the omission note, and the
    change advances with the note instead of holding."""
    result = await security_run(pr_diff="", pr_changed_paths=[".codex/tool.py"])
    record = result.report["security"]
    assert record["verdict"] == "not_applicable"
    assert record["covered_files"] == [".codex/tool.py"]

    # A real scannable path alongside the noise still holds on the real one.
    result = await security_run(pr_diff="", pr_changed_paths=[".codex/tool.py", "src/app.py"])
    record = result.report["security"]
    assert record["verdict"] == "env_blocked"
    assert record["unread_files"] == ["src/app.py"]


def test_baseline_findings_render_as_a_non_blocking_section() -> None:
    """412 round 25: unanchored tool findings ride the GitHub review body as
    a pre-existing section -- reported, never dropped, and never styled with
    the blocking reason."""
    from performer.workflows.security.models import SecurityFinding
    from performer.workflows.security.post import build_security_review

    baseline = [SecurityFinding(
        path="src/config.py", line=0, category="secret", problem="hardcoded credential",
        why_blocking="unused here", evidence="", origin="rule", severity="medium",
        routing="implementer", introduced_by="src/config.py", tool="gitleaks",
    )]
    _event, body, inline = build_security_review([], [], [], "header", baseline)
    assert "Baseline scanner findings (pre-existing, not introduced by this PR):" in body
    assert "hardcoded credential" in body
    assert "Why blocking" not in body.split("Baseline scanner findings")[-1]
    assert inline == []


def test_the_baseline_section_render_is_bounded() -> None:
    """412 round 29: the baseline bucket is uncapped in the record, but the
    GitHub review body is not -- a scan-heavy PR that renders every line
    blows past the body limit, the post fails, and a non-blocking result
    becomes env_blocked. The section is bounded by total characters and
    summarizes the remainder."""
    from performer.workflows.security.models import SecurityFinding
    from performer.workflows.security.post import MAX_BASELINE_RENDER_CHARS, build_security_review

    def finding(n: int) -> SecurityFinding:
        return SecurityFinding(
            path="src/config.py", line=n, category="secret",
            problem="hardcoded credential " + "x" * 400,
            why_blocking="unused here", evidence="", origin="rule", severity="medium",
            routing="implementer", introduced_by="src/config.py", tool="gitleaks",
        )

    baseline = [finding(i) for i in range(50)]
    _event, body, _inline = build_security_review([], [], [], "header", baseline)
    section = body.split("Baseline scanner findings")[-1]
    assert len(section) < MAX_BASELINE_RENDER_CHARS + 200, "the rendered section must stay postable"
    assert "more pre-existing findings omitted" in section
    rendered = section.count("- **medium secret**")
    omitted = int(section.split("(+")[1].split(" more")[0])
    assert rendered + omitted == 50, "the summary accounts for every baseline finding"


async def security_run(*, pr_diff: str, pr_diff_status: str = "", pr_changed_paths: list[str] | None = None, pr_changed_paths_overflow: bool = False):
    """Drive SecurityWorkflow.run with a short-circuit-bound score."""
    from performer.workflows.security import SecurityWorkflow

    gh = FakeGitHub()
    score_over = {"pr_diff": pr_diff, "pr_diff_status": pr_diff_status, "pr_changed_paths_overflow": pr_changed_paths_overflow}
    if pr_changed_paths is not None:
        score_over["pr_changed_paths"] = pr_changed_paths
    return await SecurityWorkflow(poster=gh.post, scan_runner=_fake_scanner_runner()).run(
        SimpleNamespace(path=Path("/tmp/x")), _score(**score_over), _toolkit(),
    )
