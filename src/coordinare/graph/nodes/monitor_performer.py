"""Generic, role-agnostic performer monitor node (019-performer-lifecycle).

Replaces ``monitor_agent`` with a node that resolves the active service from
``performer_services[performer_stage]`` and contains **zero** role-specific
logic (FR-004).  Terminal success states trigger lifecycle advancement via
``_advance_stage``.  Error status sets ``phase="blocked"`` per FR-006
(changed from the legacy ``system_error`` routing in ``monitor_agent``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coordinare.graph.nodes.monitor.activity import (
    _record_activity,
    _record_activity_batch,
)
from coordinare.graph.nodes.monitor.artefacts import (
    _lift_review_findings,
    _lift_security_findings,
    _record_pr_artefacts,
)
from coordinare.graph.nodes.monitor.baseline import (
    _build_baseline_index,
)
from coordinare.graph.nodes.monitor.board import (
    _teardown_workspace,
)
from coordinare.graph.nodes.monitor.body import (
    _monitor_performer_body,
    logger,
)
from coordinare.graph.nodes.monitor.constants import (
    ABSOLUTE_CEILING_MULTIPLIER,
    EXPECTED_STAGE_MARKER,
    MAX_PERFORMER_EVENTS,
    TERMINAL_SUCCESS_STATES,
    VERDICT_STAGES,
)
from coordinare.graph.nodes.monitor.events import (
    _recent_event_text,
    merge_performer_events,
)
from coordinare.graph.nodes.monitor.gate_config import (
    _get_ci_gate_config,
)
from coordinare.graph.nodes.monitor.gates import (
    _evaluate_baseline_prevention_gate,
    _evaluate_pr_checks_gate,
    _reset_ci_gate_api_error_cooldown,
)
from coordinare.graph.nodes.monitor.ui import (
    _apply_assessor_decline,
    _refresh_backend_ui,
)
from coordinare.graph.nodes.monitor.verdict import (
    _FEEDBACK_DIGEST_MAX,
    _advance_stage,
    _apply_feedback_dispositions,
    _apply_pending_override,
    _feedback_cycle_exhausted,
    _record_stage_verdict,
    _resolve_dispute_round,
    _stamp_feedback_bounce,
    _summarise_feedback_items,
)

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


# aliased: this module already binds `persona` as a loop variable elsewhere, and
# a shadowed import is a silent wrong-value bug waiting to happen.


async def monitor_performer(state: CoordinareState) -> CoordinareState:
    """Poll the active performer, then surface any terminal outcome in the feed.

    138 T041: the body has ~15 separate terminal-error / blocked returns. One
    wrapper covers every one of them, where patching individual sites would
    leave the siblings silent.
    """
    result = await _monitor_performer_body(state)
    if isinstance(result, dict) and result.get("phase") in {"blocked", "system_error"}:
        reason = result.get("system_error_reason") or "no reason reported"
        _record_activity(
            result,
            "error",
            f"{result['phase']}: {reason}",
            card_id=str((result.get("current_card") or {}).get("id", "")),
            stage=str(result.get("performer_stage") or ""),
        )
    return result


__all__ = [
    "ABSOLUTE_CEILING_MULTIPLIER",
    "EXPECTED_STAGE_MARKER",
    "MAX_PERFORMER_EVENTS",
    "TERMINAL_SUCCESS_STATES",
    "VERDICT_STAGES",
    "_FEEDBACK_DIGEST_MAX",
    "_advance_stage",
    "_apply_assessor_decline",
    "_apply_feedback_dispositions",
    "_apply_pending_override",
    "_build_baseline_index",
    "_evaluate_baseline_prevention_gate",
    "_evaluate_pr_checks_gate",
    "_feedback_cycle_exhausted",
    "_get_ci_gate_config",
    "_lift_review_findings",
    "_lift_security_findings",
    "_recent_event_text",
    "_record_activity_batch",
    "_record_pr_artefacts",
    "_record_stage_verdict",
    "_refresh_backend_ui",
    "_reset_ci_gate_api_error_cooldown",
    "_resolve_dispute_round",
    "_stamp_feedback_bounce",
    "_summarise_feedback_items",
    "_teardown_workspace",
    "logger",
    "merge_performer_events",
]
