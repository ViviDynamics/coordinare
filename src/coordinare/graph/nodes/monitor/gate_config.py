"""Per-gate configuration resolution for the monitor (435)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState



def _get_closer_pr_checks_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's closer_pr_checks config (064).

    Returns the symphony's CloserPrChecksConfig if available, else None.

    Legacy single-symphony mode (no symphony_configs entry) intentionally
    returns None so the gate stays *off* until an operator opts in by
    configuring a symphony — gating remote checks is a behavior change that
    must not silently activate on upgrade.
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    return getattr(sym_cfg, "closer_pr_checks", None)


def _get_ci_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.ci_gate config (spec 075).

    Returns the ``CIGateConfig`` if available, else None.  Legacy
    single-symphony mode (no symphony_configs entry) returns None so the
    implementer CI gate stays off until an operator opts in.
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "ci_gate", None)


def _get_local_test_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.local_test_gate config (spec 089).

    Returns the ``LocalTestGateConfig`` if available, else None.  Used on the
    coordinare side to read the coordinare-only ``max_fix_attempts`` budget for
    the bounded local-test self-fix loop (US3).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "local_test_gate", None)


def _get_baseline_prevention_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.baseline_prevention_gate config.

    Spec 090 L1 (US1). Returns the ``BaselinePreventionGateConfig`` if available,
    else None. Legacy single-symphony mode (no symphony_configs entry) returns
    None so the L1 base-precondition gate stays off until an operator opts in —
    keeping merge decisions byte-identical to the pre-feature baseline (SC-006).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "baseline_prevention_gate", None)


def _get_baseline_classification_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.baseline_classification_gate config.

    Spec 090 L2 (US2). Returns the ``BaselineClassificationGateConfig`` if
    available, else None. Legacy single-symphony mode (no symphony_configs entry)
    returns None so the L2 observe-only classifier stays off until an operator
    opts in — keeping CI-gate decisions byte-identical to the pre-feature
    baseline (SC-006).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "baseline_classification_gate", None)


def _get_env_blocked_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.env_blocked_gate config (095).

    Returns the ``EnvBlockedGateConfig`` if available, else None — keeping
    ENV_BLOCKED classification off until an operator opts in (default-off,
    SC-006-equivalent).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "env_blocked_gate", None)


def _get_inherited_repair_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.inherited_repair_gate config.

    Spec 090 L3 (US3). Returns the ``InheritedRepairGateConfig`` if available,
    else None. Legacy single-symphony mode (no symphony_configs entry) returns
    None so autonomous baseline repair stays off until an operator opts in —
    keeping dispatch byte-identical to the pre-feature baseline (SC-006).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "inherited_repair_gate", None)

