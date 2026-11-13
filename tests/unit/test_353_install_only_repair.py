"""Maintainer ruling 2026-11-04: an install-only failure with an unchanged inventory and scope repairs installation
without rediscovering; reuse is keyed on those digests, not on the capabilities digest (its cache path moves)."""
from __future__ import annotations

from types import SimpleNamespace

from coordinare.services.environment_discovery import _install_only_repair
from coordinare.services.environment_repair_triage import RepairTriage
from coordinare.services.environment_scope_reuse import inventory_digest

PATHS = ["Gemfile", "README.md"]
SCOPE_DIGEST = "e" * 64


def _triage(roots):
    return RepairTriage(manifest_digest="d" * 64, roots=roots, consequences=(), closure_ids=tuple(roots),
                        criterion_ids=(), requirement_ids=(), touched_parts=("global",), digest="f" * 64)


def _case(*, roots=None, inventory=None, scope_digest=SCOPE_DIGEST, paths=PATHS):
    previous = SimpleNamespace(install=[SimpleNamespace(id="install-lang")], inventory_digest=inventory or inventory_digest(PATHS),
                               scope_contract_digest=SCOPE_DIGEST)
    scope = SimpleNamespace(digest=lambda: scope_digest,
                            definition=SimpleNamespace(inventory_digest=inventory_digest(PATHS)))
    options = SimpleNamespace(previous_manifest=previous, execution_feedback={"results": []})
    budget = SimpleNamespace(repair_triage=_triage(roots or {"install-lang": "command"}), scope_contract=scope)
    return _install_only_repair(options, budget, paths)


def test_an_install_failure_with_the_same_inventory_and_scope_skips_rediscovery():
    assert _case()


def test_a_changed_inventory_or_scope_restarts():
    assert not _case(paths=[*PATHS, "new.txt"])
    assert not _case(scope_digest="0" * 64)


def test_a_capability_or_check_failure_restarts():
    assert not _case(roots={"install-lang": "capability"})
    assert not _case(roots={"install-lang": "command", "check-suite": "command"})
