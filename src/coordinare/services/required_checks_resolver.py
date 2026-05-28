"""Resolver for the implementer CI-gate required-checks set (spec 075).

3-layer chain (highest priority first):

1. ``persona_check_map`` ∩ ``PersonaScope.personas["implementer"].depth``
   — per-card narrowed set from 074's persona classification.
2. ``branch_protection`` — the GitHub branch-protection required-checks set.
3. ``all_head_checks`` — every check observed on HEAD (fallback / US1 MVP).

US1 lands layer 3 only.  US3 fills in layers 1 and 2.
"""

from __future__ import annotations

import fnmatch
from typing import Literal, TypedDict

ResolverSource = Literal["persona_check_map", "branch_protection", "all_head_checks"]


class RequiredChecksList(TypedDict):
    names: list[str]
    source: ResolverSource


def resolve(
    *,
    scope: dict | None = None,
    branch_protection_set: set[str] | None = None,
    all_head_checks: list[str] | set[str],
    persona_check_map: dict | None = None,
) -> RequiredChecksList:
    """Resolve the required-check names set for this HEAD.

    US1 short-circuits to layer 3 (all_head_checks).  When 074's scope and a
    populated persona_check_map are both present, US3 narrows to layer 1.
    Otherwise, if branch_protection_set is non-None, US3 falls through to
    layer 2.  All-head-checks is the terminal fallback.
    """
    head_names = sorted({c for c in all_head_checks})

    # Layer 1: persona_check_map narrows when scope + map both exist.
    if scope and persona_check_map:
        persona = (scope.get("personas") or {}).get("implementer") or {}
        depth = persona.get("depth")
        if depth and depth != "skip":
            per_persona = persona_check_map.get("implementer") or {}
            patterns = per_persona.get(depth) or []
            if patterns:
                matched = sorted(
                    {n for n in head_names if any(fnmatch.fnmatchcase(n, p) for p in patterns)}
                )
                # T041: empty intersection falls through to layer 2 so a
                # narrow persona-scope pattern doesn't silently zero out the
                # required-checks set when no observed check matches.
                if matched:
                    return {"names": matched, "source": "persona_check_map"}

    # Layer 2: branch-protection set (intersected with HEAD checks so we never
    # gate on a name that didn't report).
    # By design: a branch-protection required check that has not yet appeared
    # on HEAD (i.e. its runner hasn't been queued yet) is excluded from the
    # returned set.  This preserves fail-open semantics — the gate will not
    # artificially hold a card for a check that hasn't started.  Once the check
    # reports on HEAD it will appear in all_head_checks and be included.
    if branch_protection_set is not None:
        matched = sorted(branch_protection_set & set(head_names))
        return {"names": matched, "source": "branch_protection"}

    # Layer 3: fallback to all observed checks.
    return {"names": head_names, "source": "all_head_checks"}


__all__ = ["RequiredChecksList", "ResolverSource", "resolve"]
