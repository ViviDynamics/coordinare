"""Unit tests for the implementer CI gate inside monitor_performer (spec 075).

US1 (T020-T025a): gate fires at implementer→reviewer boundary using the
layer-3 (all_head_checks) resolver fallback. Verdicts: pass / bounce /
escalate / fail-open / no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coordinare.graph.nodes.monitor_performer import (
    _reset_ci_gate_api_error_cooldown,
    monitor_performer,
)
from coordinare.graph.state import initial_state


@pytest.fixture(autouse=True)
def _clear_ci_gate_error_cooldown() -> None:
    """Reset the API-error rate-limit window between tests so one test's error
    state cannot suppress warnings in a later test."""
    _reset_ci_gate_api_error_cooldown()
    yield  # type: ignore[misc]
    _reset_ci_gate_api_error_cooldown()

# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


def _rollup_payload(
    *,
    head_sha: str = "a" * 40,
    pushed_date: str | None = None,
    contexts: list[dict] | None = None,
) -> dict:
    if pushed_date is None:
        pushed_date = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "repository": {
            "pullRequest": {
                "number": 42,
                "baseRefName": "main",
                "headRefOid": head_sha,
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "oid": head_sha,
                                "pushedDate": pushed_date,
                                "statusCheckRollup": {
                                    "state": "PENDING",
                                    "contexts": {"nodes": contexts or []},
                                },
                            }
                        }
                    ]
                },
            },
            "branchProtectionRules": {"nodes": []},
        }
    }


class _GitHubWithRollup:
    def __init__(self, payload: dict | None = None, raise_on_execute: bool = False) -> None:
        self.move_calls: list[tuple[str, str]] = []
        self._payload = payload
        self._raise = raise_on_execute

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))

    async def _execute(self, query: str, variables: dict) -> dict:
        if self._raise:
            raise RuntimeError("graphql exploded")
        return self._payload or {}


def _ci_gate_state(
    github: object,
    *,
    enabled: bool = True,
    max_bounces: int = 3,
    bounce_counter: dict[str, int] | None = None,
) -> dict:
    """State that triggers the 075 gate at implementer→reviewer boundary.

    Performer reports pr_opened, lifecycle_sequence has implementing→reviewing
    so a successful gate decision should advance stage, not reach monitoring_pr.
    """
    from coordinare.config import CIGateConfig, PersonaScopeConfig

    state = initial_state()
    service = _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_NODE_42",
    })
    state["performer_services"] = {"implementing": service}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = github
    state["bounce_counter"] = bounce_counter if bounce_counter is not None else {}

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=enabled, max_bounces_per_head=max_bounces),
        )

    state["current_symphony"] = "default"
    state["symphony_configs"] = {"default": _Sym()}
    return state


# ---------------------------------------------------------------------------
# T020: bounces on failing required check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_bounces_on_failing_required_check() -> None:
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "name": "lint",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state(gh)

    result = await monitor_performer(state)

    # BOUNCE: stage stays implementing, phase dispatches.
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    relay = result.get("relay_feedback") or []
    assert relay and "unit-tests" in relay[-1]["body"]
    # Bounce counter incremented on this head.
    head = "a" * 40
    assert result["bounce_counter"][head] == 1
    # No board move.
    assert gh.move_calls == []
    # Decision archived.
    assert result["latest_ci_gate_decision"]["verdict"] == "bounce"
    assert result["latest_ci_gate_decision"]["bounce_count_after"] == 1


# ---------------------------------------------------------------------------
# T021: passes on all green; gate advances stage normally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_pass_on_all_green() -> None:
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "lint",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state(gh)

    result = await monitor_performer(state)

    # Advanced past implementing.
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert result.get("latest_ci_gate_decision", {}).get("verdict") == "pass"


# ---------------------------------------------------------------------------
# T022: escalates at bounce limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_escalates_at_bounce_limit() -> None:
    head = "a" * 40
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    # Already at limit-1; this bounce should escalate.
    state = _ci_gate_state(gh, max_bounces=3, bounce_counter={head: 2})

    result = await monitor_performer(state)

    # ESCALATE: needs_human_review path, no re-dispatch.
    assert result["phase"] == "blocked"
    assert result["performer_stage"] == "implementing"
    assert result["bounce_counter"][head] == 3
    assert result["latest_ci_gate_decision"]["verdict"] == "escalate"


# ---------------------------------------------------------------------------
# T023: counter resets on new head
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_counter_resets_on_new_head() -> None:
    new_head = "b" * 40
    payload = _rollup_payload(
        head_sha=new_head,
        contexts=[
            {"__typename": "CheckRun", "name": "lint",
             "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    # Old head had 2 bounces; new head starts fresh.
    state = _ci_gate_state(gh, bounce_counter={"a" * 40: 2})

    result = await monitor_performer(state)

    assert result["bounce_counter"]["a" * 40] == 2  # preserved (audit)
    assert result["bounce_counter"][new_head] == 1  # fresh
    assert result["latest_ci_gate_decision"]["verdict"] == "bounce"


# ---------------------------------------------------------------------------
# T024: fails open on GraphQL exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_fails_open_on_exception() -> None:
    gh = _GitHubWithRollup(raise_on_execute=True)
    state = _ci_gate_state(gh)

    result = await monitor_performer(state)

    # Fail-open: advance normally; no relay feedback.
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# T025: no-op when ci_gate.enabled=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_noop_when_disabled() -> None:
    gh = _GitHubWithRollup(raise_on_execute=True)
    state = _ci_gate_state(gh, enabled=False)

    result = await monitor_performer(state)

    # Disabled: no rollup query, normal advancement.
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# T025a: neutral and skipped count as pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_passes_with_neutral_and_skipped_required_checks() -> None:
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "tests",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "advisory",
             "status": "COMPLETED", "conclusion": "NEUTRAL"},
            {"__typename": "CheckRun", "name": "skipped-job",
             "status": "COMPLETED", "conclusion": "SKIPPED"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state(gh)

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["latest_ci_gate_decision"]["verdict"] == "pass"


# ---------------------------------------------------------------------------
# T029b: early-return PASS when no PR / no head_sha (FR-013)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_noop_when_no_pr() -> None:
    """A terminal success without pr_url shouldn't even reach the gate.

    The 075 gate is conditioned on a populated pr_url on the current card.
    A `pr_opened` marker without pr_url is malformed and handled elsewhere;
    here we cover the more realistic case: an intermediate-stage terminal
    success that doesn't carry pr_url at all (e.g., plan_committed) — the
    gate must not fire.
    """
    state = _ci_gate_state(_GitHubWithRollup(raise_on_execute=True))
    # Reset the lifecycle so the implementer terminal success doesn't carry pr_url.
    state["performer_services"]["implementing"] = _Performer({
        "status": "plan_committed",
    })

    result = await monitor_performer(state)

    # Gate didn't fire; normal advancement.
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"


# ---------------------------------------------------------------------------
# T033: holds on pending required check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_holds_on_pending_required_check() -> None:
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "IN_PROGRESS", "conclusion": None},
            {"__typename": "CheckRun", "name": "lint",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state(gh)

    result = await monitor_performer(state)

    # HOLD: stage stays implementing; phase parks at monitoring_performer.
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "monitoring_performer"
    # No bounce: counter untouched, no relay feedback.
    assert result.get("bounce_counter", {}) == {}
    assert not result.get("relay_feedback")
    # Decision archived with pending populated.
    decision = result["latest_ci_gate_decision"]
    assert decision["verdict"] == "hold"
    assert "unit-tests" in decision["pending_checks"]


# ---------------------------------------------------------------------------
# T034: bounces when pending exceeds pending_timeout_seconds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_bounces_on_pending_timeout() -> None:
    # pushedDate well past the default 900s timeout.
    stale = (datetime.now(UTC) - timedelta(seconds=3600)).isoformat().replace("+00:00", "Z")
    payload = _rollup_payload(
        pushed_date=stale,
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "IN_PROGRESS", "conclusion": None},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state(gh)

    result = await monitor_performer(state)

    # Timeout converts HOLD into BOUNCE.
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    assert result["bounce_counter"]["a" * 40] == 1
    assert result["latest_ci_gate_decision"]["verdict"] == "bounce"


# ---------------------------------------------------------------------------
# T046: PASS surfaces advisory (non-required) failures (FR-014)
# ---------------------------------------------------------------------------


def _ci_gate_state_with_check_map(github: object) -> dict:
    """State that pins the required set to `{lint}` via persona_check_map."""
    from coordinare.config import (
        CIGateConfig,
        PersonaCheckMapConfig,
        PersonaCheckMapPerDepth,
        PersonaScopeConfig,
    )
    from coordinare.session import create_session_from_card

    state = initial_state()
    service = _Performer({
        "status": "pr_opened",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_NODE_42",
    })
    state["performer_services"] = {"implementing": service}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["github_service"] = github
    state["bounce_counter"] = {}

    session = create_session_from_card({"id": "ITEM_1", "title": "t"})
    session["persona_scope"] = {"personas": {"implementer": {"depth": "normal"}}}
    state["active_sessions"] = {"ITEM_1": session}

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=True, max_bounces_per_head=3),
            persona_check_map=PersonaCheckMapConfig(
                root={"implementer": PersonaCheckMapPerDepth(normal=["lint*"])},
            ),
        )

    state["current_symphony"] = "default"
    state["symphony_configs"] = {"default": _Sym()}
    return state


@pytest.mark.asyncio
async def test_gate_passes_when_only_advisory_check_failed() -> None:
    """Required set is {lint} (green); `integration` fails but isn't required.

    FR-014: PASS verdict; advisory failure surfaced via state for reviewer.
    """
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "lint",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "integration",
             "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state_with_check_map(gh)

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    decision = result["latest_ci_gate_decision"]
    assert decision["verdict"] == "pass"
    assert decision["resolver_source"] == "persona_check_map"
    assert decision["required_checks"] == ["lint"]
    advisory = result.get("ci_gate_advisory_failures") or []
    assert any(a.get("name") == "integration" for a in advisory)
