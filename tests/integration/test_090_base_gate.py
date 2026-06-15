"""090 US1 — end-to-end integration tests for the L1 base-precondition gate.

Background
----------
Spec 090 (Baseline Repair Autonomy) layer 1 is a *prevention* gate: at the merge
transition in ``monitor_pr`` — once a PR is human-APPROVED and its own HEAD checks
are green — the coordinare refuses to advance to ``merging`` while a **required**
check on the PR's *base* branch is red.  The gate is config-gated
(``baseline_prevention_gate.enabled``), fail-safe (an unfetchable base never
hard-blocks), and never latched (re-read every cycle).

What these tests assert (whole-node, real services)
---------------------------------------------------
Unlike the focused unit tests in ``tests/unit/graph/nodes/test_monitor_pr.py``,
these drive the **real** ``monitor_pr`` node through a GraphQL-shaped fake GitHub
service with the **real** ``PersonaScopeConfig`` / ``BaselinePreventionGateConfig``,
the **real** ``PrChecksService`` (head + base rollup fetch + ``parse_base_rollup``),
and the **real** ``evaluate_base_gate`` decision — exercising the full merge-decision
path rather than a mocked gate.

- SC-001: an approved, head-green PR is held in ``monitoring_pr`` (no merge, no board
  move) while a REQUIRED base check is red; the *same* cached service re-reads the
  base on the next cycle and advances to ``merging`` once the base turns green
  (proving no latch).
- SC-002: an unfetchable base is INDETERMINATE — the merge proceeds exactly as the
  pre-feature baseline, with no base-not-green hold recorded.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from structlog.testing import capture_logs

from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state

# ---------------------------------------------------------------------------
# GraphQL-shaped fake GitHub service (head PR rollup + base-branch rollup).
# ---------------------------------------------------------------------------


class _GateGitHub:
    """A fake github service serving both rollup shapes through one ``_execute``.

    The head PR rollup query binds ``pr``; the base-branch rollup query binds
    ``qualifiedName`` — so a single fake can serve both.  A ``None`` base payload
    models an *unfetchable* base: the query raises, the fail-safe in
    ``get_base_branch_check_rollup`` swallows it and returns ``None``, and
    ``evaluate_base_gate`` reports INDETERMINATE (SC-002).
    """

    def __init__(
        self,
        head_payload: dict,
        reviews: list[dict],
        *,
        base_payload: dict | None,
    ) -> None:
        self._head_payload = head_payload
        self._base_payload = base_payload
        self._reviews = reviews
        self.move_calls: list[tuple[str, str]] = []
        self.execute_calls: list[dict] = []

    async def _execute(self, query: str, variables: dict) -> dict:
        self.execute_calls.append(variables)
        if "qualifiedName" in variables:
            if self._base_payload is None:
                raise RuntimeError("base rollup unavailable (test: indeterminate)")
            return self._base_payload
        return self._head_payload

    async def get_pr_reviews(self, pr_id: str) -> list[dict]:
        _ = pr_id
        return self._reviews

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))


def _green_approved_head() -> tuple[dict, list[dict]]:
    """A green-HEAD, human-APPROVED PR: the closer gate FORWARDs and the human
    approval would route straight to ``merging`` — so the *only* thing that can
    hold the merge is the L1 base gate."""
    pushed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    head_payload = {
        "repository": {
            "pullRequest": {
                "number": 42,
                "baseRefName": "main",
                "headRefOid": "headsha0",
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": "headsha0",
                                "pushedDate": pushed,
                                "statusCheckRollup": {
                                    "state": "SUCCESS",
                                    "contexts": {
                                        "nodes": [
                                            {
                                                "__typename": "CheckRun",
                                                "name": "ci/test",
                                                "status": "COMPLETED",
                                                "conclusion": "SUCCESS",
                                            }
                                        ]
                                    },
                                },
                            }
                        }
                    ]
                },
            },
            "branchProtectionRules": {
                "nodes": [
                    {
                        "pattern": "main",
                        "requiredStatusChecks": [{"context": "ci/test"}],
                    }
                ]
            },
        }
    }
    reviews = [{"id": "R1", "author_login": "alice", "state": "APPROVED"}]
    return head_payload, reviews


def _base_payload(*, contexts: list[dict], bpr_nodes: list[dict]) -> dict:
    """A base-branch rollup GraphQL response (Ref → target Commit), shaped to
    what the real ``parse_base_rollup`` consumes."""
    return {
        "repository": {
            "ref": {
                "target": {
                    "oid": "base0000",
                    "statusCheckRollup": {"contexts": {"nodes": contexts}},
                }
            },
            "branchProtectionRules": {"nodes": bpr_nodes},
        }
    }


def _ctx(name: str, conclusion: str | None, *, status: str = "COMPLETED") -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


def _gate_state(github: object, *, enabled: bool = True) -> dict:
    """A session with a real persona-scoped L1 gate config and the card sitting
    in IN_REVIEW (the merge precondition)."""
    from coordinare.config import (
        BaselinePreventionGateConfig,
        CloserPrChecksConfig,
        PersonaScopeConfig,
    )

    state = initial_state()
    state["current_card"] = {
        "id": "ITEM_42",
        "pr_node_id": "PR_NODE_42",
        "pr_url": "https://github.com/org/repo/pull/42",
        "status": "IN_REVIEW",
    }
    state["github_service"] = github
    state["human_reviewers"] = ["alice"]

    class _Sym:
        closer_pr_checks = CloserPrChecksConfig()
        persona_scope = PersonaScopeConfig(
            baseline_prevention_gate=BaselinePreventionGateConfig(enabled=enabled)
        )

    state["current_symphony"] = "default"
    state["symphony_configs"] = {"default": _Sym()}
    return state


# ---------------------------------------------------------------------------
# SC-001 — no merge while required base red; merge proceeds once base green.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l1_e2e_holds_while_base_red_then_merges_once_base_green() -> None:
    head_payload, reviews = _green_approved_head()
    bpr = [{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}]
    gh = _GateGitHub(
        head_payload,
        reviews,
        base_payload=_base_payload(
            contexts=[_ctx("ci/test", "FAILURE")], bpr_nodes=bpr
        ),
    )

    # Cycle 1: required base check is red → hold in monitoring_pr, no board move,
    # and the offending base check is recorded distinctly from the (green) head.
    with capture_logs() as logs:
        result1 = await monitor_pr(_gate_state(gh, enabled=True))

    assert result1["phase"] == "monitoring_pr"
    assert gh.move_calls == []
    holds = [e for e in logs if e.get("event") == "monitor_pr.base_not_green_hold"]
    assert len(holds) == 1
    assert holds[0]["base_ref"] == "main"
    assert [c["name"] for c in holds[0]["base_failing_checks"]] == ["ci/test"]
    # A base-branch query was actually issued (the gate fetched the base rollup).
    assert any("qualifiedName" in v for v in gh.execute_calls)

    # Base turns green; the SAME github (its cached PrChecksService) is re-read.
    gh._base_payload = _base_payload(
        contexts=[_ctx("ci/test", "SUCCESS")], bpr_nodes=bpr
    )
    with capture_logs() as logs2:
        result2 = await monitor_pr(_gate_state(gh, enabled=True))

    # No latch: a green base proceeds straight to merging on the next cycle.
    assert result2["phase"] == "merging"
    assert [e for e in logs2 if e.get("event") == "monitor_pr.base_not_green_hold"] == []


# ---------------------------------------------------------------------------
# SC-002 — indeterminate base → identical-to-baseline merge behavior.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l1_e2e_indeterminate_base_merges_like_baseline() -> None:
    head_payload, reviews = _green_approved_head()
    # base_payload=None → the base query raises → the fail-safe returns None →
    # evaluate_base_gate reports INDETERMINATE.
    gh = _GateGitHub(head_payload, reviews, base_payload=None)

    with capture_logs() as logs:
        result = await monitor_pr(_gate_state(gh, enabled=True))

    # Falls through to head-only behavior: the approved, head-green PR merges
    # exactly as it would pre-feature, and no hold is recorded (SC-002).
    assert result["phase"] == "merging"
    assert gh.move_calls == []
    assert [e for e in logs if e.get("event") == "monitor_pr.base_not_green_hold"] == []


@pytest.mark.asyncio
async def test_l1_e2e_disabled_gate_never_fetches_base() -> None:
    """SC-006 parity: with the gate disabled the approved, head-green PR merges
    exactly as today and no base-branch query is ever issued — even when the base
    is red, proving the disabled path is byte-identical to the baseline."""
    head_payload, reviews = _green_approved_head()
    gh = _GateGitHub(
        head_payload,
        reviews,
        base_payload=_base_payload(
            contexts=[_ctx("ci/test", "FAILURE")],
            bpr_nodes=[{"pattern": "main", "requiredStatusChecks": [{"context": "ci/test"}]}],
        ),
    )

    result = await monitor_pr(_gate_state(gh, enabled=False))

    assert result["phase"] == "merging"
    assert all("qualifiedName" not in v for v in gh.execute_calls)
