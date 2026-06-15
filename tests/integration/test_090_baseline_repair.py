"""090 US3 — end-to-end integration tests for autonomous baseline repair (L3).

Background
----------
Spec 090 layer 3 is the governing intent: when a card is blocked *solely* by an
inherited, code-fixable required-check failure (a red check the PR did not
introduce — it is red on the base branch too), the coordinare — with repair
enabled — dispatches an autonomous fix carrying a do-not-weaken mandate, then
adjudicates the landed candidate through a dual test-integrity guard (static
``analyze_diff`` + an independent adversarial reviewer). It lands the fix as a
*candidate* and **never** auto-merges; merge requires a fresh human approval
recorded after the repair commit.

What these tests assert (whole-node, real services)
---------------------------------------------------
Unlike the focused unit tests in
``tests/unit/graph/nodes/test_monitor_performer_ci_gate.py``, these drive the
**real** ``monitor_performer`` node across two ticks through a GraphQL-shaped
fake GitHub service with the **real** persona-scoped CI/classification/repair
gate configs, the **real** ``PrChecksService`` (head + base rollup fetch +
classification), the **real** ``failure_signature`` / ``failure_classification``
/ ``test_integrity_guard`` services, the **real** repair-mandate builder, the
**real** per-head budget accounting, and the **real** dual-guard adjudication
and audit trail.

The single seam not yet wired end-to-end is the ``diagnostic``-role adversarial
reviewer transport (``_dispatch_repair_reviewer``, which fails safe until the
real probe exists). These tests monkeypatch *only* that function — every other
code path (classification, signatures, mandate build, static guard, audit
append, candidate comment, stage advancement) runs for real.

- SC-008: a card blocked solely by an inherited, code-fixable required-check
  failure is *resolved* — tick 1 autonomously dispatches a repair mandate
  (instead of self-blocking / giving up), and tick 2 (the repaired head now
  green) clears the dual guard and advances toward review up to the final human
  approval. The contrast case (repair disabled) shows the same inherited failure
  only bounces, with no mandate and no autonomous resolution.
- SC-005: an accepted repair is landed as a *candidate* on the active branch and
  is **never** auto-merged — the board is not moved to a terminal/merge column,
  the flow advances only to ``reviewing`` (not ``merging``/``monitoring_pr``),
  and the coordinare posts a comment restating that a prior approval is dismissed
  on the new head and fresh re-approval is required before merge.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.graph.nodes.monitor_performer import (
    _reset_ci_gate_api_error_cooldown,
    monitor_performer,
)
from coordinare.graph.state import initial_state

_HEAD_SHA = "a" * 40

# A production-only candidate repair diff: it touches no test file, so the
# static test-integrity guard clears it (the do-not-weaken mandate is about
# tests; a real production fix to the inherited failure is exactly what should
# be allowed to land).
_REPAIR_DIFF = """\
diff --git a/src/coordinare/widget.py b/src/coordinare/widget.py
index 1234567..89abcde 100644
--- a/src/coordinare/widget.py
+++ b/src/coordinare/widget.py
@@ -1,3 +1,4 @@
 def answer() -> int:
