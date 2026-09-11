"""Spec 170 FR-008 to FR-012, FR-019: every gate rule is a pure function with its
own test. Each test names the mutation that breaks it (data-model "Rule
predicates"); the mutation table in the PR records each one applied in the real tree."""
from __future__ import annotations

from performer.workflows.reviewer.diffparse import diff_lines
from performer.workflows.reviewer.models import ChangedFile
from performer.workflows.security.gate import (
    anchor_ok_security,
    apply_downgrade,
    merge_scanner_findings,
    normalise_tool_path,
    routing_for,
    run_gate,
    scanner_to_findings,
    severity_for,
    split_blocking,
    verdict,
)
from performer.workflows.security.models import SECURITY_CATEGORIES

from tests.unit.workflows.security._fixtures import (
    BANDIT_MD5,
    DIFF,
    SEMGREP_SECRET,
    changed_files,
    finding,
)

LINES = diff_lines(DIFF)


# severity_for: mutation = return "high" for weak_crypto, or "medium" for hardcoded_secret
def test_severity_comes_from_the_category_table():
    assert severity_for("hardcoded_secret") == "critical"
    for c in ("injection", "broken_authorization", "insecure_deserialization", "path_traversal", "ssrf"):
        assert severity_for(c) == "high", c
    for c in ("weak_crypto", "missing_hardening", "information_leak", "other_insecure_pattern"):
        assert severity_for(c) == "medium", c
    assert severity_for("made_up") == "medium", "an unknown category is advisory"


# routing_for: mutation = route broken_authorization to the implementer
def test_routing_comes_from_the_category():
    assert routing_for("broken_authorization") == "architect"
    for c in ("injection", "hardcoded_secret", "weak_crypto", "made_up"):
        assert routing_for(c) == "implementer", c


# apply_downgrade: mutation = downgrade a scanner finding; downgrade without a reason
def test_downgrade_needs_a_model_finding_in_a_blocking_category_with_a_reason():
    down = apply_downgrade(finding(downgrade_reason="the value is cast to int first"))
    assert (down.severity, down.downgraded) == ("medium", True)
    assert apply_downgrade(finding()).downgraded is False, "no reason, no downgrade"
    scanner = finding(origin="rule", tool="semgrep", evidence="", downgrade_reason="false positive")
    assert apply_downgrade(scanner).severity == "high" and apply_downgrade(scanner).downgraded is False, "tools are never downgraded"
    advisory = apply_downgrade(finding(category="weak_crypto", severity="medium", downgrade_reason="x"))
    assert advisory.downgraded is False, "only a blocking category can be downgraded"


# anchor_ok_security: mutations = drop the introduced_by clause; accept an unsurveyed unchanged path; drop the evidence clause
def test_anchor_widens_to_surveyed_unchanged_files_but_stays_tied_to_the_pr():
    files = changed_files()
    assert anchor_ok_security(finding(), files, [], LINES, [])
    assert not anchor_ok_security(finding(line=40), files, [], LINES, []), "outside the hunks and not surveyed"
    sink = finding(path="src/sink.py", line=2, evidence="return db.execute(sql)")
    assert anchor_ok_security(sink, files, ["src/sink.py"], LINES, ["    return db.execute(sql)"]), "an opened unchanged file anchors a sink"
    assert not anchor_ok_security(sink, files, [], LINES, ["    return db.execute(sql)"]), "an unopened unchanged file never anchors"
    assert not anchor_ok_security(finding(introduced_by="src/other.py"), files, [], LINES, []), "introduced_by must be a changed file"
    assert not anchor_ok_security(finding(evidence="made up"), files, [], LINES, []), "hallucinated evidence drops the finding"


