"""The structured handoff shares the documenter's evidence budget."""
from __future__ import annotations

from performer.workflows.documenter.gather import with_analysis_inputs


def test_combined_evidence_budget_retains_every_roles_attribution():
    findings = {role: {"source_head": f"head-{role}", "findings": {"details": "x" * 12000}}
                for role in ("assessing", "architecting", "reviewing", "security", "qa")}
    result = with_analysis_inputs("repository evidence" * 4000, findings, max_chars=24000)
    assert len(result) <= 24000
    assert result.startswith("repository evidence")
    for role in findings:
        assert f"Role {role}; source head head-{role}" in result
    assert "[truncated]" in result
    assert with_analysis_inputs("unchanged", {}, max_chars=1) == "unchanged"
    assert len(with_analysis_inputs("evidence", findings, max_chars=3)) <= 3