-    return 41
+    # off-by-one fixed (the inherited lint/test failure)
+    return 42
"""


@pytest.fixture(autouse=True)
def _clear_ci_gate_error_cooldown() -> None:
    """Reset the API-error rate-limit window between tests so one test's error
    state cannot suppress warnings in a later test."""
    _reset_ci_gate_api_error_cooldown()
    yield  # type: ignore[misc]
    _reset_ci_gate_api_error_cooldown()


# ---------------------------------------------------------------------------
# GraphQL-shaped fake GitHub service serving head rollup + base rollup, plus the
# candidate diff fetch and comment / board-move capture.
# ---------------------------------------------------------------------------


def _check_run(name: str, conclusion: str, *, title: str | None = None,
               summary: str | None = None) -> dict:
    node: dict = {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
    }
    if title is not None or summary is not None:
        node["output"] = {"title": title, "summary": summary}
    return node


def _head_rollup(*, contexts: list[dict], state: str) -> dict:
    pushed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "repository": {
            "pullRequest": {
                "number": 42,
                "baseRefName": "main",
                "headRefOid": _HEAD_SHA,
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": _HEAD_SHA,
                                "pushedDate": pushed,
                                "statusCheckRollup": {
                                    "state": state,
                                    "contexts": {"nodes": contexts},
                                },
                            }
                        }
                    ]
                },
            },
            "branchProtectionRules": {"nodes": []},
        }
    }


def _base_rollup(*, contexts: list[dict]) -> dict:
    return {
        "repository": {
            "ref": {
                "target": {
                    "oid": "c" * 40,
                    "statusCheckRollup": {
                        "state": "FAILURE",
                        "contexts": {"nodes": contexts},
                    },
                }
            },
            "branchProtectionRules": {"nodes": []},
        }
    }


# The inherited failure: ``lint`` is red on the HEAD *and* red on the base with
# the byte-identical reason — so the L2 classifier labels it INHERITED and L3
# may autonomously repair it.
_INHERITED_LINT = _check_run("lint", "FAILURE", title="ruff E501", summary="line too long")


class _RepairFlowGitHub:
    """A single ``_execute`` serving both rollup shapes (head query vs.
    ``BaseCheckRollup`` base query), plus the candidate diff fetch and
    comment / board-move capture used by the dual guard.

    Omits ``get_required_status_checks`` so the resolver falls back to layer-3
    (all head checks required) — the red ``lint`` therefore bounces and reaches
    the classifier. The head payload is swappable so a test can model the
    repaired (now-green) head on the second tick.
    """

    def __init__(self, *, head_payload: dict, base_payload: dict, pr_diff: str) -> None:
        self.head_payload = head_payload
        self.base_payload = base_payload
        self._pr_diff = pr_diff
        self.move_calls: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []

    async def _execute(self, query: str, variables: dict) -> dict:
        if "BaseCheckRollup" in query:
            return self.base_payload
        return self.head_payload

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))

    async def add_comment(self, subject_id: str, body: str) -> dict:
        self.comments.append((subject_id, body))
        return {}

    async def get_pr_diff(self, pr_url: str) -> tuple[str, list[str]]:
        _ = pr_url
        return self._pr_diff, []


def _repair_flow_state(
    github: object,
    *,
    enabled: bool = True,
    max_attempts: int = 1,
    inheritance_repair_counter: dict[str, int] | None = None,
    repair_audit: list[dict] | None = None,
) -> dict:
    """A session that triggers the implementer CI gate at the
    implementer→reviewer boundary with the L2 classifier and L3 repair gate
    configured.

    The implementer reports terminal success (``pr_opened``) so the CI gate runs
    on each tick; the card sits IN_PROGRESS with a real PR URL so the gate and
    the candidate-diff fetch have a PR to act on.
    """
    from coordinare.config import (
        BaselineClassificationGateConfig,
        CIGateConfig,
        InheritedRepairGateConfig,
        PersonaScopeConfig,
    )

    class _Performer:
        async def check_status(self, session_id: str, **kwargs: object) -> dict:
            _ = (session_id, kwargs)
            return {
                "status": "pr_opened",
                "pr_url": "https://github.com/org/repo/pull/42",
                "pr_node_id": "PR_NODE_42",
            }

    state = initial_state()
    state["performer_services"] = {"implementing": _Performer()}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = github
    state["bounce_counter"] = {}
    state["inheritance_repair_counter"] = (
        dict(inheritance_repair_counter) if inheritance_repair_counter is not None else {}
    )
    if repair_audit is not None:
        state["repair_audit"] = [dict(r) for r in repair_audit]

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=True, max_bounces_per_head=3),
            baseline_classification_gate=BaselineClassificationGateConfig(enabled=True),
            inherited_repair_gate=InheritedRepairGateConfig(
                enabled=enabled, max_repair_attempts_per_head=max_attempts
            ),
        )

    state["current_symphony"] = "default"
    state["symphony_configs"] = {"default": _Sym()}
    return state


def _audit_kinds(result: dict) -> list[str]:
    return [r["kind"] for r in result.get("repair_audit", [])]


# ---------------------------------------------------------------------------
# SC-008 — inherited, code-fixable failure resolved (dispatch then accept).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sc008_inherited_failure_autonomously_repaired_up_to_review(monkeypatch) -> None:
    """SC-008: a card blocked solely by an inherited, code-fixable required-check
    failure is *resolved* with repair enabled — instead of self-blocking, the
    coordinare dispatches an autonomous repair (tick 1) and, once the repaired
    head is green and clears the dual guard, advances toward human review (tick
    2). No human intervention is needed up to the final approval."""
    gh = _RepairFlowGitHub(
        head_payload=_head_rollup(contexts=[_INHERITED_LINT], state="FAILURE"),
        base_payload=_base_rollup(contexts=[_INHERITED_LINT]),
        pr_diff=_REPAIR_DIFF,
    )

    # --- Tick 1: the inherited lint failure is classified and autonomously
    # dispatched as a repair (NOT self-blocked). ---
    tick1 = await monitor_performer(_repair_flow_state(gh))

    assert tick1["phase"] == "dispatching"
    assert tick1["latest_ci_gate_decision"]["verdict"] == "bounce"
    # The inherited failure drives a real repair mandate built only from the
    # INHERITED check, carrying the do-not-weaken instruction.
    mandate = tick1["repair_mandate"]
    assert {c["name"] for c in mandate["inherited_checks"]} == {"lint"}
    assert mandate["attempt"] == 1
    instr = mandate["instruction"].lower()
    for forbidden in ("weaken", "skip", "xfail"):
        assert forbidden in instr, forbidden
    # The per-head budget is consumed at dispatch and a ``dispatch`` audit record
    # is appended — the coordinare took autonomous action rather than giving up.
    assert tick1["inheritance_repair_counter"][_HEAD_SHA] == 1
    assert _audit_kinds(tick1) == ["dispatch"]
    assert gh.move_calls == []  # nothing merged/moved at dispatch

    # --- Tick 2: the repair fixed the inherited failure (head now green). The
    # dual guard adjudicates the landed candidate before the CI gate. The only
    # un-wired seam — the adversarial reviewer — is monkeypatched to clear; the
    # static guard runs for real over the production-only diff. ---
    async def _reviewer_clears(**_):
        return True, ""

    monkeypatch.setattr(
        "coordinare.graph.nodes.monitor_performer._dispatch_repair_reviewer",
        _reviewer_clears,
    )
    gh.head_payload = _head_rollup(
        contexts=[_check_run("lint", "SUCCESS")], state="SUCCESS"
    )

    tick2 = await monitor_performer(
        _repair_flow_state(
            gh,
            inheritance_repair_counter=tick1["inheritance_repair_counter"],
            repair_audit=tick1["repair_audit"],
        )
    )

    # Both guard halves cleared → the repair lands as a candidate and the flow
    # advances toward review — resolved without human intervention up to here.
    assert tick2["phase"] != "blocked"
    assert tick2["performer_stage"] == "reviewing"
    assert _audit_kinds(tick2) == [
        "dispatch",
        "static_guard",
        "reviewer",
        "acceptance",
    ]
    assert tick2["repair_audit"][1]["is_safe"] is True
    assert tick2["repair_audit"][1]["flagged_patterns"] == []
    assert tick2["repair_audit"][3]["kind"] == "acceptance"


@pytest.mark.asyncio
async def test_sc008_contrast_disabled_repair_only_bounces(monkeypatch) -> None:
    """SC-008 contrast: with repair *disabled* the same inherited, code-fixable
    failure is only bounced back to the implementer — no autonomous repair
    mandate, no budget consumption, no audit. This is the pre-spec-090 behavior
    the originating problem describes (the card cannot resolve an inherited
    failure it did not introduce and would self-block), proving the resolution
    in the enabled case is L3's doing (SC-006 parity)."""
    # The reviewer must never be consulted when repair is disabled.
    async def _reviewer_must_not_run(**_):
        raise AssertionError("the adversarial reviewer must not run with L3 disabled")

    monkeypatch.setattr(
        "coordinare.graph.nodes.monitor_performer._dispatch_repair_reviewer",
        _reviewer_must_not_run,
    )

    gh = _RepairFlowGitHub(
        head_payload=_head_rollup(contexts=[_INHERITED_LINT], state="FAILURE"),
        base_payload=_base_rollup(contexts=[_INHERITED_LINT]),
        pr_diff=_REPAIR_DIFF,
    )

    result = await monitor_performer(_repair_flow_state(gh, enabled=False))

    # L2 still classifies the inherited failure (so the absence of repair is the
    # L3 flag, not a missing INHERITED label)...
    assert {c["name"] for c in result["latest_ci_gate_decision"]["inherited_checks"]} == {"lint"}
    # ...but with L3 off there is no autonomous repair: a plain bounce, no
    # mandate, no budget mutation, no audit, no merge.
    assert result["phase"] == "dispatching"
    assert result["latest_ci_gate_decision"]["verdict"] == "bounce"
    assert "repair_mandate" not in result
    assert result.get("inheritance_repair_counter", {}) == {}
    assert result.get("repair_audit", []) == []
    assert gh.move_calls == []
    assert gh.comments == []


