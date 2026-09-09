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
# 095: ENV_BLOCKED classification (observe-only at this layer)
# ---------------------------------------------------------------------------


def _env_gate_state(github: object) -> dict:
    """State with the env_blocked + L2 classification gates enabled."""
    from coordinare.config import (
        BaselineClassificationGateConfig,
        CIGateConfig,
        EnvBlockedGateConfig,
        PersonaScopeConfig,
    )

    state = _ci_gate_state(github)

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=True, max_bounces_per_head=3),
            baseline_classification_gate=BaselineClassificationGateConfig(enabled=True),
            env_blocked_gate=EnvBlockedGateConfig(enabled=True),
        )

    state["symphony_configs"] = {"default": _Sym()}
    return state


@pytest.mark.asyncio
async def test_env_blocked_holds_no_bounce_no_dispatch() -> None:
    """095 (US1/SC-001/SC-007): a required failing check whose reason is an infra
    signature (artifact-storage quota, also failing on base) HOLDs the card —
    verdict=hold, NO bounce counter increment, NO re-dispatch — and surfaces the
    env_blocked checks + a deduped operator event."""
    import structlog.testing

    payload = _rollup_payload(
        contexts=[
            {
                "__typename": "CheckRun",
                "name": "Build Pull Request",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "title": "Failed to CreateArtifact: artifact storage quota has been hit",
            },
        ],
    )
    gh = _GitHubWithRollup(payload)  # base rollup == head rollup → also fails on base
    state = _env_gate_state(gh)

    with structlog.testing.capture_logs() as cap_logs:
        result = await monitor_performer(state)

    decision = result["latest_ci_gate_decision"]
    head = "a" * 40
    assert decision["verdict"] == "hold", decision
    assert "Build Pull Request" in [c["name"] for c in decision.get("env_blocked_checks", [])]
    # Not misclassified as inherited despite also failing on base.
    assert "Build Pull Request" not in [c["name"] for c in decision.get("inherited_checks", [])]
    # SC-001: no bounce was counted; the card is not re-dispatched.
    assert result.get("bounce_counter", {}).get(head, 0) == 0
    assert result.get("phase") != "dispatching"
    # SC-002: one operator event naming the cause + action, secret-free.
    holds = [e for e in cap_logs if e["event"] == "ci_gate.env_blocked_hold"]
    assert len(holds) == 1 and holds[0]["pattern_id"] == "artifact_storage_quota"
    assert "storage" in holds[0]["action"].lower()


@pytest.mark.asyncio
async def test_env_blocked_dispatches_distinct_operator_notification() -> None:
    """095 (T012/FR-003): the env-block surfaces through the real notification
    channel as a DISTINCT ``env_blocked`` event carrying the infra cause + action
    (not a generic 'tests failed'), deduped per (head, pattern)."""
    from coordinare.models.notification import EventType

    class _CapturingNotifier:
        def __init__(self) -> None:
            self.events: list = []

        async def dispatch(self, event):
            self.events.append(event)

    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "Build Pull Request",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "Failed to CreateArtifact: artifact storage quota has been hit"},
        ],
    )
    notifier = _CapturingNotifier()
    state = _env_gate_state(_GitHubWithRollup(payload))
    state["notification_service"] = notifier

    result = await monitor_performer(state)

    assert result["latest_ci_gate_decision"]["verdict"] == "hold"
    assert len(notifier.events) == 1
    event = notifier.events[0]
    assert event.event_type == EventType.env_blocked
    assert event.dedup_key == "env_blocked:" + result["latest_ci_gate_decision"]["env_blocked_checks"][0]["head_signature"]
    assert event.payload["pattern_id"] == "artifact_storage_quota"
    assert "storage" in event.payload["action"].lower()
    # secret-free: payload carries only identifiers/cause/action
    assert "head_sha" in event.payload and len(event.payload["head_sha"]) == 7

    # Re-evaluating the SAME persisted block must NOT re-dispatch (dedup).
    state["env_blocked"] = result.get("env_blocked")
    await monitor_performer(state)
    assert len(notifier.events) == 1


@pytest.mark.asyncio
async def test_env_blocked_multiple_patterns_aggregated_in_notification() -> None:
    """095 (review fix): a card blocked by TWO DISTINCT infra patterns (artifact
    quota + offline runner) must surface BOTH causes in one notification, and the
    dedup key must reflect the full sorted set of pattern ids — not just the first
    matched pattern (which would mask the second cause and mis-dedup)."""
    class _CapturingNotifier:
        def __init__(self) -> None:
            self.events: list = []

        async def dispatch(self, event):
            self.events.append(event)

    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "Build Pull Request",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "artifact storage quota has been hit"},
            {"__typename": "CheckRun", "name": "deploy",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "cancelled because no runner came online"},
        ],
    )
    notifier = _CapturingNotifier()
    state = _env_gate_state(_GitHubWithRollup(payload))
    state["notification_service"] = notifier

    result = await monitor_performer(state)

    assert result["latest_ci_gate_decision"]["verdict"] == "hold"
    assert len(notifier.events) == 1
    ev = notifier.events[0]
    # The notification identity uses job/error signatures across unrelated heads.
    assert ev.payload["pattern_id"] == "artifact_storage_quota+runner_offline"
    assert ev.dedup_key == "env_blocked:" + "|".join(sorted(c["head_signature"] for c in result["latest_ci_gate_decision"]["env_blocked_checks"]))
    # both causes named, not just the first
    assert "storage" in ev.payload["cause"].lower()
    assert "runner" in ev.payload["cause"].lower()
    # persisted env_blocked carries the same composite pattern id
    assert result["env_blocked"]["pattern_id"] == "artifact_storage_quota+runner_offline"


@pytest.mark.asyncio
async def test_env_blocked_mixed_with_other_failure_holds_without_crash() -> None:
    """095 (FR-009 regression): a card with BOTH an env_blocked check AND a
    non-infra failure must HOLD without the CIGateDecision validator raising
    (the hold carries only env_blocked_checks, never the inherited/introduced
    lists which a hold verdict forbids alongside empty failed_checks)."""
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "Build Pull Request",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "artifact storage quota has been hit"},
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "2 examples, 1 failure"},
        ],
    )
    import structlog.testing

    gh = _GitHubWithRollup(payload)
    state = _env_gate_state(gh)

    # Must not raise (pre-fix this crashed in CIGateDecision validation).
    with structlog.testing.capture_logs() as cap_logs:
        result = await monitor_performer(state)

    decision = result["latest_ci_gate_decision"]
    assert decision["verdict"] == "hold"
    assert "Build Pull Request" in [c["name"] for c in decision.get("env_blocked_checks", [])]
    # The hold decision does not carry the L2 lists (validator constraint).
    assert decision.get("inherited_checks", []) == []
    assert decision.get("introduced_checks", []) == []
    # FR-009: the operator event still distinguishes the OTHER failing check so
    # the non-infra failure isn't masked by the infra hold.
    hold = next(e for e in cap_logs if e["event"] == "ci_gate.env_blocked_hold")
    assert "unit-tests" in hold["other_failed"]


