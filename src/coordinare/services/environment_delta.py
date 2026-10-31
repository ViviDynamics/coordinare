"""Delta repair: an execution repair re-authors only the closure of its failure, as changed and added records.

The failed accepted manifest is the base. A touched author returns the records it changes or adds; the daemon applies
them to the base by id, so every untouched record carries over unchanged. The result then passes the full admission
pipeline, including the closure guard. A patch never removes a record: an omitted base record is kept.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from coordinare.services.environment_delta_schema import RECORD_GROUPS
from coordinare.services.environment_obligations import frozen_records, part_records, refuse_frozen
from coordinare.services.environment_repair_triage import (
    ACTIVATION,
    GLOBAL,
    RepairTriage,
    depends_on_closure,
    part_of,
)
from coordinare.services.environment_shard_construction import (
    CheckGroup,
    global_view,
    owns,
    parts_from_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from coordinare_service_inference.environment.models import EnvironmentManifest

# Failures a delta repair does not correct in place: the full repair runs instead while budget remains.
DELTA_FALLBACK = frozenset({"attempt_schema_limit", "repair_delta_rejected", "executable_merge_invalid", "repair_triage_stale"})
# Proposal fields a manifest's standing review depends on; provenance and cache-only facts never change a judgment.
STANDING_EXCLUDED = frozenset({"manifest_digest", "probe_evidence", "untrusted_execution_history",
                               "execution_capabilities", "capabilities_digest"})
_PROPOSAL_FIELDS = ("runtime_endpoints", "criterion_checks", "requirements", "install", "checks", "activation", "services",
                    "empty_reason", "local_credentials")


def proposal_of(manifest: EnvironmentManifest) -> dict[str, Any]:
    """The model-owned proposal fields of a manifest, as the authors and the admission rules see them."""
    return dict(manifest.model_dump(mode="json", include=set(_PROPOSAL_FIELDS)))


def _record_dump(item: Any) -> dict[str, Any]:
    return dict(item.model_dump(mode="json"))


def _lifecycle(service: Any) -> set[str]:
    return {command.id for command in (service.init, service.start, service.health, service.stop) if command is not None}


@dataclass(frozen=True)
class DeltaPlan:
    """The failed accepted manifest and the closure a repair may change, fixed before any author is called."""

    base: EnvironmentManifest
    triage: RepairTriage
    # Commands an earlier execution fixed (fixed_command_ids): a closure record among them keeps all but its timeout.
    fixed: frozenset[str] = frozenset()

    @property
    def closure(self) -> frozenset[str]:
        return frozenset(self.triage.closure_ids)

    @property
    def touched(self) -> frozenset[str]:
        return frozenset(self.triage.touched_parts)

    @property
    def digest(self) -> str:
        return self.triage.digest

    def group_touched(self, group: CheckGroup) -> bool:
        """A group is touched when a closure check is one it owns, including the split remainders under its prefix."""
        return any(owns(group.prefix, identifier) for identifier in self.closure)

    def brief(self, *, divided: bool) -> dict[str, Any]:
        """What a repair author is shown: the failure's roots and consequences, and the closure records and criteria."""
        return {'rejected_manifest_digest': self.base.digest(), 'roots': self.triage.roots,
                'consequences': list(self.triage.consequences)[:64], 'touched_parts': list(self.triage.touched_parts),
                'criterion_ids': list(self.triage.criterion_ids), 'requirement_ids': list(self.triage.requirement_ids),
                'closure_records': self.closure_global() if divided else self.closure_records(),
                'closure_criteria': self.closure_criteria(),
                **({'fixed_ids': fixed} if (fixed := sorted(self.closure & self.fixed)) else {})}

    def restore_frozen(self, base: Mapping[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        """The candidate part with every frozen install or check record it changed or dropped put back as it was.

        Dettmore e2e-13: execution feedback named c00-gh-cli and c00-repo-env, outside the closure; the delta author,
        never shown their records, rewrote them, and three corrections asking it to restore them exactly as they were
        went unanswered until the schema budget ran out. A delta may not change a frozen record, so restoring it is
        exactly the correction; the author's closure changes stand. Service, requirement, mapping and activation
        changes are still corrected by the guard. An unparsable part is returned unchanged for admission to report.
        """
        try:
            before = part_records(base)
            frozen = frozen_records(before, part_records(candidate), self.triage) & set(before.commands)
        except ValidationError:
            return candidate
        restored = dict(candidate)
        for key in ('install', 'checks'):
            originals = {item['id']: item for item in base.get(key) or [] if isinstance(item, dict) and item.get('id') in frozen}
            if not originals:
                continue
            items = [originals.get(item.get('id'), item) if isinstance(item, dict) else item for item in candidate.get(key) or []]
            present = {item.get('id') for item in items if isinstance(item, dict)}
            restored[key] = [*items, *(record for identifier, record in originals.items() if identifier not in present)]
        return restored

    def guard_part(self, base: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
        """Inside an author's own phase, the merge guard's rules over the records this part owns, so the author can
        correct an out-of-closure edit before any merge. Ids only in the correction."""
        refuse_frozen(frozen_records(part_records(base), part_records(candidate), self.triage))

    def base_proposal(self) -> dict[str, Any]:
        return proposal_of(self.base)

    def base_global(self, groups: list[CheckGroup]) -> dict[str, Any]:
        """The global part's base: checks that no check group owns, and no criterion mappings."""
        return global_view(self.base_proposal(), groups)

    def base_group(self, group: CheckGroup) -> dict[str, Any] | None:
        return parts_from_manifest(self.base, [group]).get(group.prefix)

    def closure_records(self, owner: str | None = None) -> dict[str, Any]:
        """Closure records by id. ``owner`` selects one part (a check group prefix, or GLOBAL for everything else); None
        selects all. A service stands for its lifecycle commands: it is in the closure when one of them is."""
        def mine(identifier: str) -> bool:
            if owner is None:
                return True
            return part_of(identifier) == GLOBAL if owner == GLOBAL else owns(owner, identifier)

        records: dict[str, Any] = {}
        records.update({check.id: _record_dump(check) for check in self.base.checks if check.id in self.closure and mine(check.id)})
        if owner is None or owner == GLOBAL:
            records.update({item.id: _record_dump(item) for item in [*self.base.install, *self.base.requirements]
                            if item.id in self.closure or item.id in self.triage.requirement_ids})
            records.update({service.id: _record_dump(service) for service in self.base.services
                            if service.id in self.closure or self.closure.intersection(_lifecycle(service))})
            if ACTIVATION in self.closure:
                records[ACTIVATION] = _record_dump(self.base.activation)
        return records

    def closure_global(self) -> dict[str, Any]:
        return self.closure_records(GLOBAL)

    def closure_checks(self, group: CheckGroup) -> list[dict[str, Any]]:
        return list(self.closure_records(group.prefix).values())

    def closure_criteria(self, criteria: frozenset[str] | None = None) -> dict[str, list[str]]:
        wanted = set(self.triage.criterion_ids) if criteria is None else set(criteria)
        return {criterion: list(ids) for criterion, ids in sorted(self.base.criterion_checks.items()) if criterion in wanted}


TOP_LEVEL = ("activation", "runtime_endpoints", "local_credentials", "empty_reason")
TOP_LEVEL_DEFAULTS: dict[str, Any] = {"activation": {"path": [], "env": {}}, "runtime_endpoints": [],
                                     "local_credentials": [], "empty_reason": None}


def changed_ids(base: EnvironmentManifest, candidate: Mapping[str, Any]) -> dict[str, list[str]]:
    """Which records differ from the base: changed, added and removed by id, and changed top-level fields by name.

    Top-level fields (activation, runtime endpoints, local credentials, empty reason) are named in ``changed`` so the
    repair scope tells the reviewer about them as it does about records.
    """
    before = {item.id: _record_dump(item) for group in RECORD_GROUPS for item in getattr(base, group)}
    after = {item["id"]: item for group in RECORD_GROUPS for item in candidate.get(group) or [] if isinstance(item, dict)}
    changed = sorted(identifier for identifier in before if identifier in after and after[identifier] != before[identifier])
    base_fields = proposal_of(base)
    changed += [name for name in TOP_LEVEL if candidate.get(name) is not None
                and candidate[name] != base_fields.get(name, TOP_LEVEL_DEFAULTS[name])]
    return {"changed": sorted(changed), "added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after))}


def dependency_digest(manifest: EnvironmentManifest, identifiers: Iterable[str]) -> str:
    """A digest of the full records of ``identifiers`` and of everything they transitively depend on.

    A part's standing depends on this: a changed prerequisite changes what its checks test, even when the checks are not.
    """
    records = depends_on_closure(manifest, identifiers)
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def standing_view(shared: Mapping[str, Any]) -> dict[str, Any]:
    """The review context a part's standing depends on: no probe history, execution history or cache path."""
    return {key: value for key, value in shared.items() if key not in STANDING_EXCLUDED}