# ---------------------------------------------------------------------------
# SC-005 — an accepted repair is landed as a candidate, never auto-merged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sc005_accepted_repair_lands_as_candidate_never_auto_merged(monkeypatch) -> None:
    """SC-005: when the dual guard clears the candidate repair, the coordinare
    lands it as a candidate and NEVER auto-merges. The board is not moved to a
    terminal/merge column, the flow advances only to ``reviewing`` (not
    ``merging``/``monitoring_pr``), and a comment restates that a prior approval
    is dismissed on the new head so fresh re-approval is required before merge —
    i.e. the repair reaches ``main`` only via a fresh post-repair human
    approval."""
    async def _reviewer_clears(**_):
        return True, ""

    monkeypatch.setattr(
        "coordinare.graph.nodes.monitor_performer._dispatch_repair_reviewer",
        _reviewer_clears,
    )

    # The repaired head is green; a pending ``dispatch`` record from the prior
    # tick is on the audit trail so the guard adjudicates this candidate.
    gh = _RepairFlowGitHub(
        head_payload=_head_rollup(contexts=[_check_run("lint", "SUCCESS")], state="SUCCESS"),
        base_payload=_base_rollup(contexts=[_INHERITED_LINT]),
        pr_diff=_REPAIR_DIFF,
    )
    pending_dispatch = [
        {
            "head_sha": _HEAD_SHA,
            "attempt": 1,
            "kind": "dispatch",
            "is_safe": None,
            "flagged_patterns": [],
            "detail": "Dispatched autonomous baseline-repair attempt 1/1.",
            "decided_at": "2026-06-14T00:00:00Z",
        }
    ]

    result = await monitor_performer(
        _repair_flow_state(
            gh,
            inheritance_repair_counter={_HEAD_SHA: 1},
            repair_audit=pending_dispatch,
        )
    )

    # Landed as a candidate: advanced past implementing, never blocked.
    assert result["phase"] != "blocked"
    assert result["performer_stage"] == "reviewing"
    # NEVER auto-merged: no board move to a terminal/merge column, and the flow
    # did not jump to merging / monitoring_pr.
    assert gh.move_calls == []
    assert result["performer_stage"] != "monitoring_pr"
    assert result["phase"] not in ("merging", "monitoring_pr")
    # The audit records the clean acceptance.
    assert _audit_kinds(result) == ["dispatch", "static_guard", "reviewer", "acceptance"]

    # Exactly one candidate comment restating the no-auto-merge / stale-approval
    # assumption — re-approval on the repaired head is required before merge.
    assert len(gh.comments) == 1
    body = gh.comments[0][1].lower()
    assert "approval" in body
    assert "merge" in body
    assert "not" in body and "auto-merged" in body