@pytest.mark.asyncio
async def test_env_blocked_state_cleared_on_resume() -> None:
    """095 (FR-008): when a later evaluation is NOT an env-hold (here: all green
    → PASS), the stale env_blocked dedup state is cleared, so a future recurrence
    of the same (head, pattern) re-notifies rather than being suppressed."""
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "Build Pull Request",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _env_gate_state(gh)
    # Stale hold state from a prior cycle.
    state["env_blocked"] = {"head_sha": "a" * 40, "pattern_id": "artifact_storage_quota"}

    result = await monitor_performer(state)

    assert result["latest_ci_gate_decision"]["verdict"] == "pass"
    assert result.get("env_blocked") is None


@pytest.mark.asyncio
async def test_env_on_l2_off_does_not_attach_l2_lists() -> None:
    """095 (SC-006): with the env gate ON but the L2 classification gate OFF, a
    non-infra failure must NOT populate the inherited/introduced lists on the
    decision — those stay byte-identical to the pre-L2 baseline."""
    from coordinare.config import CIGateConfig, EnvBlockedGateConfig, PersonaScopeConfig

    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "2 examples, 1 failure"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state(gh)

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=True, max_bounces_per_head=3),
            env_blocked_gate=EnvBlockedGateConfig(enabled=True),  # env ON, L2 OFF
        )

    state["symphony_configs"] = {"default": _Sym()}

    result = await monitor_performer(state)
    decision = result["latest_ci_gate_decision"]
    assert decision["verdict"] == "bounce"
    # L2 off → no L2 classification lists on the decision.
    assert decision.get("inherited_checks", []) == []
    assert decision.get("introduced_checks", []) == []


@pytest.mark.asyncio
async def test_env_only_mixed_surfaces_other_failure_and_clears_dispatch() -> None:
    """095 round-4 regressions: in ENV-ONLY mode (L2 off) a mixed card must still
    (a) surface the non-env failure on the operator event (FR-009, robust w/o L2)
    and (b) clear agent_dispatch so neither ephemeral nor persistent performers
    are re-polled (FR-004)."""
    import structlog.testing

    from coordinare.config import CIGateConfig, EnvBlockedGateConfig, PersonaScopeConfig

    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "Build Pull Request",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "artifact storage quota has been hit"},
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "2 examples, 1 failure"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state(gh)

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=True, max_bounces_per_head=3),
            env_blocked_gate=EnvBlockedGateConfig(enabled=True),  # env ON, L2 OFF
        )

    state["symphony_configs"] = {"default": _Sym()}

    with structlog.testing.capture_logs() as cap_logs:
        result = await monitor_performer(state)

    assert result["latest_ci_gate_decision"]["verdict"] == "hold"
    hold = next(e for e in cap_logs if e["event"] == "ci_gate.env_blocked_hold")
    # FR-009 even with L2 off: the non-env failure is on the event.
    assert "unit-tests" in hold["other_failed"]
    # FR-004: dispatch cleared so no re-poll/re-dispatch.
    assert result.get("agent_dispatch") == {}


@pytest.mark.asyncio
async def test_env_blocked_gate_off_yields_no_env_classification() -> None:
    """FR-012/SC-006: with the env gate off, an infra-reason failure is NOT
    labeled env_blocked (it classifies by the existing rules)."""
    payload = _rollup_payload(
        contexts=[
            {
                "__typename": "CheckRun",
                "name": "Build Pull Request",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "title": "artifact storage quota has been hit",
            },
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state(gh)  # only ci_gate enabled; env + L2 off

    result = await monitor_performer(state)
    decision = result["latest_ci_gate_decision"]
    assert decision.get("env_blocked_checks", []) == []


@pytest.mark.asyncio
async def test_env_blocked_gate_off_dispatches_no_operator_notification() -> None:
    """095 (T016/FR-012/SC-006): with the gate off, NO env_blocked state is set
    and NO distinct operator notification fires — behaviour is pre-feature."""
    class _CapturingNotifier:
        def __init__(self) -> None:
            self.events: list = []

        async def dispatch(self, event):
            self.events.append(event)

    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "Build Pull Request",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "artifact storage quota has been hit"},
        ],
    )
    notifier = _CapturingNotifier()
    state = _ci_gate_state(_GitHubWithRollup(payload))  # env gate off
    state["notification_service"] = notifier

    result = await monitor_performer(state)
    assert result.get("env_blocked") is None
    assert notifier.events == []


@pytest.mark.asyncio
async def test_env_blocked_operator_resume_does_not_bypass_shared_cooldown() -> None:
    """095 (T017/FR-004/FR-006): a flapping infra block — block (notify once),
    operator resumes (env_blocked cleared), then the SAME block recurs — stays within the shared infrastructure cooldown (263)."""
    from coordinare.models.notification import EventType

    class _CapturingNotifier:
        def __init__(self) -> None:
            self.events: list = []

        async def dispatch(self, event):
            self.events.append(event)

    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "Build Pull Request",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "artifact storage quota has been hit"},
        ],
    )
    notifier = _CapturingNotifier()
    state = _env_gate_state(_GitHubWithRollup(payload))
    state["notification_service"] = notifier

    # 1) First block → one notification, env_blocked persisted.
    r1 = await monitor_performer(state)
    assert len(notifier.events) == 1
    assert r1.get("env_blocked", {}).get("pattern_id") == "artifact_storage_quota"

    # 2) Condition cleared (operator acted) — dedup state reset to None.
    state["env_blocked"] = None

    # 3) The same unresolved incident stays quiet within its cooldown.
    await monitor_performer(state)
    assert len(notifier.events) == 1
    assert notifier.events[-1].event_type == EventType.env_blocked


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


@pytest.mark.asyncio
async def test_env_blocked_on_non_required_check_does_not_hold() -> None:
    """095 (T017): an infra signature on a NON-required check must NOT HOLD the
    card — ENV_BLOCKED is scoped to required checks, just like bounce. The
    required set is green, so the gate PASSes and surfaces the infra failure as
    advisory rather than blocking on it."""
    from coordinare.config import EnvBlockedGateConfig

    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "lint",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "integration",
             "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "artifact storage quota has been hit"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state_with_check_map(gh)
    # Turn the env gate ON without changing the required set ({lint}).
    state["symphony_configs"]["default"].persona_scope.env_blocked_gate = (
        EnvBlockedGateConfig(enabled=True)
    )

    result = await monitor_performer(state)

    decision = result["latest_ci_gate_decision"]
    assert decision["verdict"] == "pass"
    assert decision.get("env_blocked_checks", []) == []
    assert result.get("env_blocked") is None
    # The (non-required) infra failure is still surfaced as advisory.
    advisory = result.get("ci_gate_advisory_failures") or []
    assert any(a.get("name") == "integration" for a in advisory)