# scanner_to_findings: mutation = ignore the reading's category (use the fallback for everything)
def test_scanner_findings_are_rule_findings_scored_by_their_category():
    out = scanner_to_findings([SEMGREP_SECRET, BANDIT_MD5])
    assert [(f.tool, f.origin, f.severity, f.category, f.line, f.evidence, f.introduced_by) for f in out] == [
        ("semgrep", "rule", "critical", "hardcoded_secret", 6, "", "src/db.py"),
        ("bandit", "rule", "medium", "weak_crypto", 8, "", "src/db.py"),
    ]
    assert out[0].routing == "implementer"


# scanner_to_findings category guard: mutation = keep whatever the reading said
def test_a_category_the_reading_invented_lands_in_the_fallback():
    """SecurityFinding.category is a plain string, so this is the only guard.

    The reading names the kind of issue, and a model naming things is a model
    that can name one that does not exist -- "sql_injection" for "injection",
    or a CWE it decided to pass through. Nothing downstream validates it:
    the field is an unconstrained str whose comment merely claims it is one of
    SECURITY_CATEGORIES. Two things depend on that claim being true: the
    severity comes from a table keyed by category, and the merge that collapses
    a model finding into a tool's uses (path, line, category) -- so an invented
    name silently means "advisory, and duplicated at the same anchor".

    This survived a mutation run with the guard disabled, which is how it got
    written.
    """
    invented = {**SEMGREP_SECRET, "category": "sql_injection"}
    assert scanner_to_findings([invented])[0].category == "other_insecure_pattern"

    passed_through = {**SEMGREP_SECRET, "category": "CWE-89"}
    assert scanner_to_findings([passed_through])[0].category == "other_insecure_pattern"

    empty = {**SEMGREP_SECRET, "category": ""}
    assert scanner_to_findings([empty])[0].category == "other_insecure_pattern"

    assert all(f.category in set(SECURITY_CATEGORIES) for f in scanner_to_findings([invented, passed_through, empty, SEMGREP_SECRET]))


# merge_scanner_findings: mutations = keep the model's severity; skip the append
def test_a_model_finding_at_a_scanner_anchor_collapses_into_the_tools():
    # the collapse key is (path, line, category), so the reading has to call
    # this what the model called it -- which is the point: the category is the
    # reading's judgement, and agreeing is what makes the two findings one.
    scanner = scanner_to_findings([{**SEMGREP_SECRET, "line": 7, "category": "injection"}])
    merged = merge_scanner_findings([finding(), finding(line=8)], scanner)
    assert [(f.tool, f.line, f.severity) for f in merged] == [("model", 8, "high"), ("semgrep", 7, "high")]
    assert merge_scanner_findings([], scanner) == scanner


# split_blocking: mutation = treat high as advisory
def test_blocking_is_critical_or_high():
    b, a = split_blocking([finding(severity="critical"), finding(severity="high"), finding(severity="medium"), finding(severity="low")])
    assert [f.severity for f in b] == ["critical", "high"] and [f.severity for f in a] == ["medium", "low"]


# verdict: mutations = pass with blocking findings; pass without coverage
def test_verdict_is_derived_by_code():
    assert verdict([finding()], coverage_ok=True) == "security_failed"
    assert verdict([finding()], coverage_ok=False) == "security_failed"
    assert verdict([], coverage_ok=True) == "security_passed"
    assert verdict([], coverage_ok=False) == "env_blocked"


def test_run_gate_end_to_end_orders_the_rules():
    files = changed_files()
    model = [finding(), finding(evidence="hallucinated"), finding(category="weak_crypto", severity="medium", line=8, evidence="lookup(conn):")]
    result = run_gate(model, [SEMGREP_SECRET], changed_files=files, diff_lines=LINES, survey_lines=[], surveyed_files=[], truncated=False, coverage_pass_ran=False)
    assert [f.evidence for f in result.dropped] == ["hallucinated"]
    assert [(f.tool, f.category) for f in result.blocking] == [("model", "injection"), ("semgrep", "hardcoded_secret")]
    assert [f.category for f in result.advisory] == ["weak_crypto"] and result.verdict == "security_failed"
    clean = run_gate([], [], changed_files=files, diff_lines=LINES, survey_lines=[], surveyed_files=[], truncated=False, coverage_pass_ran=False)
    assert clean.verdict == "security_passed" and clean.covered_files == ["src/db.py", "tests/test_db.py"]
    unread = [*files, ChangedFile(path="src/extra.py", hunks=[], fully_in_diff=False)]
    hold = run_gate([], [], changed_files=unread, diff_lines=LINES, survey_lines=[], surveyed_files=[], truncated=True, coverage_pass_ran=True)
    assert hold.verdict == "env_blocked" and hold.unread_files == ["src/extra.py"]


