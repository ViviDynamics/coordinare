"""Unit tests for the security workflow models (spec 170).

Strategy: validate bounds, validators, and schema rejection of forbidden keys.
Every SecurityFinding and SecurityRecord round-trips through model_dump.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from performer.workflows.security.models import (
    BLOCKING,
    CATEGORY_TABLE,
    ROUTING,
    SECURITY_CATEGORIES,
    ScanResult,
    SecurityFinding,
    SecurityRecord,
    model_security_findings_schema,
)

SPEC_ROOT = Path(__file__).resolve().parents[4] / "specs" / "170-security-workflow"
SCHEMA_PATH = SPEC_ROOT / "contracts" / "security-record.schema.json"


class TestSecurityCategories:
    """Test the security category constants."""

    def test_security_categories_expected(self):
        assert SECURITY_CATEGORIES == (
            "injection",
            "broken_authorization",
            "hardcoded_secret",
            "insecure_deserialization",
            "path_traversal",
            "ssrf",
            "weak_crypto",
            "missing_hardening",
            "information_leak",
            "other_insecure_pattern",
        )

    def test_category_table_matches_spec(self):
        assert CATEGORY_TABLE == {
            "hardcoded_secret": "critical",
            "injection": "high",
            "broken_authorization": "high",
            "insecure_deserialization": "high",
            "path_traversal": "high",
            "ssrf": "high",
            "weak_crypto": "medium",
            "missing_hardening": "medium",
            "information_leak": "medium",
            "other_insecure_pattern": "medium",
        }

    def test_routing_table_only_architect(self):
        assert ROUTING == {"broken_authorization": "architect"}

    def test_blocking_set(self):
        assert frozenset({"critical", "high"}) == BLOCKING



class TestSecurityFinding:
    """Test SecurityFinding model and validators."""

    def test_finding_valid_minimal(self):
        f = SecurityFinding(
            path="app.py",
            line=10,
            category="injection",
            problem="SQL injection",
            why_blocking="Attacker can bypass database",
            evidence="query = 'SELECT * FROM users WHERE id=' + user_id",
            origin="model",
            severity="high",
            routing="implementer",
            introduced_by="app.py",
            tool="model",
        )
        assert f.line == 10
        assert f.downgraded is False
        assert f.downgrade_reason == ""

    def test_finding_evidence_empty_only_for_rule_origin(self):
        """Evidence empty only when origin == 'rule' (scanner findings)."""
        # Model finding with empty evidence should fail
        with pytest.raises(ValueError, match="evidence must be non-empty"):
            SecurityFinding(
                path="app.py",
                line=10,
                category="injection",
                problem="SQL injection",
                why_blocking="Attacker can bypass",
                evidence="",
                origin="model",
                severity="high",
                routing="implementer",
                introduced_by="app.py",
                tool="model",
            )

    def test_finding_evidence_empty_for_rule(self):
        """Rule findings (scanner) can have empty evidence."""
        f = SecurityFinding(
            path="app.py",
            line=10,
            category="hardcoded_secret",
            problem="Hardcoded API key",
            why_blocking="Secret exposed",
            evidence="",
            origin="rule",
            severity="critical",
            routing="implementer",
            introduced_by="app.py",
            tool="semgrep",
        )
        assert f.evidence == ""

    def test_finding_downgraded_only_for_model_tool(self):
        """downgraded=True only when tool == 'model'."""
        # Scanner finding with downgraded=True should fail
        with pytest.raises(ValueError, match="downgraded can only be true"):
            SecurityFinding(
                path="app.py",
                line=10,
                category="injection",
                problem="SQL injection",
                why_blocking="Attacker can bypass",
                evidence="",
                origin="rule",
                severity="high",
                routing="implementer",
                introduced_by="app.py",
                tool="semgrep",
                downgraded=True,
            )

    def test_finding_with_downgrade(self):
        """Model finding can be downgraded with reason."""
        f = SecurityFinding(
            path="app.py",
            line=10,
            category="injection",
            problem="SQL injection",
            why_blocking="Attacker can bypass",
            evidence="query = ...",
            origin="model",
            severity="high",
            routing="implementer",
            introduced_by="app.py",
            tool="model",
            downgraded=True,
            downgrade_reason="Context shows parameterized query at runtime",
        )
        assert f.downgraded is True
        assert f.downgrade_reason == "Context shows parameterized query at runtime"

    def test_finding_line_minimum_zero(self):
        f = SecurityFinding(
            path="app.py",
            line=0,
            category="injection",
            problem="Issue",
            why_blocking="Fix it",
            evidence="code",
            origin="model",
            severity="medium",
            routing="implementer",
            introduced_by="app.py",
            tool="model",
        )
        assert f.line == 0

    def test_finding_category_bounded(self):
        """Category is a string, 1-64 chars."""
        with pytest.raises(ValueError):
            SecurityFinding(
                path="app.py",
                line=10,
                category="",
                problem="Issue",
                why_blocking="Fix it",
                evidence="code",
                origin="model",
                severity="medium",
                routing="implementer",
                introduced_by="app.py",
                tool="model",
            )

    def test_finding_problem_bounded(self):
        """problem: 1-500 chars."""
        with pytest.raises(ValueError):
            SecurityFinding(
                path="app.py",
                line=10,
                category="injection",
                problem="",
                why_blocking="Fix it",
                evidence="code",
                origin="model",
                severity="medium",
                routing="implementer",
                introduced_by="app.py",
                tool="model",
            )

    def test_finding_evidence_bounded(self):
        """evidence: <=200 chars."""
        with pytest.raises(ValueError):
            SecurityFinding(
                path="app.py",
                line=10,
                category="injection",
                problem="Issue",
                why_blocking="Fix it",
                evidence="x" * 201,
                origin="model",
                severity="medium",
                routing="implementer",
                introduced_by="app.py",
                tool="model",
            )

    def test_finding_downgrade_reason_bounded(self):
        """downgrade_reason: <=300 chars."""
        with pytest.raises(ValueError):
            SecurityFinding(
                path="app.py",
                line=10,
                category="injection",
                problem="Issue",
                why_blocking="Fix it",
                evidence="code",
                origin="model",
                severity="high",
                routing="implementer",
                introduced_by="app.py",
                tool="model",
                downgrade_reason="x" * 301,
            )


class TestScanResult:
    """Test ScanResult model."""

    def test_scan_result_valid(self):
        r = ScanResult(
            tool="semgrep",
            command="semgrep --config auto --json app.py",
            exit_code=1,
            finding_count=2,
            duration_ms=1500,
        )
        assert r.tool == "semgrep"
        assert r.finding_count == 2

    def test_scan_result_no_error_default(self):
        r = ScanResult(
            tool="bandit",
            command="bandit -f json -r .",
            exit_code=0,
            finding_count=0,
            duration_ms=500,
        )
        assert r.error is None

    def test_scan_result_with_error(self):
        r = ScanResult(
            tool="semgrep",
            command="semgrep --config auto --json app.py",
            exit_code=None,
            finding_count=0,
            duration_ms=0,
            error="binary not found",
        )
        assert r.error == "binary not found"

    def test_scan_result_finding_count_nonnegative(self):
        with pytest.raises(ValueError):
            ScanResult(
                tool="semgrep",
                command="cmd",
                exit_code=0,
                finding_count=-1,
                duration_ms=0,
            )


class TestSecurityRecord:
    """Test SecurityRecord model."""

    def test_record_valid_minimal(self):
        from performer.workflows.reviewer.models import ChangedFile, Hunk

        cf = ChangedFile(
            path="app.py", hunks=[Hunk(header="@@", start_line=1, end_line=5)], fully_in_diff=True
        )
        sr = ScanResult(
            tool="semgrep", command="semgrep --config auto --json app.py", exit_code=0, finding_count=0, duration_ms=100
        )
        rec = SecurityRecord(
            changed_files=[cf],
            diff_truncated=False,
            scan=[sr],
            verdict="security_passed",
            covered_files=["app.py"],
        )
        assert rec.verdict == "security_passed"
        assert len(rec.scan) == 1

    def test_record_blocking_advisory_lists(self):
        from performer.workflows.reviewer.models import ChangedFile, Hunk

        cf = ChangedFile(path="app.py", hunks=[Hunk(header="@@", start_line=1, end_line=5)], fully_in_diff=True)
        sr = ScanResult(tool="semgrep", command="cmd", exit_code=0, finding_count=0, duration_ms=100)
        f = SecurityFinding(
            path="app.py",
            line=10,
            category="injection",
            problem="SQL injection",
            why_blocking="Attacker can bypass",
            evidence="query = ...",
            origin="rule",
            severity="high",
            routing="implementer",
            introduced_by="app.py",
            tool="semgrep",
        )
        rec = SecurityRecord(
            changed_files=[cf],
            diff_truncated=False,
            scan=[sr],
            blocking=[f],
            verdict="security_failed",
            covered_files=["app.py"],
        )
        assert len(rec.blocking) == 1
        assert rec.verdict == "security_failed"

    def test_record_finding_bounds(self):
        """findings_before_gate, findings_dropped, findings_after_anchor_recheck: max 30."""
        from performer.workflows.reviewer.models import ChangedFile, Hunk

        cf = ChangedFile(path="app.py", hunks=[Hunk(header="@@", start_line=1, end_line=5)], fully_in_diff=True)
        sr = ScanResult(tool="semgrep", command="cmd", exit_code=0, finding_count=0, duration_ms=100)
        f = SecurityFinding(
            path="app.py",
            line=10,
            category="injection",
            problem="Issue",
            why_blocking="Fix it",
            evidence="code",
            origin="model",
            severity="medium",
            routing="implementer",
            introduced_by="app.py",
            tool="model",
        )
        findings = [f] * 31
        with pytest.raises(ValueError):
            SecurityRecord(
                changed_files=[cf],
                diff_truncated=False,
                scan=[sr],
                findings_before_gate=findings,
                verdict="security_passed",
                covered_files=["app.py"],
            )

    def test_record_blocking_advisory_bounds(self):
        """blocking and advisory: max 230 (30 model survivors plus up to 200 tool findings)."""
        from performer.workflows.reviewer.models import ChangedFile, Hunk

        cf = ChangedFile(path="app.py", hunks=[Hunk(header="@@", start_line=1, end_line=5)], fully_in_diff=True)
        sr = ScanResult(tool="semgrep", command="cmd", exit_code=0, finding_count=0, duration_ms=100)
        f = SecurityFinding(
            path="app.py",
            line=10,
            category="injection",
            problem="Issue",
            why_blocking="Fix it",
            evidence="code",
            origin="model",
            severity="medium",
            routing="implementer",
            introduced_by="app.py",
            tool="model",
        )
        findings = [f] * 231
        with pytest.raises(ValueError):
            SecurityRecord(
                changed_files=[cf],
                diff_truncated=False,
                scan=[sr],
                blocking=findings,
                verdict="security_failed",
                covered_files=["app.py"],
            )

    def test_record_scanner_findings_bound(self):
        """scanner_findings: max 200."""
        from performer.workflows.reviewer.models import ChangedFile, Hunk

        cf = ChangedFile(path="app.py", hunks=[Hunk(header="@@", start_line=1, end_line=5)], fully_in_diff=True)
        sr = ScanResult(tool="semgrep", command="cmd", exit_code=0, finding_count=0, duration_ms=100)
        f = SecurityFinding(
            path="app.py",
            line=10,
            category="injection",
            problem="Issue",
            why_blocking="Fix it",
            evidence="",
            origin="rule",
            severity="medium",
            routing="implementer",
            introduced_by="app.py",
            tool="semgrep",
        )
        findings = [f] * 201
        with pytest.raises(ValueError):
            SecurityRecord(
                changed_files=[cf],
                diff_truncated=False,
                scan=[sr],
                scanner_findings=findings,
                verdict="security_passed",
                covered_files=["app.py"],
            )


class TestModelSecurityFindingsSchema:
    """Test the schema builder for the model's findings call."""

    def test_schema_with_introduced_by(self):
        """Schema adds introduced_by and downgrade_reason to model findings."""
        schema = model_security_findings_schema(("injection", "weak_crypto"))
        # Validate that we can parse a finding with these fields
        result = schema.model_validate(
            {
                "findings": [
                    {
                        "path": "app.py",
                        "line": 10,
                        "category": "injection",
                        "problem": "SQL injection",
                        "why_blocking": "Attacker can bypass",
                        "evidence": "query = ...",
                        "introduced_by": "app.py",
                        "downgrade_reason": "",
                    }
                ]
            }
        )
        assert result.findings[0].introduced_by == "app.py"
        assert result.findings[0].downgrade_reason == ""

    def test_schema_rejects_severity_key(self):
        """Schema forbids severity key (verdict derived by code)."""
        schema = model_security_findings_schema(("injection",))
        with pytest.raises(ValueError):
            schema.model_validate(
                {
                    "findings": [
                        {
                            "path": "app.py",
                            "line": 10,
                            "category": "injection",
                            "problem": "SQL injection",
                            "why_blocking": "Attacker can bypass",
                            "evidence": "query = ...",
                            "introduced_by": "app.py",
                            "severity": "high",
                        }
                    ]
                }
            )

    def test_schema_rejects_routing_key(self):
        """Schema forbids routing key (routing derived by code)."""
        schema = model_security_findings_schema(("injection",))
        with pytest.raises(ValueError):
            schema.model_validate(
                {
                    "findings": [
                        {
                            "path": "app.py",
                            "line": 10,
                            "category": "injection",
                            "problem": "SQL injection",
                            "why_blocking": "Attacker can bypass",
                            "evidence": "query = ...",
                            "introduced_by": "app.py",
                            "routing": "implementer",
                        }
                    ]
                }
            )

    def test_schema_rejects_verdict_key(self):
        """Schema forbids verdict key (verdict derived by code)."""
        schema = model_security_findings_schema(("injection",))
        with pytest.raises(ValueError):
            schema.model_validate(
                {
                    "findings": [
                        {
                            "path": "app.py",
                            "line": 10,
                            "category": "injection",
                            "problem": "SQL injection",
                            "why_blocking": "Attacker can bypass",
                            "evidence": "query = ...",
                            "introduced_by": "app.py",
                            "verdict": "rejected",
                        }
                    ]
                }
            )

    def test_schema_rejects_unknown_category(self):
        """Unknown category is a schema violation."""
        schema = model_security_findings_schema(("injection", "weak_crypto"))
        with pytest.raises(ValueError):
            schema.model_validate(
                {
                    "findings": [
                        {
                            "path": "app.py",
                            "line": 10,
                            "category": "unknown_category",
                            "problem": "Something",
                            "why_blocking": "Fix it",
                            "evidence": "code",
                            "introduced_by": "app.py",
                        }
                    ]
                }
            )

    def test_schema_accepts_custom_categories(self):
        """Schema accepts a custom category set."""
        custom_cats = ("custom_one", "custom_two")
        schema = model_security_findings_schema(custom_cats)
        result = schema.model_validate(
            {
                "findings": [
                    {
                        "path": "app.py",
                        "line": 10,
                        "category": "custom_one",
                        "problem": "Issue",
                        "why_blocking": "Fix it",
                        "evidence": "code",
                        "introduced_by": "app.py",
                    }
                ]
            }
        )
        assert result.findings[0].category == "custom_one"

    def test_schema_caps_findings_at_30(self):
        """Default max_findings is 30."""
        schema = model_security_findings_schema(("injection",), max_findings=30)
        findings = [
            {
                "path": "app.py",
                "line": i,
                "category": "injection",
                "problem": f"Issue {i}",
                "why_blocking": "Fix it",
                "evidence": f"code_{i}",
                "introduced_by": "app.py",
            }
            for i in range(31)
        ]
        with pytest.raises(ValueError):
            schema.model_validate({"findings": findings})


class TestSecurityRecordJsonSchemaCompliance:
    """Test that SecurityRecord validates against contracts/security-record.schema.json."""

    @pytest.fixture
    def json_schema(self):
        """Load the contract JSON schema."""
        if not SCHEMA_PATH.exists():
            raise AssertionError(f"Schema file not found: {SCHEMA_PATH}")
        with open(SCHEMA_PATH) as f:
            return json.load(f)

    def test_record_round_trip_compliance(self, json_schema):
        """SecurityRecord.model_dump() validates against the JSON schema."""
        from performer.workflows.reviewer.models import ChangedFile, Hunk

        cf = ChangedFile(path="app.py", hunks=[Hunk(header="@@", start_line=1, end_line=5)], fully_in_diff=True)
        sr = ScanResult(tool="semgrep", command="cmd", exit_code=0, finding_count=0, duration_ms=100)
        rec = SecurityRecord(
            changed_files=[cf],
            diff_truncated=False,
            scan=[sr],
            verdict="security_passed",
            covered_files=["app.py"],
        )
        dumped = rec.model_dump()
        jsonschema.validate(dumped, json_schema)