# ---------------------------------------------------------------------------
# 077: ephemeral-implementer CI-gate HOLD must not strand the card.
#
# Regression for the live #93 block: an ephemeral implementer succeeded, the gate
# HELD on a pending check, the one-shot container tore down, and the next cycle
# re-polled the dead session → ephemeral_job_lookup_miss → false transport-error
# → max_retries → BLOCKED. The fix: a gone ephemeral session clears agent_dispatch
# on HOLD, and the next cycle re-evaluates the gate directly instead of re-polling.
# ---------------------------------------------------------------------------


class _EphemeralPerformer:
    """Ephemeral performer whose one-shot session is gone after terminal success.

    ``check_status`` raises a TransportError when polled with ``raise_on_poll`` —
    that is exactly the lookup-miss the fix must avoid on HOLD re-entry, so tests
    assert it is *never* called.
    """

    def __init__(
        self,
        response: dict | None = None,
        *,
        live: bool = False,
        raise_on_poll: bool = False,
        die_after_poll: bool = False,
    ) -> None:
        self._response = response or {}
        self._live = live
        self._raise = raise_on_poll
        # Model the real ephemeral teardown: check_status cleans up _active_jobs
        # on a terminal result, so has_live_session flips True->False mid-turn.
        self._die_after_poll = die_after_poll
        self.check_status_calls = 0

    def has_live_session(self, session_id: str) -> bool:
        _ = session_id
        return self._live

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        self.check_status_calls += 1
        if self._raise:
            from coordinare.transport.base import TransportError

            raise TransportError(
                "performer codex-ephemeral has no endpoint resolved yet"
            )
        if self._die_after_poll:
            self._live = False  # terminal cleanup tore the one-shot container down
        return self._response


def _held_reentry_state(github: object, performer: object) -> dict:
    """State on the cycle AFTER an ephemeral implementer HELD: agent_dispatch is
    already cleared, the prior HOLD decision is stashed, and the card carries the
    PR url that was persisted before the gate."""
    from coordinare.config import CIGateConfig, PersonaScopeConfig

    state = initial_state()
    state["performer_services"] = {"implementing": performer}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {
        "id": "ITEM_1",
        "status": "IN_PROGRESS",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_NODE_42",
    }
    state["agent_dispatch"] = {}  # cleared by the prior HOLD
    state["agent_dispatch_at"] = None
    state["latest_ci_gate_decision"] = {"verdict": "hold", "pending_checks": ["unit-tests"]}
    state["github_service"] = github
    state["bounce_counter"] = {}
    state["phase"] = "monitoring_performer"

    class _Sym:
        persona_scope = PersonaScopeConfig(ci_gate=CIGateConfig(enabled=True))

    state["current_symphony"] = "default"
    state["symphony_configs"] = {"default": _Sym()}
    return state


@pytest.mark.asyncio
async def test_ephemeral_hold_clears_dispatch_and_persists_pr() -> None:
    """First HOLD on an ephemeral (gone) implementer clears the stale session
    reference and persists the PR url, so the next cycle has no dead session to
    re-poll and a PR to re-gate on."""
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "IN_PROGRESS", "conclusion": None},
        ],
    )
    gh = _GitHubWithRollup(payload)
    state = _ci_gate_state(gh)
    # Replace the default performer with an ephemeral one that is live on entry
    # (so it is polled normally) but tears down on the terminal result — exactly
    # the transition that strands the HOLD path.
    perf = _EphemeralPerformer(
        {
            "status": "pr_opened",
            "pr_url": "https://github.com/org/repo/pull/42",
            "pr_node_id": "PR_NODE_42",
        },
        live=True,
        die_after_poll=True,
    )
    state["performer_services"] = {"implementing": perf}

    result = await monitor_performer(state)

    assert result["latest_ci_gate_decision"]["verdict"] == "hold"
    assert result["phase"] == "monitoring_performer"
    # Stale session reference cleared (the core fix) so _is_stale can't misfire.
    assert result["agent_dispatch"] == {}
    assert result["agent_dispatch_at"] is None
    # PR identifiers persisted to the card for the gate-only re-evaluation.
    assert result["current_card"]["pr_url"] == "https://github.com/org/repo/pull/42"


@pytest.mark.asyncio
async def test_ephemeral_hold_reentry_passes_without_repoll() -> None:
    """On HOLD re-entry with no live session, the gate is re-evaluated directly;
    a now-green gate advances to the reviewer WITHOUT re-polling the dead
    performer (no transport-error cascade)."""
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    perf = _EphemeralPerformer(raise_on_poll=True)  # raises if polled
    state = _held_reentry_state(gh, perf)

    result = await monitor_performer(state)

    # Advanced to the reviewer — the gate cleared on re-evaluation.
    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    # The dead ephemeral session was never re-polled.
    assert perf.check_status_calls == 0


@pytest.mark.asyncio
async def test_ephemeral_hold_reentry_keeps_holding_while_pending() -> None:
    """On HOLD re-entry with checks still pending, stay parked (no advance, no
    re-poll, no false transport error)."""
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "IN_PROGRESS", "conclusion": None},
        ],
    )
    gh = _GitHubWithRollup(payload)
    perf = _EphemeralPerformer(raise_on_poll=True)
    state = _held_reentry_state(gh, perf)

    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "monitoring_performer"
    assert result["latest_ci_gate_decision"]["verdict"] == "hold"
    assert perf.check_status_calls == 0
    assert result["agent_dispatch"] == {}


