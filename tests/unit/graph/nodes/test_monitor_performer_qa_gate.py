"""Spec 120 US1: coordinare-side QA evidence-integrity gate in monitor_performer.

A self-reported ``qa_passed`` that verified zero criteria (or lacks required
visual evidence) must not advance: it HOLDs when an environment signal is present
and BOUNCEs otherwise. Legitimate passes advance unchanged.
"""

from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


class _EnvCacheSvc:
    def __init__(self) -> None:
        self.marked: list[str] = []

    def mark_runtime_health_failed(self, symphony: str, state: object) -> None:
        self.marked.append(symphony)


def _qa_state(response: dict, *, env_cache_svc: object | None = None) -> dict:
    state = initial_state()
    service = _Performer(response)
    state["performer_services"] = {"qa": service}
    state["performer_stage"] = "qa"
    state["lifecycle_sequence"] = ["implementing", "qa", "closing_review"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["current_symphony"] = "website"
    if env_cache_svc is not None:
        state["env_cache_service"] = env_cache_svc
    return state


def _qa_passed(report: dict, **extra) -> dict:
    base = {"status": "qa_passed", "report": report}
    base.update(extra)
    return base


# --- HOLD: zero criteria + environment signal -----------------------------


@pytest.mark.asyncio
async def test_zero_criteria_with_env_error_holds_not_advances():
    svc = _EnvCacheSvc()
    state = _qa_state(
        _qa_passed({
            "criteria_checked": 5,
            "criteria_passed": 0,
            "environment_error": "ruby runtime not installed",
        }),
        env_cache_svc=svc,
    )
    result = await monitor_performer(state)

    assert result["performer_stage"] == "qa"  # held on same stage, not advanced
    assert "qa_evidence_floor" in str(result.get("env_health_hold_reason", ""))
    assert svc.marked == ["website"]
    assert result.get("phase") == "dispatching"


@pytest.mark.asyncio
async def test_zero_criteria_with_health_failed_flag_holds():
    svc = _EnvCacheSvc()
    state = _qa_state(
        _qa_passed(
            {"criteria_checked": 3, "criteria_passed": 0},
            env_cache_health_failed=True,
        ),
        env_cache_svc=svc,
    )
    result = await monitor_performer(state)
    assert result["performer_stage"] == "qa"
    # Idempotent mark (sets a bool); pre-existing 063 wiring may also mark it.
    assert "website" in svc.marked


# --- BOUNCE: zero criteria, no environment signal -------------------------


@pytest.mark.asyncio
async def test_zero_criteria_without_env_signal_bounces_to_implementer():
    state = _qa_state(_qa_passed({"criteria_checked": 5, "criteria_passed": 0}))
    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"  # bounced for remediation
    assert result.get("phase") == "dispatching"
    relayed = result.get("relay_feedback") or []
    assert any(
        f.get("type") == "unsubstantiated_pass" for f in relayed if isinstance(f, dict)
    )


@pytest.mark.asyncio
async def test_visual_required_without_evidence_bounces():
    state = _qa_state(_qa_passed({
        "criteria_checked": 2,
        "criteria_passed": 2,
        "visual_validation_required": True,
        "visual_evidence": [],
    }))
    result = await monitor_performer(state)
    assert result["performer_stage"] == "implementing"


# --- ADVANCE: legitimate passes (no regression) ---------------------------


@pytest.mark.asyncio
async def test_legitimate_pass_advances():
    state = _qa_state(_qa_passed({
        "criteria_checked": 5,
        "criteria_passed": 4,
        "executed_checks": [{"command": "pytest", "exit_code": 0}],
    }))
    result = await monitor_performer(state)
    assert result["performer_stage"] == "closing_review"  # advanced past qa
    assert result.get("env_health_hold_reason") in (None, "")


@pytest.mark.asyncio
async def test_no_criteria_scope_advances():
    state = _qa_state(_qa_passed({
        "criteria_checked": 0,
        "criteria_passed": 0,
        "visual_validation_required": False,
    }))
    result = await monitor_performer(state)
    assert result["performer_stage"] == "closing_review"


@pytest.mark.asyncio
async def test_persists_app_boot_ok_from_report():
    # 120 (US3/FR-014): the screenshot backstop gate signal is set from the report.
    state = _qa_state(_qa_passed({
        "criteria_checked": 2,
        "criteria_passed": 2,
        "app_boot_check": {"command": "curl localhost:3000", "exit_code": 0},
    }))
    result = await monitor_performer(state)
    assert result.get("qa_app_boot_ok") is True


@pytest.mark.asyncio
async def test_persists_app_boot_not_ok_when_boot_failed():
    state = _qa_state(_qa_passed({
        "criteria_checked": 2,
        "criteria_passed": 2,
        "app_boot_check": {"command": "curl localhost:3000", "exit_code": 7},
    }))
    result = await monitor_performer(state)
    assert result.get("qa_app_boot_ok") is False


@pytest.mark.asyncio
async def test_visual_pass_with_screenshot_advances():
    state = _qa_state(_qa_passed({
        "criteria_checked": 3,
        "criteria_passed": 3,
        "visual_validation_required": True,
        "visual_evidence": [{"path_or_url": "artifacts/home.png"}],
    }))
    result = await monitor_performer(state)
    assert result["performer_stage"] == "closing_review"
