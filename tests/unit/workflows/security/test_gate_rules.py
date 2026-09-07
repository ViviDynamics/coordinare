"""Spec 170 FR-008 to FR-012, FR-019: every gate rule is a pure function with its
own test. Each test names the mutation that breaks it (data-model "Rule
predicates"); the mutation table in the PR records each one applied in the real tree."""
from __future__ import annotations

from performer.workflows.reviewer.diffparse import diff_lines
from performer.workflows.reviewer.models import ChangedFile
from performer.workflows.security.gate import (
    anchor_ok_security,
    apply_downgrade,
    map_scanner_category,
    merge_scanner_findings,
    normalise_tool_path,
    routing_for,
    run_gate,
    scanner_to_findings,
    severity_for,
    split_blocking,
    verdict,
)

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


# map_scanner_category: mutation = map 798 to other_insecure_pattern
def test_scanner_categories_map_through_the_cwe_table():
    assert map_scanner_category("798") == "hardcoded_secret"
    assert map_scanner_category("CWE-89") == "injection"
    assert map_scanner_category("22") == "path_traversal"
    assert map_scanner_category("hashlib") == "weak_crypto", "a rule id maps by keyword"
    assert map_scanner_category("") == "other_insecure_pattern"
    assert map_scanner_category("B798") == "other_insecure_pattern", "digits inside a tool code are not a CWE"


# scanner_to_findings: mutation = drop the tool's severity (use the table)
def test_scanner_findings_are_rule_findings_with_the_tools_severity():
    out = scanner_to_findings([SEMGREP_SECRET, BANDIT_MD5])
    assert [(f.tool, f.origin, f.severity, f.category, f.line, f.evidence, f.introduced_by) for f in out] == [
        ("semgrep", "rule", "critical", "hardcoded_secret", 6, "", "src/db.py"),
        ("bandit", "rule", "medium", "weak_crypto", 8, "", "src/db.py"),
    ]
    assert out[0].routing == "implementer"


# merge_scanner_findings: mutations = keep the model's severity; skip the append
def test_a_model_finding_at_a_scanner_anchor_collapses_into_the_tools():
    scanner = scanner_to_findings([{**SEMGREP_SECRET, "line": 7, "category": "89", "severity": "critical"}])
    merged = merge_scanner_findings([finding(), finding(line=8)], scanner)
    assert [(f.tool, f.line, f.severity) for f in merged] == [("model", 8, "high"), ("semgrep", 7, "critical")]
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


# map_scanner_category keyword fallback: mutation = drop the keyword table (everything without a CWE is other_insecure_pattern)
def test_rule_ids_without_cwe_metadata_map_by_keyword():
    """Live round: semgrep's tainted-sql-string rules carry no CWE and must still be injection so they collapse into the model's finding."""
    assert map_scanner_category("python.flask.security.injection.tainted-sql-string.tainted-sql-string") == "injection"
    assert map_scanner_category("python.sqlalchemy.security.sqlalchemy-execute-raw-query") == "injection"
    assert map_scanner_category("generic.secrets.security.detected-aws-access-key") == "hardcoded_secret"
    assert map_scanner_category("python.lang.security.deserialization.pickle") == "insecure_deserialization"
    assert map_scanner_category("hashlib") == "weak_crypto"
    assert map_scanner_category("B608") == "other_insecure_pattern", "a bare bandit id carries no category word"
    assert map_scanner_category("B798") == "other_insecure_pattern", "digits inside an id are not a CWE number"
    assert map_scanner_category("CWE-1798") == "other_insecure_pattern"


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


# scanner_to_findings category fallback: mutation = drop the description fallback
def test_a_cwe_that_maps_nowhere_defers_to_the_rule_ids_words():
    """Live round: semgrep's tainted-sql-string rules carry CWE-915 and CWE-704; the rule id says sql injection."""
    raw = {"severity": "critical", "category": "915", "description": "semgrep:python.flask.security.injection.tainted-sql-string.tainted-sql-string", "file": "app/web.py", "line": 11, "routing": "implementer"}
    assert scanner_to_findings([raw])[0].category == "injection"
    assert scanner_to_findings([{**raw, "category": "798"}])[0].category == "hardcoded_secret", "a CWE that maps wins over the words"
    assert scanner_to_findings([{**raw, "category": "915", "description": "semgrep:rules.something.odd"}])[0].category == "other_insecure_pattern"