@pytest.mark.asyncio
async def test_ephemeral_hold_reentry_works_without_stashed_verdict() -> None:
    """Regression for the live #93 re-block: latest_ci_gate_decision does not
    reliably survive the multi-session state round-trip, so the re-evaluation
    branch must NOT depend on it. With no stashed verdict but a gone session +
    open PR + enabled gate, a green gate must still advance — not poll the dead
    session into a false transport-error block."""
    payload = _rollup_payload(
        contexts=[
            {"__typename": "CheckRun", "name": "unit-tests",
             "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    )
    gh = _GitHubWithRollup(payload)
    perf = _EphemeralPerformer(raise_on_poll=True)
    state = _held_reentry_state(gh, perf)
    # Simulate the persistence quirk: the prior HOLD verdict was lost.
    state["latest_ci_gate_decision"] = None

    result = await monitor_performer(state)

    assert result["performer_stage"] == "reviewing"
    assert result["phase"] == "dispatching"
    assert perf.check_status_calls == 0


@pytest.mark.asyncio
async def test_ephemeral_gone_implementer_without_pr_redispatches_cleanly() -> None:
    """A gone ephemeral implementer session with NO open PR (degraded snapshot,
    or the container died before pushing) must re-dispatch cleanly rather than
    poll the dead session into a false transport-error block."""
    gh = _GitHubWithRollup({})
    perf = _EphemeralPerformer(raise_on_poll=True)
    state = _held_reentry_state(gh, perf)
    state["current_card"].pop("pr_url", None)  # no PR to gate on
    state["current_card"].pop("pr_node_id", None)
    state["latest_ci_gate_decision"] = None

    result = await monitor_performer(state)

    # Clean re-dispatch — not a transport-error/system_error, no dead-session poll.
    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert result["agent_dispatch"] == {}
    assert result.get("system_error_count", 0) == 0
    assert perf.check_status_calls == 0


# ---------------------------------------------------------------------------
# 090-L2 (US2, T024): observe-only inherited/introduced/flake/unknown
# classification. The gate is opt-in (baseline_classification_gate.enabled);
# when enabled it attaches the four lists to the BOUNCE/ESCALATE decision while
# the verdict stays byte-identical to the gate-disabled path (FR-013, FR-014,
# SC-006).
# ---------------------------------------------------------------------------


def _check_run(
    name: str,
    conclusion: str,
    *,
    status: str = "COMPLETED",
    title: str | None = None,
    summary: str | None = None,
    details_url: str | None = None,
) -> dict:
    """A CheckRun rollup node, lifting title/summary into the F1 ``output`` block
    only when supplied (so a node without output matches the pre-090 shape).

    ``details_url`` sets the CheckRun ``detailsUrl`` (parsed into
    ``CheckEntry.details_url``) so a test can assert the L3 mandate's
    ``html_url`` origin; omitted → no key, so ``html_url`` resolves to ``None``.
    """
    node: dict = {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }
    if title is not None or summary is not None:
        # Real GraphQL shape: title/summary are top-level on CheckRun (no output{}).
        node["title"] = title
        node["summary"] = summary
    if details_url is not None:
        node["detailsUrl"] = details_url
    return node


def _base_rollup_payload(
    *,
    base_sha: str = "c" * 40,
    contexts: list[dict] | None = None,
) -> dict:
    """Base-branch (Ref→target→Commit) rollup payload, mirroring the shape
    ``parse_base_rollup`` consumes. Empty ``branchProtectionRules`` keeps the
    base index driven purely by check failure state, not required status."""
    return {
        "repository": {
            "ref": {
                "target": {
                    "oid": base_sha,
                    "statusCheckRollup": {
                        "state": "FAILURE",
                        "contexts": {"nodes": contexts or []},
                    },
                }
            },
            "branchProtectionRules": {"nodes": []},
        }
    }


class _GitHubWithBaseRollup:
    """GitHub double that discriminates the base-branch rollup query from the
    HEAD query so a single ``_execute`` can serve both fetches.

    Like ``_GitHubWithRollup`` it omits ``get_required_status_checks`` so the
    resolver falls back to layer-3 (all head checks required) — every failing
    head check therefore bounces and reaches the classifier.
    """

    def __init__(
        self,
        *,
        head_payload: dict | None = None,
        base_payload: dict | None = None,
        raise_on_base: bool = False,
    ) -> None:
        self.move_calls: list[tuple[str, str]] = []
        self._head_payload = head_payload
        self._base_payload = base_payload
        self._raise_on_base = raise_on_base

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))

    async def _execute(self, query: str, variables: dict) -> dict:
        if "BaseCheckRollup" in query:
            if self._raise_on_base:
                raise RuntimeError("base rollup graphql exploded")
            return self._base_payload or {}
        return self._head_payload or {}


def _classification_gate_state(
    github: object,
    *,
    enabled: bool = True,
    bounce_counter: dict[str, int] | None = None,
) -> dict:
    """`_ci_gate_state` with the 090-L2 classification gate toggled on/off.

    The CI gate itself stays enabled (so a red required check bounces); only the
    observe-only classifier is gated by ``enabled``.
    """
    from coordinare.config import (
        BaselineClassificationGateConfig,
        CIGateConfig,
        PersonaScopeConfig,
    )

    state = _ci_gate_state(github, bounce_counter=bounce_counter)

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=True, max_bounces_per_head=3),
            baseline_classification_gate=BaselineClassificationGateConfig(enabled=enabled),
        )

    state["symphony_configs"] = {"default": _Sym()}
    return state


@pytest.mark.asyncio
async def test_classification_labels_inherited_and_introduced_on_bounce() -> None:
    """A same-reason base+head failure → INHERITED; a head-only failure →
    INTRODUCED. The labels ride on the BOUNCE decision and are emitted to
    observability (FR-013)."""
    from structlog.testing import capture_logs

    head = _rollup_payload(
        contexts=[
            _check_run("lint", "FAILURE", title="ruff E501", summary="line too long"),
            _check_run("new-test", "FAILURE"),
        ],
    )
    base = _base_rollup_payload(
        contexts=[
            _check_run("lint", "FAILURE", title="ruff E501", summary="line too long"),
        ],
    )
    gh = _GitHubWithBaseRollup(head_payload=head, base_payload=base)
    state = _classification_gate_state(gh)

    with capture_logs() as logs:
        result = await monitor_performer(state)

    dec = result["latest_ci_gate_decision"]
    assert dec["verdict"] == "bounce"
    assert {c["name"] for c in dec["inherited_checks"]} == {"lint"}
    assert {c["name"] for c in dec["introduced_checks"]} == {"new-test"}
    assert dec["flake_checks"] == []
    assert dec["unknown_checks"] == []

    # INHERITED carries a baseline signature equal to the head signature (match).
    lint_fc = next(c for c in dec["inherited_checks"] if c["name"] == "lint")
    assert lint_fc["baseline_signature"] == lint_fc["head_signature"]
    # INTRODUCED has no baseline counterpart.
    new_fc = next(c for c in dec["introduced_checks"] if c["name"] == "new-test")
    assert new_fc["baseline_signature"] is None

    # FR-013: the classification is emitted to observability.
    assert "ci_gate.classified" in {e.get("event") for e in logs}


@pytest.mark.asyncio
async def test_classification_verdict_byte_identical_to_disabled() -> None:
    """The classifier is strictly additive: with it enabled the verdict and all
    routing fields match the gate-disabled path exactly; only the four lists
    differ (FR-014, SC-006)."""
    head_contexts = [
        _check_run("lint", "FAILURE", title="ruff E501", summary="line too long"),
    ]

    # Disabled (pre-090) path.
    d_gh = _GitHubWithRollup(_rollup_payload(contexts=head_contexts))
    d_state = _ci_gate_state(d_gh)
    d_result = await monitor_performer(d_state)
    d_dec = d_result["latest_ci_gate_decision"]

    # Enabled path with a matching base (so lint classifies INHERITED).
    e_gh = _GitHubWithBaseRollup(
        head_payload=_rollup_payload(contexts=head_contexts),
        base_payload=_base_rollup_payload(contexts=head_contexts),
    )
    e_state = _classification_gate_state(e_gh)
    e_result = await monitor_performer(e_state)
    e_dec = e_result["latest_ci_gate_decision"]

    # Verdict + routing fields byte-identical.
    for key in ("verdict", "required_checks", "failed_checks", "bounce_count_after",
                "resolver_source"):
        assert d_dec[key] == e_dec[key], key
    assert d_result["performer_stage"] == e_result["performer_stage"]
    assert d_result["phase"] == e_result["phase"]

    # Disabled → all four lists empty; enabled → INHERITED populated.
    assert d_dec["inherited_checks"] == []
    assert d_dec["introduced_checks"] == []
    assert d_dec["flake_checks"] == []
    assert d_dec["unknown_checks"] == []
    assert {c["name"] for c in e_dec["inherited_checks"]} == {"lint"}


