"""Tests for the 433 strict-mypy per-module allowlist ratchet."""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
SRC = PYPROJECT.parent / "src"


_SNAPSHOT: tuple[str, ...] = (
    "coordinare.auth",
    "coordinare.bench.space",
    "coordinare.bench.sweep",
    "coordinare.config",
    "coordinare.config_descriptors",
    "coordinare.config_validation",
    "coordinare.daemon",
    "coordinare.dashboard",
    "coordinare.doctor",
    "coordinare.eval.advocate_scenarios",
    "coordinare.eval.architect_scenarios",
    "coordinare.eval.assessor_scenarios",
    "coordinare.eval.closer_scenarios",
    "coordinare.eval.curator_scenarios",
    "coordinare.eval.documenter_scenarios",
    "coordinare.eval.gateway",
    "coordinare.eval.implementer_scenarios",
    "coordinare.eval.qa_scenarios",
    "coordinare.eval.reviewer_scenarios",
    "coordinare.eval.security_scenarios",
    "coordinare.graph.nodes.assess_card",
    "coordinare.graph.nodes.check_board",
    "coordinare.graph.nodes.classify_human_feedback",
    "coordinare.graph.nodes.github_retry",
    "coordinare.graph.nodes.handle_blocked",
    "coordinare.graph.nodes.handle_system_error",
    "coordinare.graph.nodes.monitor_performer",
    "coordinare.graph.nodes.monitor_pr",
    "coordinare.graph.nodes.notify",
    "coordinare.graph.nodes.route_issue_comments",
    "coordinare.graph.routing",
    "coordinare.graph.state",
    "coordinare.localhost_guard",
    "coordinare.models.rebase",
    "coordinare.protocol",
    "coordinare.routing_config_service",
    "coordinare.services.base_gate",
    "coordinare.services.ci_detection",
    "coordinare.services.ci_gate",
    "coordinare.services.claude",
    "coordinare.services.conducting",
    "coordinare.services.config_write_service",
    "coordinare.services.dispatch_guard",
    "coordinare.services.documentation_findings",
    "coordinare.services.documenting_side",
    "coordinare.services.env_cache",
    "coordinare.services.failure_classification",
    "coordinare.services.github",
    "coordinare.services.http_performer_service",
    "coordinare.services.intake_dispatch",
    "coordinare.services.kubernetes_egress",
    "coordinare.services.kubernetes_runtime",
    "coordinare.services.performer_pool",
    "coordinare.services.qa_verdict",
    "coordinare.services.rebase",
    "coordinare.services.reconciliation",
    "coordinare.services.required_checks_resolver",
    "coordinare.services.retry_counter",
    "coordinare.services.security_scanner",
    "coordinare.services.slot_manager",
    "coordinare.services.workflow_step",
    "coordinare.session",
    "coordinare.state_store",
    "coordinare.workspace",
)

def _ratchet_overrides() -> list[str]:
    doc = tomllib.loads(PYPROJECT.read_text())
    overrides = doc["tool"]["mypy"]["overrides"]
    blocks = [
        o for o in overrides
        if o.get("ignore_errors") is True and "coordinare." in str(o.get("module", ""))
    ]
    assert len(blocks) == 1, "expected exactly one coordinare allowlist block"
    return blocks[0]["module"]


class TestAllowlist:
    def test_allowlist_is_sorted(self) -> None:
        modules = _ratchet_overrides()
        assert modules == sorted(modules)

    def test_allowlist_has_no_duplicates(self) -> None:
        modules = _ratchet_overrides()
        assert len(modules) == len(set(modules))

    def test_allowlist_matches_the_committed_snapshot(self) -> None:
        """The allowlist can only shrink: additions require touching this file."""
        assert tuple(_ratchet_overrides()) == _SNAPSHOT

    def test_every_entry_maps_to_a_real_file(self) -> None:
        for module in _ratchet_overrides():
            parts = module.split(".")
            assert parts[0] == "coordinare", module
            path = SRC.joinpath(*parts)
            assert path.with_suffix(".py").exists() or (path / "__init__.py").exists(), module

    def test_the_ratchet_tooth_is_ignore_errors_true(self) -> None:
        doc = tomllib.loads(PYPROJECT.read_text())
        strict = doc["tool"]["mypy"]["strict"]
        assert strict is True
