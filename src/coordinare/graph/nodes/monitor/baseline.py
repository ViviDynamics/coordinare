"""Baseline failure classification for the L2 observe-only gate (435, 090)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services.ci_gate import FailedCheck, FailedCheckWithSignature
from coordinare.services.failure_classification import BaselineFailure, classify_failure_origin
from coordinare.services.failure_signature import make_failure_signature
from coordinare.services.pr_checks_policy import _is_failure

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
    from coordinare.services.pr_checks_service import CheckRollup, PrChecksService


from coordinare.graph.nodes.monitor.gate_config import (
    _get_baseline_classification_gate_config,
    _get_env_blocked_gate_config,
)

logger = structlog.get_logger(__name__)


def _build_baseline_index(
    base_rollup: CheckRollup | None,
) -> dict[str, BaselineFailure] | None:
    """Index the merge-base baseline's *failing* checks by name (090-L2).

    Returns ``None`` when the base rollup is unavailable (unfetchable /
    indeterminate) so ``classify_failure_origin`` routes every HEAD failure to
    UNKNOWN rather than INHERITED (FR-012).  A fetched base with zero failures
    yields an **empty dict** — a distinct sentinel that lets same-name HEAD
    failures classify INTRODUCED.  The two must never be conflated.
    """
    if base_rollup is None:
        return None
    index: dict[str, BaselineFailure] = {}
    for entry in base_rollup.checks:
        if not _is_failure(entry):
            continue
        conclusion = entry.conclusion or "failure"
        signature, normalized = make_failure_signature(
            entry.name, conclusion, entry.title, entry.summary,
        )
        index[entry.name] = BaselineFailure(
            name=entry.name,
            conclusion=conclusion,
            signature=signature,
            normalized_reason=normalized,
        )
    return index


def _plain(fc: FailedCheckWithSignature) -> FailedCheck:
    """Drop the signature fields — FLAKE/UNKNOWN lists carry plain failures."""
    return FailedCheck(
        name=fc.name,
        conclusion=fc.conclusion,
        html_url=fc.html_url,
        last_log_line=fc.last_log_line,
    )


async def _classify_head_failures(
    *,
    state: CoordinareState,
    card_id: str,
    svc: PrChecksService,
    rollup: CheckRollup,
    failed_names: list[str],
    failed_conclusion: str,
    url_by_name: dict[str, str],
) -> dict[str, Any]:
    """Observe-only 090-L2 classification of each failing HEAD check.

    Returns the four classification lists as ``CIGateDecision`` kwargs, or an
    empty dict when the gate is disabled / unconfigured.  The entire body is
    wrapped in its own ``try/except`` returning ``{}`` so a classification
    failure can NEVER reach ``_evaluate_ci_gate``'s outer fail-open ``except``
    (which would turn a BOUNCE into a PASS).  Classification is strictly
    additive — it never influences the verdict or routing (FR-013, FR-014,
    SC-006).
    """
    cfg = _get_baseline_classification_gate_config(state)
    l2_on = cfg is not None and getattr(cfg, "enabled", False)
    # 095: ENV_BLOCKED classification is its own gate, independent of L2. Run the
    # classifier when EITHER gate is on; ``env_patterns is None`` keeps Row 0 off
    # (byte-identical) when the env gate is disabled.
    env_cfg = _get_env_blocked_gate_config(state)
    env_on = env_cfg is not None and getattr(env_cfg, "enabled", False)
    if not (l2_on or env_on):
        return {}
    env_patterns = list(getattr(env_cfg, "patterns", []) or []) if env_on else None
    try:
        base_rollup = await svc.get_base_branch_check_rollup(rollup.base_ref or "main")
        if env_on and base_rollup is not None:
            base_rollup = await svc.enrich_failure_evidence(base_rollup)
        baseline_index = _build_baseline_index(base_rollup)
        if env_on and hasattr(svc, "refresh_peer_rollups"):
            await svc.refresh_peer_rollups(rollup.pr_number)
        structural_causes: dict[str, Any] = {}
        head_by_name = {c.name: c for c in rollup.checks}

        inherited: list[FailedCheckWithSignature] = []
        introduced: list[FailedCheckWithSignature] = []
        flake: list[FailedCheck] = []
        unknown: list[FailedCheck] = []
        env_blocked: list[FailedCheckWithSignature] = []
        conclusion: str

        for name in failed_names:
            entry = head_by_name.get(name)
            if entry is not None and entry.conclusion is not None:
                # Real conclusion/output: a transient conclusion classifies
                # FLAKE (FR-010), so we must NOT substitute the gate's
                # overridden failed_conclusion here.
                conclusion = entry.conclusion
                title = entry.title
                summary = entry.summary
            else:
                # pending_timeout / missing entry: no real output to read, so
                # synthesize the gate's failed_conclusion ("timed_out" for a
                # pending timeout → FLAKE; "failure" otherwise).
                conclusion = failed_conclusion
                title = None
                summary = None
            head_sig, head_reason = make_failure_signature(
                name, conclusion, title, summary,
            )
            base_failure = baseline_index.get(name) if baseline_index else None
            fc = FailedCheckWithSignature(
                name=name,
                conclusion=conclusion,
                html_url=url_by_name.get(name) or None,
                head_signature=head_sig,
                baseline_signature=base_failure.signature if base_failure else None,
            )
            origin = classify_failure_origin(
                fc, head_reason, baseline_index, env_patterns=env_patterns,
            )
            if env_on:
                from coordinare.services.env_signature import EnvCause

                if entry is not None and getattr(entry, "setup_failure", False):
                    structural_causes[name] = EnvCause("ci_setup_failure", "CI failed during runner/platform setup",
                                                       "Repair runner setup or platform credentials, then rerun the failed job")
                elif head_reason and hasattr(svc, "unrelated_failure_seen") and svc.unrelated_failure_seen(rollup, name, head_sig) is True:
                    structural_causes[name] = EnvCause("ci_shared_failure", "The same CI failure occurs on an unrelated head",
                                                       "Inspect the shared runner/service or base-branch failure, then rerun CI")
                if name in structural_causes:
                    origin = "env_blocked"
            if origin == "env_blocked":
                env_blocked.append(fc)
            elif l2_on:
                # Only collect the L2 lists when the L2 gate is on. With env-only
                # (l2_on=False), a non-env failure stays unclassified so the
                # decision is byte-identical to the pre-L2 baseline (SC-006).
                if origin == "inherited":
                    inherited.append(fc)
                elif origin == "introduced":
                    introduced.append(fc)
                elif origin == "flake":
                    flake.append(_plain(fc))
                else:
                    unknown.append(_plain(fc))

        logger.info(
            "ci_gate.classified",
            card_id=card_id,
            pr=rollup.pr_number,
            head=rollup.head_sha[:7],
            inherited=[c.name for c in inherited],
            introduced=[c.name for c in introduced],
            flake=[c.name for c in flake],
            unknown=[c.name for c in unknown],
            env_blocked=[c.name for c in env_blocked],
            base_fetched=baseline_index is not None,
        )
        # L2 lists only when L2 is on; env_blocked_checks only when env gate is on.
        result: dict[str, Any] = {}
        if l2_on:
            result["inherited_checks"] = inherited
            result["introduced_checks"] = introduced
            result["flake_checks"] = flake
            result["unknown_checks"] = unknown
        if env_on:
            result["env_blocked_checks"] = env_blocked
            result["env_causes"] = structural_causes
        return result
    except Exception as exc:
        logger.warning(
            "ci_gate.classification_failed", card_id=card_id, error=str(exc),
        )

        return {}


def _get_persona_check_map(state: CoordinareState) -> dict[str, Any] | None:
    """Pull the configured persona_check_map for the active symphony (075 US3).

    ``PersonaCheckMapConfig`` is a Pydantic ``RootModel`` wrapping
    ``dict[str, PersonaCheckMapPerDepth]``; flatten to the plain dict the
    resolver expects.
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    cm = getattr(persona_scope_cfg, "persona_check_map", None)
    if cm is None:
        return None
    root = getattr(cm, "root", None)
    if not root:
        return None
    out: dict[str, dict[str, list[str]]] = {}
    for persona, per_depth in root.items():
        out[persona] = {
            # 077 FR-013: `any` is the depth-agnostic list the resolver uses
            # when no 074 scope/depth is present. Must be flattened through here
            # or the decoupled gate scoping silently no-ops (falls to layer 3).
            "any": list(getattr(per_depth, "any", []) or []),
            "skim": list(getattr(per_depth, "skim", []) or []),
            "normal": list(getattr(per_depth, "normal", []) or []),
            "full": list(getattr(per_depth, "full", []) or []),
        }
    return out


def _get_session_persona_scope(state: CoordinareState, card_id: str) -> dict[str, Any] | None:
    sessions = state.get("active_sessions") or {}
    sess = sessions.get(card_id)
    if not isinstance(sess, dict):
        return None
    scope = sess.get("persona_scope")
    return scope if isinstance(scope, dict) else None