@pytest.mark.asyncio
async def test_classification_unfetchable_base_is_all_unknown() -> None:
    """An unfetchable base rollup → every head failure lands in unknown_checks,
    none INHERITED (FR-012)."""
    gh = _GitHubWithBaseRollup(
        head_payload=_rollup_payload(contexts=[_check_run("lint", "FAILURE")]),
        raise_on_base=True,
    )
    state = _classification_gate_state(gh)

    result = await monitor_performer(state)

    dec = result["latest_ci_gate_decision"]
    assert dec["verdict"] == "bounce"
    assert {c["name"] for c in dec["unknown_checks"]} == {"lint"}
    assert dec["inherited_checks"] == []
    assert dec["introduced_checks"] == []
    assert dec["flake_checks"] == []


@pytest.mark.asyncio
async def test_classification_disabled_attaches_no_lists() -> None:
    """Gate disabled → the decision carries no classification lists, identical to
    today (SC-006)."""
    gh = _GitHubWithRollup(_rollup_payload(contexts=[_check_run("lint", "FAILURE")]))
    state = _classification_gate_state(gh, enabled=False)

    result = await monitor_performer(state)

    dec = result["latest_ci_gate_decision"]
    assert dec["verdict"] == "bounce"
    assert dec["inherited_checks"] == []
    assert dec["introduced_checks"] == []
    assert dec["flake_checks"] == []
    assert dec["unknown_checks"] == []


# ---------------------------------------------------------------------------
# 090-L3 (US3, T028): opt-in autonomous repair mandate. When the inherited-
# repair gate is enabled, an INHERITED head failure with remaining budget rides
# a ``metadata["repair_mandate"]`` onto the bounce re-dispatch (FR-016, FR-017).
# The mandate is built ONLY from INHERITED checks (never INTRODUCED/FLAKE/
# UNKNOWN), carries reason-fidelity (each ``normalized_reason`` byte-equal to the
# canonical string the classifier hashed), and is strictly additive — absent at
# all flag defaults and whenever there is no INHERITED failure (SC-006).
#
# These are TDD-red against the T029 construction site (the BOUNCE path of
# ``_evaluate_ci_gate``); the mandate surfaces as the top-level
# ``result["repair_mandate"]`` state update the dispatch node later threads into
# the JobInitPayload metadata.
# ---------------------------------------------------------------------------


def _inherited_repair_gate_state(
    github: object,
    *,
    enabled: bool = True,
    max_attempts: int = 1,
    inheritance_repair_counter: dict[str, int] | None = None,
) -> dict:
    """`_ci_gate_state` with BOTH the L2 classifier and the L3 repair gate on.

    L3 consumes the L2 INHERITED classification, so the classification gate must
    stay enabled for a mandate to be built. The L3 budget counter is seeded
    directly into flat state (it round-trips via the per-card session like
    ``bounce_counter``; the disk wiring lands in T034).
    """
    from coordinare.config import (
        BaselineClassificationGateConfig,
        CIGateConfig,
        InheritedRepairGateConfig,
        PersonaScopeConfig,
    )

    state = _ci_gate_state(github)
    state["inheritance_repair_counter"] = (
        inheritance_repair_counter if inheritance_repair_counter is not None else {}
    )

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=True, max_bounces_per_head=3),
            baseline_classification_gate=BaselineClassificationGateConfig(enabled=True),
            inherited_repair_gate=InheritedRepairGateConfig(
                enabled=enabled, max_repair_attempts_per_head=max_attempts
            ),
        )

    state["symphony_configs"] = {"default": _Sym()}
    return state


def _inherited_repair_head_base(
    *,
    lint_details_url: str | None = "https://github.com/org/repo/runs/9001",
) -> tuple[dict, dict]:
    """A head rollup with one INHERITED (``lint``, same reason on base) and one
    INTRODUCED (``new-test``, head-only) failure, plus the matching base rollup.
    """
    head = _rollup_payload(
        contexts=[
            _check_run(
                "lint",
                "FAILURE",
                title="ruff E501",
                summary="line too long",
                details_url=lint_details_url,
            ),
            _check_run("new-test", "FAILURE"),
        ],
    )
    base = _base_rollup_payload(
        contexts=[
            _check_run("lint", "FAILURE", title="ruff E501", summary="line too long"),
        ],
    )
    return head, base


@pytest.mark.asyncio
async def test_repair_mandate_built_for_inherited_on_bounce() -> None:
    """L3 enabled + an INHERITED failure with budget → a ``repair_mandate``
    matching the repair-dispatch Field Registry rides the bounce (FR-016)."""
    from coordinare.services import failure_signature

    head, base = _inherited_repair_head_base()
    gh = _GitHubWithBaseRollup(head_payload=head, base_payload=base)
    state = _inherited_repair_gate_state(gh)

    result = await monitor_performer(state)

    mandate = result["repair_mandate"]
    assert mandate["type"] == "baseline_repair"

    # Built ONLY from INHERITED checks — the INTRODUCED `new-test` is excluded.
    assert {c["name"] for c in mandate["inherited_checks"]} == {"lint"}
    lint = next(c for c in mandate["inherited_checks"] if c["name"] == "lint")
    assert lint["conclusion"] == "failure"
    assert set(lint) == {"name", "conclusion", "normalized_reason", "html_url"}

    # Reason-fidelity: byte-equal to the canonical string the classifier hashed
    # (recomputed on the head CheckEntry title/summary, NOT the raw title).
    assert lint["normalized_reason"] == failure_signature.normalize_reason(
        "ruff E501", "line too long"
    )

    # html_url origin = head CheckEntry.details_url.
    assert lint["html_url"] == "https://github.com/org/repo/runs/9001"

    # attempt = 1-based post-increment of a fresh per-head budget; the bumped
    # counter rides back on the same state update.
    head_sha = "a" * 40
    assert mandate["attempt"] == 1
    assert mandate["max_attempts"] == 1
    assert result["inheritance_repair_counter"][head_sha] == 1

    # Durable do-not-weaken instruction.
    instr = mandate["instruction"].lower()
    for forbidden in ("weaken", "skip", "xfail", "delete", "mock", "loosen"):
        assert forbidden in instr, forbidden

    # Additive-only: the bounce verdict + counter are unchanged by L3.
    assert result["phase"] == "dispatching"
    assert result["bounce_counter"][head_sha] == 1


@pytest.mark.asyncio
async def test_repair_mandate_max_attempts_reflects_config() -> None:
    """``max_attempts`` mirrors ``max_repair_attempts_per_head``; the first
    attempt on a fresh head is always 1 regardless of the ceiling."""
    head, base = _inherited_repair_head_base()
    gh = _GitHubWithBaseRollup(head_payload=head, base_payload=base)
    state = _inherited_repair_gate_state(gh, max_attempts=3)

    result = await monitor_performer(state)

    mandate = result["repair_mandate"]
    assert mandate["max_attempts"] == 3
    assert mandate["attempt"] == 1
    assert result["inheritance_repair_counter"]["a" * 40] == 1