# normalise_tool_path: mutation = return the path unchanged
def test_tool_paths_match_the_diff_paths():
    """Live round: bandit reports ./app/web.py; the diff, the dedup key and the inline comment need app/web.py."""
    assert normalise_tool_path("./app/web.py") == "app/web.py"
    assert normalise_tool_path("app/web.py") == "app/web.py"
    assert normalise_tool_path("././x.py") == "x.py"
    out = scanner_to_findings([{**BANDIT_MD5, "file": "./src/db.py"}])
    assert (out[0].path, out[0].introduced_by) == ("src/db.py", "src/db.py")
    merged = merge_scanner_findings([finding(line=8, category="weak_crypto", severity="medium")], out)
    assert [f.tool for f in merged] == ["bandit"], "the model finding at the same anchor collapses into the tool's once paths agree"


# run_gate caps: mutation = slice the merged list (tool findings can then be dropped)
def test_tool_findings_are_never_sliced_out_by_the_survivor_cap():
    """Review finding: 50 advisory model findings plus 15 tool findings must keep every tool finding."""
    from tests.unit.workflows.security._fixtures import SEMGREP_SECRET

    model = [finding(line=7, category="weak_crypto", severity="medium", evidence='query = "SELECT * FROM users WHERE id = " + user_id') for _ in range(50)]
    tools = [{**SEMGREP_SECRET, "line": 100 + i} for i in range(15)]
    result = run_gate(model, tools, changed_files=changed_files(), diff_lines=LINES, survey_lines=[], surveyed_files=[], truncated=False, coverage_pass_ran=False)
    assert len(result.blocking) == 15 and all(f.tool == "semgrep" for f in result.blocking)
    assert len(result.advisory) == 30, "the model's survivors are capped at the findings cap"




# scanner cap ordering: mutation = slice before ordering (the positional slice)
def test_the_scanner_bound_can_only_drop_findings_that_were_not_going_to_fail():
    """A repository with more than 200 scanner findings must not pass on ordering.

    The 200 bound is documented and intended (data-model: "at most 200"). What
    was not intended is that it sliced positionally: findings arrive in tool
    order then reading order, so a repository whose blocking findings happened
    to land after position 200 lost every one of them and the round passed.
    Measured before the fix, 240 advisory findings followed by 10 SQL
    injections yielded 0 of 10 blocking findings surviving, and a verdict of
    security_passed.

    Ordering blocking first means the bound can only ever drop findings that
    were not going to fail the round. This is not the anchor rule of FR-011 --
    that one is about dropping, and is tested above -- it is the bound.
    """
    advisory = [{"tool": "semgrep", "category": "weak_crypto", "description": f"w{i}", "file": "src/db.py", "line": i}
                for i in range(240)]
    blocking_raw = [{"tool": "semgrep", "category": "injection", "description": f"sqli{i}", "file": "src/db.py", "line": 1000 + i}
                    for i in range(10)]

    result = run_gate(
        [], advisory + blocking_raw, changed_files=changed_files(), diff_lines=LINES,
        survey_lines=[], surveyed_files=[], truncated=False, coverage_pass_ran=False,
    )
    assert len(result.blocking) == 10, "a blocking finding was dropped by the bound, not by a rule"
    assert result.verdict == "security_failed", "ten SQL injections passed because of list position"
    assert len(result.blocking) + len(result.advisory) == 200, "the documented bound still holds"
