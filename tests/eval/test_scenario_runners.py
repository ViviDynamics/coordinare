"""The stubbed scenario runners (164 to 172) are CLIs a human runs; exercise
their stubbed path under pytest so a broken runner is caught in CI and so
the coverage gate measures them (they tipped the total under 90 percent on
spec 167's branch)."""
from __future__ import annotations

import asyncio

import pytest

from coordinare.eval import (
    advocate_scenarios,
    architect_scenarios,
    assessor_scenarios,
    closer_scenarios,
    curator_scenarios,
    documenter_scenarios,
    implementer_scenarios,
    reviewer_scenarios,
    security_scenarios,
)


@pytest.mark.parametrize("module", [architect_scenarios, assessor_scenarios, implementer_scenarios, reviewer_scenarios, security_scenarios, documenter_scenarios, closer_scenarios, advocate_scenarios, curator_scenarios])
def test_stubbed_runner_passes_every_fixture(module):
    scores = asyncio.run(module.run_all(live=False, only=None))
    assert scores, "the runner produced no results"
    for score in scores:
        passed = score.passed if hasattr(score, "passed") else score.get("passed")
        assert passed, getattr(score, "notes", None) or score


@pytest.mark.parametrize("module", [architect_scenarios, assessor_scenarios, implementer_scenarios, reviewer_scenarios, security_scenarios, documenter_scenarios, closer_scenarios, advocate_scenarios, curator_scenarios])
def test_runner_main_exits_zero_stubbed(module, capsys):
    assert module.main([]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out


def test_runner_main_only_selects_one_fixture(capsys):
    assert implementer_scenarios.main(["--only", "chore"]) == 0
    out = capsys.readouterr().out
    assert "chore" in out and "two_milestones" not in out


def test_reviewer_runner_main_only_selects_one_fixture(capsys):
    assert reviewer_scenarios.main(["--only", "findings"]) == 0
    out = capsys.readouterr().out
    assert "findings" in out and "hallucinated_anchor" not in out


def test_closer_runner_main_only_selects_one_fixture(capsys):
    assert closer_scenarios.main(["--only", "clean"]) == 0
    out = capsys.readouterr().out
    assert "clean" in out and "hallucinated_quote" not in out


def test_documenter_runner_main_only_selects_one_fixture(capsys):
    assert documenter_scenarios.main(["--only", "trivial"]) == 0
    out = capsys.readouterr().out
    assert "trivial" in out and "hallucinated_citation" not in out


def test_security_runner_main_only_selects_one_fixture(capsys):
    assert security_scenarios.main(["--only", "secret"]) == 0
    out = capsys.readouterr().out
    assert "secret" in out and "scanner_unavailable" not in out


def test_qa_runner_main_exits_zero_stubbed_with_one_repeat(capsys):
    from coordinare.eval import qa_scenarios

    assert qa_scenarios.main(["--repeats", "1"]) == 0
    assert capsys.readouterr().out.strip()


def test_advocate_runner_main_only_selects_one_fixture(capsys):
    assert advocate_scenarios.main(["--only", "answerable"]) == 0
    out = capsys.readouterr().out
    assert "answerable" in out and "hallucinated_citation" not in out


def test_curator_runner_main_only_selects_one_fixture(capsys):
    assert curator_scenarios.main(["--only", "qualifies"]) == 0
    out = capsys.readouterr().out
    assert "qualifies" in out and "unquotable_reason" not in out


@pytest.mark.parametrize(
    ("module", "fixture", "payload"),
    [
        ("advocate_scenarios", "sensitive_keyword", {"classifications": []}),
        ("curator_scenarios", "does_not_qualify",
         {"judgements": [{"issue_id": "I_2", "qualifies": False,
                          "reason": "no acceptance criteria", "quote": ""}]}),
    ],
)
def test_the_live_path_wires_the_real_gateway_helper(monkeypatch, module, fixture, payload):
    """The --live branch is the one a human runs and CI never exercises. An
    earlier draft imported a module that did not exist, so it would have failed
    on import at the first live round. This runs the branch with the gateway
    call stubbed, proving the wiring rather than the model."""
    import importlib
    import json as _json

    from performer.workflows.budget import ModelReply

    gateway = importlib.import_module("coordinare.eval.gateway")

    async def _fake_call(persona, content, max_tokens):
        return ModelReply(content=_json.dumps(payload), finish_reason="stop")

    monkeypatch.setattr(gateway, "_call_model", _fake_call)

    runner = importlib.import_module(f"coordinare.eval.{module}")
    scores = asyncio.run(runner.run_all(live=True, only=fixture))
    assert scores and scores[0].passed, getattr(scores[0], "notes", None)