@pytest.mark.asyncio
async def test_repair_mandate_html_url_null_when_check_omits_details() -> None:
    """An INHERITED check whose head CheckEntry carries no detailsUrl yields a
    ``null`` html_url (the field is not required; FR Field Registry)."""
    head, base = _inherited_repair_head_base(lint_details_url=None)
    gh = _GitHubWithBaseRollup(head_payload=head, base_payload=base)
    state = _inherited_repair_gate_state(gh)

    result = await monitor_performer(state)

    lint = next(c for c in result["repair_mandate"]["inherited_checks"] if c["name"] == "lint")
    assert lint["html_url"] is None


@pytest.mark.asyncio
async def test_repair_mandate_absent_when_l3_disabled() -> None:
    """SC-006: at the L3 flag default (disabled) no mandate is built and the
    budget counter is untouched, even though L2 still classifies INHERITED."""
    head, base = _inherited_repair_head_base()
    gh = _GitHubWithBaseRollup(head_payload=head, base_payload=base)
    state = _inherited_repair_gate_state(gh, enabled=False)

    result = await monitor_performer(state)

    # L2 still classifies (proving the mandate's absence is the L3 gate, not a
    # missing INHERITED), but no repair mandate / budget mutation occurs.
    assert {c["name"] for c in result["latest_ci_gate_decision"]["inherited_checks"]} == {"lint"}
    assert "repair_mandate" not in result
    assert result.get("inheritance_repair_counter", {}) == {}


@pytest.mark.asyncio
async def test_repair_mandate_absent_when_no_inherited_failure() -> None:
    """L3 enabled but every head failure is INTRODUCED (no base counterpart) →
    nothing to repair, so the mandate is absent and the budget untouched."""
    head = _rollup_payload(contexts=[_check_run("new-test", "FAILURE")])
    base = _base_rollup_payload(contexts=[])  # base is green for this check
    gh = _GitHubWithBaseRollup(head_payload=head, base_payload=base)
    state = _inherited_repair_gate_state(gh)

    result = await monitor_performer(state)

    dec = result["latest_ci_gate_decision"]
    assert {c["name"] for c in dec["introduced_checks"]} == {"new-test"}
    assert dec["inherited_checks"] == []
    assert "repair_mandate" not in result
    assert result.get("inheritance_repair_counter", {}) == {}


# ---------------------------------------------------------------------------
# 090-L3 (US3, T031/T032): the dual test-integrity guard on the candidate repair
# diff. After a repair mandate is dispatched (T034 appends a ``dispatch``
# RepairDecisionRecord), the re-dispatched implementer returns terminal success
# on the NEXT tick. Before the CI gate is consulted — a weakened test could turn
# the check green precisely BECAUSE the requirement was removed — the guard
# adjudicates the landed diff:
#
#   (1) static ``analyze_diff`` (hot path), then
#   (2) an independent ``diagnostic``-role adversarial reviewer with fresh
#       context (runs ONLY when the static half clears).
#
# Land-as-candidate only when BOTH clear (FR-021, SC-005 — never auto-merged,
# awaiting fresh human approval). Either veto — OR any uncertainty (diff fetch
# fails / reviewer errors) — REJECTS: ``phase="blocked"`` + an ``open_questions``
# escalation + a GitHub comment, with no push (FR-019, FR-020). The guard fails
# SAFE, unlike the fail-open L1/L2/CI gates.
#
# Gating is purely on the presence of a pending ``dispatch`` audit record, which
# can only exist when L3 was enabled — so at all flag defaults the guard is a
# no-op and behavior is byte-identical to pre-spec-090 (SC-006).
# ---------------------------------------------------------------------------


# A production-only diff: touches no test file, so the static guard clears it
# (the do-not-weaken mandate is about tests; production code is out of scope).
_PRODUCTION_DIFF = """\
diff --git a/src/coordinare/widget.py b/src/coordinare/widget.py
index 1234567..89abcde 100644
--- a/src/coordinare/widget.py
+++ b/src/coordinare/widget.py
@@ -1,3 +1,4 @@
 def answer() -> int:
-    return 41
+    # off-by-one fixed
+    return 42
"""

# A test-weakening diff: adds a skip marker to an existing test → static veto.
_SKIP_DIFF = """\
diff --git a/tests/unit/test_widget.py b/tests/unit/test_widget.py
index 1234567..89abcde 100644
--- a/tests/unit/test_widget.py
+++ b/tests/unit/test_widget.py
@@ -1,5 +1,6 @@
 import pytest

+@pytest.mark.skip(reason="flaky on CI")
 def test_answer():
     assert answer() == 42
"""


class _GitHubForRepairGuard:
    """GitHub double for the L3 dual guard: serves the candidate repair diff and
    records escalation/acceptance comments.

    Omits the rollup queries — the guard runs *before* the CI gate, which is
    disabled in these tests so the only adjudication on the terminal-success
    tick is the guard itself.
    """

    def __init__(self, *, pr_diff: str | None = None) -> None:
        self.move_calls: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self._pr_diff = pr_diff

    async def move_card(self, item_id: str, status: str) -> None:
        self.move_calls.append((item_id, status))

    async def add_comment(self, subject_id: str, body: str) -> dict:
        self.comments.append((subject_id, body))
        return {}

    async def get_pr_diff(self, pr_url: str) -> tuple[str, list[str]]:
        if self._pr_diff is None:
            raise RuntimeError("pr diff fetch exploded")
        return self._pr_diff, []


def _repair_guard_state(
    gh: object,
    *,
    ci_enabled: bool = False,
    head_sha: str = "a" * 40,
    attempt: int = 1,
    max_attempts: int = 1,
) -> dict:
    """`_ci_gate_state` seeded with a pending ``dispatch`` audit record so the
    guard adjudicates on this tick.

    The CI gate is disabled by default so the guard is the sole adjudicator: on
    ACCEPT the flow falls through to ``_advance_stage`` (implementing→reviewing),
    proving the repair lands as a candidate and is not auto-merged.
    """
    from coordinare.config import (
        BaselineClassificationGateConfig,
        CIGateConfig,
        InheritedRepairGateConfig,
        PersonaScopeConfig,
    )

    state = _ci_gate_state(gh, enabled=ci_enabled)
    state["inheritance_repair_counter"] = {head_sha: attempt}
    state["repair_audit"] = [
        {
            "head_sha": head_sha,
            "attempt": attempt,
            "kind": "dispatch",
            "is_safe": None,
            "flagged_patterns": [],
            "detail": None,
            "decided_at": "2026-06-14T00:00:00Z",
        }
    ]

    class _Sym:
        persona_scope = PersonaScopeConfig(
            ci_gate=CIGateConfig(enabled=ci_enabled, max_bounces_per_head=3),
            baseline_classification_gate=BaselineClassificationGateConfig(enabled=True),
            inherited_repair_gate=InheritedRepairGateConfig(
                enabled=True, max_repair_attempts_per_head=max_attempts
            ),
        )

    state["symphony_configs"] = {"default": _Sym()}
    return state


