"""Unit tests for RequiredChecksResolver (spec 075).

US1 (T027): layer 3 (all_head_checks) only.
US3 (T040-T046): layers 1 and 2 — added incrementally below.
"""

from __future__ import annotations

from coordinare.services.required_checks_resolver import resolve

# ---------------------------------------------------------------------------
# T027 — Layer 3 (all_head_checks) fallback
# ---------------------------------------------------------------------------


def test_layer3_no_scope_no_bp_returns_all_head_sorted() -> None:
    out = resolve(all_head_checks=["unit-tests", "lint", "integration"])
    assert out == {
        "names": ["integration", "lint", "unit-tests"],
        "source": "all_head_checks",
    }


def test_layer3_empty_head_returns_empty() -> None:
    out = resolve(all_head_checks=[])
    assert out == {"names": [], "source": "all_head_checks"}


def test_layer3_dedup_input_set() -> None:
    out = resolve(all_head_checks=["lint", "lint", "lint"])
    assert out == {"names": ["lint"], "source": "all_head_checks"}


def test_scope_present_but_no_check_map_falls_through_to_layer3() -> None:
    scope = {"personas": {"implementer": {"depth": "normal"}}}
    out = resolve(scope=scope, all_head_checks=["lint", "unit"])
    assert out["source"] == "all_head_checks"


def test_check_map_present_but_no_scope_falls_through() -> None:
    check_map = {"implementer": {"normal": ["lint*"]}}
    out = resolve(persona_check_map=check_map, all_head_checks=["lint", "unit"])
    assert out["source"] == "all_head_checks"


# ---------------------------------------------------------------------------
# T040-T045 — Layers 1 & 2 (US3)
# ---------------------------------------------------------------------------


def test_resolver_layer1_persona_check_map() -> None:
    scope = {"personas": {"implementer": {"depth": "normal"}}}
    check_map = {"implementer": {"normal": ["lint*", "unit*"]}}
    out = resolve(
        scope=scope,
        persona_check_map=check_map,
        all_head_checks=["lint", "unit-tests", "integration"],
    )
    assert out == {
        "names": ["lint", "unit-tests"],
        "source": "persona_check_map",
    }


def test_resolver_layer1_empty_intersection_falls_through() -> None:
    """Patterns match no HEAD checks → fall through to layer 2."""
    scope = {"personas": {"implementer": {"depth": "normal"}}}
    check_map = {"implementer": {"normal": ["nomatch*"]}}
    bp = {"lint", "unit-tests"}
    out = resolve(
        scope=scope,
        persona_check_map=check_map,
        branch_protection_set=bp,
        all_head_checks=["lint", "unit-tests", "integration"],
    )
    assert out == {
        "names": ["lint", "unit-tests"],
        "source": "branch_protection",
    }


# ---------------------------------------------------------------------------
# 077 — Layer 1 depth-agnostic `any` (decoupled from 074 scope tiering)
# ---------------------------------------------------------------------------


def test_resolver_layer1_any_used_without_scope() -> None:
    """077 decoupling: with no 074 scope, the depth-agnostic `any` list scopes
    the gate (persona_check_map usable standalone)."""
    check_map = {"implementer": {"any": ["lint*", "unit*"]}}
    out = resolve(
        persona_check_map=check_map,
        all_head_checks=["lint", "unit-tests", "integration"],
    )
    assert out == {"names": ["lint", "unit-tests"], "source": "persona_check_map"}


def test_resolver_layer1_any_fallback_when_depth_list_empty() -> None:
    """Scope present but the depth-specific list is empty → fall back to `any`."""
    scope = {"personas": {"implementer": {"depth": "full"}}}
    check_map = {"implementer": {"any": ["lint*"], "full": []}}
    out = resolve(
        scope=scope,
        persona_check_map=check_map,
        all_head_checks=["lint", "unit-tests"],
    )
    assert out == {"names": ["lint"], "source": "persona_check_map"}


def test_resolver_layer1_depth_specific_takes_precedence_over_any() -> None:
    """When both a depth list and `any` are set, the depth list wins."""
    scope = {"personas": {"implementer": {"depth": "full"}}}
    check_map = {"implementer": {"any": ["lint*"], "full": ["unit*"]}}
    out = resolve(
        scope=scope,
        persona_check_map=check_map,
        all_head_checks=["lint", "unit-tests"],
    )
    assert out == {"names": ["unit-tests"], "source": "persona_check_map"}


def test_resolver_layer1_any_empty_intersection_falls_through() -> None:
    """`any` patterns match no HEAD check → fall through to branch protection."""
    check_map = {"implementer": {"any": ["nomatch*"]}}
    bp = {"lint"}
    out = resolve(
        persona_check_map=check_map,
        branch_protection_set=bp,
        all_head_checks=["lint", "unit-tests"],
    )
    assert out == {"names": ["lint"], "source": "branch_protection"}


def test_resolver_layer2_branch_protection() -> None:
    bp = {"lint", "unit-tests"}
    out = resolve(
        branch_protection_set=bp,
        all_head_checks=["lint", "unit-tests", "integration"],
    )
    assert out == {
        "names": ["lint", "unit-tests"],
        "source": "branch_protection",
    }


def test_resolver_layer2_unreported_bp_checks_dropped() -> None:
    """Branch-protection required check not yet on HEAD → dropped (by design).

    When a branch-protection rule lists a check that hasn't reported on HEAD
    yet (e.g. its runner hasn't been queued), the intersection is empty and the
    resolver returns an empty required-checks list from branch_protection.
    This is intentional fail-open behaviour: the gate should not hold a card
    for a check that hasn't started running.  See the inline comment in
    required_checks_resolver.py for the full rationale.
    """
    bp = {"lint"}
    out = resolve(
        branch_protection_set=bp,
        all_head_checks=["unit-tests"],  # "lint" not on HEAD yet
    )
    assert out == {"names": [], "source": "branch_protection"}


def test_resolver_layer3_all_checks_fallback() -> None:
    """Both prior layers empty/absent → fall through to all_head_checks."""
    out = resolve(all_head_checks=["lint", "unit-tests"])
    assert out["source"] == "all_head_checks"
    assert out["names"] == ["lint", "unit-tests"]


def test_resolver_persona_absent_from_map() -> None:
    """Persona name not in check_map → falls through to layer 2."""
    scope = {"personas": {"implementer": {"depth": "normal"}}}
    check_map = {"reviewer": {"normal": ["lint*"]}}
    bp = {"unit-tests"}
    out = resolve(
        scope=scope,
        persona_check_map=check_map,
        branch_protection_set=bp,
        all_head_checks=["lint", "unit-tests"],
    )
    assert out == {"names": ["unit-tests"], "source": "branch_protection"}


def test_resolver_glob_patterns() -> None:
    """`lint*` matches lint-py/lint-js; matrix-job glob matches build (3.11, …)."""
    scope = {"personas": {"implementer": {"depth": "full"}}}
    check_map = {
        "implementer": {
            "full": ["lint*", "build (3.11, *)"],
        },
    }
    out = resolve(
        scope=scope,
        persona_check_map=check_map,
        all_head_checks=[
            "lint",
            "lint-py",
            "lint-js",
            "build (3.11, ubuntu)",
            "build (3.10, ubuntu)",
            "integration",
        ],
    )
    assert out == {
        "names": ["build (3.11, ubuntu)", "lint", "lint-js", "lint-py"],
        "source": "persona_check_map",
    }