def _audit_kinds(result: dict) -> list[str]:
    return [r["kind"] for r in result.get("repair_audit", [])]


@pytest.mark.asyncio
async def test_repair_guard_clean_diff_lands_as_candidate_not_merged(monkeypatch) -> None:
    """A clean diff passing BOTH the static guard and the adversarial reviewer
    lands as a candidate on the active branch and is NOT auto-merged, awaiting
    fresh human approval (FR-021, SC-005)."""
    gh = _GitHubForRepairGuard(pr_diff=_PRODUCTION_DIFF)
    state = _repair_guard_state(gh)

    async def _reviewer_clears(**_):
        return True, ""

    monkeypatch.setattr(
        "coordinare.graph.nodes.monitor_performer._dispatch_repair_reviewer",
        _reviewer_clears,
    )

    result = await monitor_performer(state)

    # Not blocked: the repair landed and the flow advanced past implementing.
    assert result["phase"] != "blocked"
    assert result["performer_stage"] == "reviewing"
    # Never auto-merged — the board was not moved to a terminal column.
    assert gh.move_calls == []

    # Audit: dispatch → static_guard(clear) → reviewer(clear) → acceptance.
    assert _audit_kinds(result) == ["dispatch", "static_guard", "reviewer", "acceptance"]
    audit = result["repair_audit"]
    assert audit[1]["is_safe"] is True and audit[1]["flagged_patterns"] == []
    assert audit[2]["is_safe"] is True
    assert audit[3]["kind"] == "acceptance"

    # A single comment was posted restating the no-auto-merge / stale-approval
    # assumption (FR-021, SC-005).
    assert len(gh.comments) == 1
    body = gh.comments[0][1].lower()
    assert "approval" in body
    assert "merge" in body


@pytest.mark.asyncio
async def test_repair_guard_static_veto_rejects_and_escalates(monkeypatch) -> None:
    """A diff the static guard vetoes is rejected + escalated with no push, and
    the adversarial reviewer is NEVER consulted (static is the hot path;
    FR-020)."""
    gh = _GitHubForRepairGuard(pr_diff=_SKIP_DIFF)
    state = _repair_guard_state(gh)

    async def _reviewer_must_not_run(**_):
        raise AssertionError("reviewer must not run after a static veto")

    monkeypatch.setattr(
        "coordinare.graph.nodes.monitor_performer._dispatch_repair_reviewer",
        _reviewer_must_not_run,
    )

    result = await monitor_performer(state)

    # Rejected + escalated, no push.
    assert result["phase"] == "blocked"
    assert result["agent_dispatch"] == {}
    assert gh.move_calls == []

    # Audit ends at the static veto — no reviewer record.
    assert _audit_kinds(result) == ["dispatch", "static_guard", "rejection"]
    static_rec = result["repair_audit"][1]
    assert static_rec["is_safe"] is False
    assert static_rec["flagged_patterns"]  # non-empty reasons
    assert any("skip" in p.lower() for p in static_rec["flagged_patterns"])

    # Visible escalation: open_questions entry + a GitHub comment.
    assert result["open_questions"]
    assert len(gh.comments) == 1


@pytest.mark.asyncio
async def test_repair_guard_reviewer_veto_rejects_and_escalates(monkeypatch) -> None:
    """A diff the static guard clears but the adversarial reviewer vetoes is
    rejected + escalated (FR-019, FR-020 — either half vetoes)."""
    gh = _GitHubForRepairGuard(pr_diff=_PRODUCTION_DIFF)
    state = _repair_guard_state(gh)

    async def _reviewer_vetoes(**_):
        return False, "the fix masks the failure instead of addressing it"

    monkeypatch.setattr(
        "coordinare.graph.nodes.monitor_performer._dispatch_repair_reviewer",
        _reviewer_vetoes,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert gh.move_calls == []

    # static clears → reviewer runs and vetoes → rejection.
    assert _audit_kinds(result) == ["dispatch", "static_guard", "reviewer", "rejection"]
    audit = result["repair_audit"]
    assert audit[1]["is_safe"] is True
    assert audit[2]["is_safe"] is False
    assert "masks the failure" in (audit[3].get("detail") or "")

    assert result["open_questions"]
    assert len(gh.comments) == 1


@pytest.mark.asyncio
async def test_repair_guard_reviewer_uncertainty_rejects(monkeypatch) -> None:
    """Uncertainty from the reviewer half (it raises) rejects — the guard fails
    SAFE (FR-020)."""
    gh = _GitHubForRepairGuard(pr_diff=_PRODUCTION_DIFF)
    state = _repair_guard_state(gh)

    async def _reviewer_crashes(**_):
        raise RuntimeError("diagnostic performer unreachable")

    monkeypatch.setattr(
        "coordinare.graph.nodes.monitor_performer._dispatch_repair_reviewer",
        _reviewer_crashes,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert gh.move_calls == []
    # The reviewer record is recorded as a veto (is_safe False) before rejection.
    assert _audit_kinds(result) == ["dispatch", "static_guard", "reviewer", "rejection"]
    assert result["repair_audit"][2]["is_safe"] is False
    assert result["open_questions"]
    assert len(gh.comments) == 1


@pytest.mark.asyncio
async def test_repair_guard_diff_fetch_failure_rejects(monkeypatch) -> None:
    """Uncertainty from the static half (the diff cannot even be fetched)
    rejects — the guard fails SAFE and never reaches the reviewer (FR-020)."""
    gh = _GitHubForRepairGuard(pr_diff=None)  # get_pr_diff raises
    state = _repair_guard_state(gh)

    async def _reviewer_must_not_run(**_):
        raise AssertionError("reviewer must not run when the diff is unavailable")

    monkeypatch.setattr(
        "coordinare.graph.nodes.monitor_performer._dispatch_repair_reviewer",
        _reviewer_must_not_run,
    )

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert gh.move_calls == []
    # No static_guard/reviewer record — rejection straight after dispatch.
    assert _audit_kinds(result) == ["dispatch", "rejection"]
    assert result["open_questions"]
    assert len(gh.comments) == 1


# ---------------------------------------------------------------------------
# 090-L3 (US3, T033): per-head repair budget + escalation + audit trail.
#
# ``max_repair_attempts_per_head`` caps autonomous repair on a single head. The
# budget is incremented AT dispatch (FR-018) and round-trips on the per-card
# session (mirroring ``bounce_counter``). When an INHERITED failure remains red
# but the budget is fully consumed, the gate escalates for human repair rather
# than looping a fresh attempt: ``phase="blocked"`` + an ``open_questions`` entry
# + a GitHub comment + an ``escalation`` ``RepairDecisionRecord`` (FR-022,
# FR-024, SC-007, FR-023). A budget of zero (classify-but-never-dispatch) and the
# L3-disabled default both fall through to a plain bounce with an empty audit,
# keeping behavior byte-identical to pre-spec-090 (SC-006).
# ---------------------------------------------------------------------------


class _GitHubBaseRollupWithComments(_GitHubWithBaseRollup):
    """``_GitHubWithBaseRollup`` plus comment capture, for the budget-exhaustion
    escalation path which posts a PR comment the bare base-rollup double can't
    record."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.comments: list[tuple[str, str]] = []

    async def add_comment(self, subject_id: str, body: str) -> dict:
        self.comments.append((subject_id, body))
        return {}


@pytest.mark.asyncio
async def test_repair_budget_exhausted_escalates_instead_of_dispatching() -> None:
    """With ``max_repair_attempts_per_head: 1`` a second INHERITED failure on the
    same head (budget already consumed) does NOT dispatch — it escalates with
    ``phase="blocked"`` + an ``open_questions`` entry + a GitHub comment + an
    ``escalation`` audit record, and no fresh mandate (FR-022, FR-024, SC-007)."""
    head, base = _inherited_repair_head_base()
    gh = _GitHubBaseRollupWithComments(head_payload=head, base_payload=base)
    head_sha = "a" * 40
    # Budget already fully consumed for this head (one prior attempt, ceiling 1).
    state = _inherited_repair_gate_state(
        gh, max_attempts=1, inheritance_repair_counter={head_sha: 1}
    )

    result = await monitor_performer(state)

    # Escalated for human repair, not re-dispatched.
    assert result["phase"] == "blocked"
    assert result["agent_dispatch"] == {}
    assert "repair_mandate" not in result
    # The budget is NOT bumped past the ceiling on escalation.
    assert result["inheritance_repair_counter"][head_sha] == 1
    assert gh.move_calls == []

    # Visible escalation: an open_questions entry + exactly one PR comment naming
    # the exhausted budget.
    assert any("budget exhausted" in q.lower() for q in result["open_questions"])
    assert len(gh.comments) == 1
    assert "budget exhausted" in gh.comments[0][1].lower()

    # Audit: a single ``escalation`` record (no ``dispatch`` this tick — the
    # prior dispatch consumed the budget on an earlier tick).
    assert _audit_kinds(result) == ["escalation"]
    esc = result["repair_audit"][0]
    assert esc["head_sha"] == head_sha
    assert esc["attempt"] == 1
    assert "budget exhausted" in (esc["detail"] or "").lower()


@pytest.mark.asyncio
async def test_repair_budget_exhausted_appends_to_existing_audit() -> None:
    """The ``escalation`` record is appended after a prior, already-adjudicated
    attempt (dispatch → rejection), preserving the append-only audit order across
    ticks (FR-023).

    The trailing record must NOT be a pending ``dispatch`` — that routes to the
    guard, not the budget path — so the prior attempt carries its rejection."""
    head, base = _inherited_repair_head_base()
    gh = _GitHubBaseRollupWithComments(head_payload=head, base_payload=base)
    head_sha = "a" * 40
    state = _inherited_repair_gate_state(
        gh, max_attempts=1, inheritance_repair_counter={head_sha: 1}
    )
    # A prior, already-adjudicated attempt on the trail (dispatch then rejection).
    state["repair_audit"] = [
        {
            "head_sha": head_sha,
            "attempt": 1,
            "kind": "dispatch",
            "is_safe": None,
            "flagged_patterns": [],
            "detail": "Dispatched autonomous baseline-repair attempt 1/1.",
            "decided_at": "2026-06-14T00:00:00Z",
        },
        {
            "head_sha": head_sha,
            "attempt": 1,
            "kind": "rejection",
            "is_safe": False,
            "flagged_patterns": ["skip"],
            "detail": "Prior repair vetoed by the test-integrity guard.",
            "decided_at": "2026-06-14T00:01:00Z",
        },
    ]

    result = await monitor_performer(state)

    assert _audit_kinds(result) == ["dispatch", "rejection", "escalation"]


@pytest.mark.asyncio
async def test_repair_budget_zero_classifies_but_never_dispatches() -> None:
    """``max_repair_attempts_per_head: 0`` classifies INHERITED but never
    dispatches and never escalates — it falls through to a plain bounce with an
    empty audit, byte-identical to L3-off (SC-006)."""
    head, base = _inherited_repair_head_base()
    gh = _GitHubBaseRollupWithComments(head_payload=head, base_payload=base)
    state = _inherited_repair_gate_state(gh, max_attempts=0)

    result = await monitor_performer(state)

    # L2 still classifies the inherited failure...
    assert {c["name"] for c in result["latest_ci_gate_decision"]["inherited_checks"]} == {"lint"}
    # ...but L3 neither dispatches nor escalates.
    assert result["phase"] == "dispatching"
    assert result["latest_ci_gate_decision"]["verdict"] == "bounce"
    assert "repair_mandate" not in result
    assert result.get("inheritance_repair_counter", {}) == {}
    assert result.get("repair_audit", []) == []
    assert gh.comments == []


@pytest.mark.asyncio
async def test_repair_budget_fresh_for_new_head() -> None:
    """A budget consumed on one head does not block repair on a different head —
    the per-head counter starts fresh, so the new head dispatches at attempt 1."""
    head, base = _inherited_repair_head_base()
    gh = _GitHubWithBaseRollup(head_payload=head, base_payload=base)
    # A stale entry for a *different* head; the live head ("a"*40) is untouched.
    state = _inherited_repair_gate_state(
        gh, max_attempts=1, inheritance_repair_counter={"b" * 40: 1}
    )

    result = await monitor_performer(state)

    head_sha = "a" * 40
    mandate = result["repair_mandate"]
    assert mandate["attempt"] == 1
    assert result["inheritance_repair_counter"][head_sha] == 1
    # The stale other-head budget is preserved untouched.
    assert result["inheritance_repair_counter"]["b" * 40] == 1
    assert result["phase"] == "dispatching"
    assert _audit_kinds(result) == ["dispatch"]


@pytest.mark.asyncio
async def test_l3_disabled_leaves_repair_audit_empty() -> None:
    """SC-006: at the L3 flag default (disabled) no audit record is written even
    when L2 classifies an INHERITED failure — ``repair_audit`` stays empty and no
    escalation comment is posted."""
    head, base = _inherited_repair_head_base()
    gh = _GitHubBaseRollupWithComments(head_payload=head, base_payload=base)
    # Even with a "consumed" budget seeded, L3-off must not escalate.
    state = _inherited_repair_gate_state(
        gh, enabled=False, max_attempts=1, inheritance_repair_counter={"a" * 40: 1}
    )

    result = await monitor_performer(state)

    assert {c["name"] for c in result["latest_ci_gate_decision"]["inherited_checks"]} == {"lint"}
    assert result["phase"] == "dispatching"
    assert "repair_mandate" not in result
    assert result.get("repair_audit", []) == []
    assert gh.comments == []
