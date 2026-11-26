"""Delta repair: an execution repair re-authors only the closure of its failure, as changed and added records.

The failed accepted manifest is the base. A touched author returns the records it changes or adds; the daemon applies
them to the base by id, so every untouched record carries over unchanged. The result then passes the full admission
pipeline, including the closure guard. A patch never removes a record: an omitted base record is kept.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.models import Command, content_key
from pydantic import ValidationError

from coordinare.services.environment_delta_schema import RECORD_GROUPS
from coordinare.services.environment_obligations import (
    PartView,
    frozen_records,
    part_records,
    refuse_frozen,
    refuse_unrepaired_roots,
)
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
# nare-native-036: a group's mappings still named global checks the global author retired, and the error held the
# attempt with 5.8 hours left; the full repair can re-derive every mapping.
DELTA_FALLBACK = frozenset({"attempt_schema_limit", "repair_delta_rejected", "executable_merge_invalid", "repair_triage_stale",
                            "execution_command_changed", "criterion_mapping_invalid", "inherited_mapping_missing"})
# Proposal fields a manifest's standing review depends on; provenance and cache-only facts never change a judgment.
STANDING_EXCLUDED = frozenset({"manifest_digest", "probe_evidence", "untrusted_execution_history",
                               "untrusted_trial_observations", "fixed_ids",
                               "execution_capabilities", "capabilities_digest"})
_PROPOSAL_FIELDS = ("runtime_endpoints", "criterion_checks", "requirements", "install", "checks", "activation", "services",
                    "empty_reason", "local_credentials")


# nare-native-036: a retirement that also dropped the check's depends_on read as a rewrite, and retire_edited minted a
# replacement running the same argv, which admission then refused as duplicate content naming an id the author never
# wrote. What a check depends on is not what it runs.
_COMMAND_CONTENT_EXCLUDED = ('required', 'timeout_seconds', 'depends_on')
# The timeout a command record gets when it names none.
DEFAULT_COMMAND_TIMEOUT: float = Command.model_fields['timeout_seconds'].default


def _same_command(item: Mapping[str, Any], original: Mapping[str, Any]) -> bool:
    """Whether a check record runs what the original ran: everything but required and the timeout."""
    try:
        current, before = Command.model_validate(item), Command.model_validate(original)
    except ValidationError:
        return True  # an unparsable record is left for admission to report
    excluded = set(_COMMAND_CONTENT_EXCLUDED)
    return bool(current.model_dump(mode='json', exclude=excluded) == before.model_dump(mode='json', exclude=excluded))


def _as_ran(item: dict[str, Any], original: Mapping[str, Any]) -> dict[str, Any]:
    """The same command as it ran: its executed dependencies, and at least its executed timeout."""
    kept = _ran_timeout(item, original)
    ran = list(original.get('depends_on') or [])
    return kept if list(kept.get('depends_on') or []) == ran else {**kept, 'depends_on': ran}


def _ran_timeout(item: dict[str, Any], original: Mapping[str, Any]) -> dict[str, Any]:
    """The record with at least the timeout its executed original ran with. nare-native-031: a check author resent two
    retirements without timeout_seconds; the 180 s default replaced 1800 s and 300 s and the merge refused the delta."""
    timeout, ran = item.get('timeout_seconds', DEFAULT_COMMAND_TIMEOUT), original.get('timeout_seconds')
    if type(ran) in {int, float} and type(timeout) in {int, float} and timeout < ran:
        return {**item, 'timeout_seconds': ran}
    return item


def _content(item: Mapping[str, Any]) -> str | None:
    """The duplicate-content key of a check record (argv, cwd, env and scope), or None when it does not parse."""
    try:
        return str(content_key(Command.model_validate(item)))
    except ValidationError:
        return None


def _restore_activation(base: Mapping[str, Any], candidate: Any) -> dict[str, Any]:
    """Frozen activation as it was, with the author's growth the guard allows: new PATH entries after the existing
    ones, in the author's order, and new environment keys. nare-native-028: the global delta author reordered the
    PATH entries, was refused nine times, and the delta was rejected."""
    old_path = list(base.get('path') or [])
    old_env = dict(base.get('env') or {})
    new: dict[str, Any] = candidate if isinstance(candidate, dict) else {}
    new_path: list[Any] = path if isinstance(path := new.get('path'), list) else []
    new_env: dict[str, Any] = values if isinstance(values := new.get('env'), dict) else {}
    return {**new, 'path': [*old_path, *(entry for entry in new_path if entry not in old_path)],
            'env': {**new_env, **old_env}}


def _redepend(item: Any, renamed: Mapping[str, str]) -> Any:
    """A required command that depended on a retired check depends on its replacement instead."""
    if not isinstance(item, dict) or not isinstance(depends := item.get('depends_on'), list) or item.get('required') is False:
        return item
    return {**item, 'depends_on': [renamed.get(identifier, identifier) for identifier in depends]}


def retirement_renames(base: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, str]:
    """Global checks the candidate retired (required in ``base``, not in ``candidate``) and the required ``-rN``
    replacement retire_edited minted for each."""
    before = {item['id']: item for item in base.get('checks') or [] if isinstance(item, dict)}
    after = {item['id']: item for item in candidate.get('checks') or [] if isinstance(item, dict)}
    renames: dict[str, str] = {}
    for identifier, old in before.items():
        new = after.get(identifier)
        if new is None or not old.get('required', True) or new.get('required', True):
            continue
        pattern = re.compile(re.escape(identifier) + r'-r\d+')
        if replacement := next((other for other in sorted(after) if pattern.fullmatch(other)
                                and after[other].get('required', True)), None):
            renames[identifier] = replacement
    return renames


def renamed_part(part: Mapping[str, Any], renames: Mapping[str, str]) -> dict[str, Any]:
    """A check group's part with every mapping and dependency on a retired global check moved to its replacement.

    nare-native-036: the global delta author retired check-replay and check-pytest-sample by rewriting them; the c00-
    and c01- criteria still mapped the old ids, the global author may not write mappings and c01- was not touched, so
    nobody could move them and the attempt held on criterion_mapping_invalid."""
    if not renames:
        return dict(part)
    mapping = {criterion: list(dict.fromkeys(renames.get(identifier, identifier) for identifier in ids))
               for criterion, ids in (part.get('criterion_checks') or {}).items()}
    return {**part, 'criterion_checks': mapping, 'checks': [_redepend(item, renames) for item in part.get('checks') or []]}


def proposal_of(manifest: EnvironmentManifest) -> dict[str, Any]:
    """The model-owned proposal fields of a manifest, as the authors and the admission rules see them.

    A manifest keeps its forwards under runtime.endpoints, where construction compiled the proposal's runtime_endpoints;
    they are put back here. Dettmore e2e-65: a delta repair built from this base silently dropped the postgres forward,
    and the next execution's carrier-side health probe found nothing listening.
    """
    data = manifest.model_dump(mode="json", include={*_PROPOSAL_FIELDS, "runtime"})
    runtime = data.pop("runtime", None)
    data["runtime_endpoints"] = list(runtime.get("endpoints") or []) if isinstance(runtime, dict) else []
    return dict(data)


def _ids_of(records: Any) -> frozenset[str]:
    return frozenset({item['id'] for item in records or [] if isinstance(item, dict) and isinstance(item.get('id'), str)})


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
    # Checks that failed in an execution and never passed (budget.retirable_checks): one may be retired and replaced.
    retirable: frozenset[str] = frozenset()

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
        exactly the correction; the author's closure changes stand. Frozen activation is put back the same way, keeping
        the growth the guard allows. Service, requirement and mapping changes are still corrected by the guard. An
        unparsable part is returned unchanged for admission to report.
        """
        try:
            before = part_records(base)
            frozen = frozen_records(before, part_records(candidate), self.triage)
        except ValidationError:
            return candidate
        restored = dict(candidate)
        if ACTIVATION in frozen and isinstance(base.get('activation'), dict):
            restored['activation'] = _restore_activation(base['activation'], candidate.get('activation'))
        frozen &= set(before.commands)
        for key in ('install', 'checks'):
            originals = {item['id']: item for item in base.get(key) or [] if isinstance(item, dict) and item.get('id') in frozen}
            if not originals:
                continue
            items = [originals.get(item.get('id'), item) if isinstance(item, dict) else item for item in candidate.get(key) or []]
            present = {item.get('id') for item in items if isinstance(item, dict)}
            restored[key] = [*items, *(record for identifier, record in originals.items() if identifier not in present)]
        return self.retire_edited(base, restored)

    def retire_edited(self, base: Mapping[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        """The candidate with each failed check it rewrote in place retired as it ran and replaced by the rewrite.

        nare-native-024: five checks failed because they were wrong, and the delta author rewrote one in place ten
        times though each correction said to retire it (keep it exactly as it ran, required false) and add a new
        check; the attempt ran out of schema corrections. A rewrite of a check that failed and never passed can only
        be that retirement, so it is made here: the executed record stays, not required, and the rewrite becomes a
        new required check under a derived id that takes over the old one's mappings and dependents. Review still
        judges the replacement.
        """
        editable = self.closure & self.fixed & self.retirable
        originals = {item['id']: item for item in base.get('checks') or []
                     if isinstance(item, dict) and item.get('id') in editable}
        if not originals or not isinstance(candidate.get('checks'), list):
            return candidate
        taken = {item.get('id') for key in ('install', 'checks') for item in candidate.get(key) or [] if isinstance(item, dict)}
        taken |= {command.get('id') for service in candidate.get('services') or [] if isinstance(service, dict)
                  for command in (service.get(phase) for phase in ('init', 'start', 'health', 'stop'))
                  if isinstance(command, dict)}
        # nare-native-026: the author both rewrote a check in place and added its own replacement with the same
        # command; a second copy under a derived id was refused as duplicate content. That replacement takes over.
        existing = {key: item['id'] for item in candidate['checks'] if isinstance(item, dict)
                    and item.get('id') not in originals and (key := _content(item)) is not None}
        renamed: dict[str, str] = {}
        checks: list[Any] = []
        lengthened = False
        for item in candidate['checks']:
            original = originals.get(item.get('id')) if isinstance(item, dict) else None
            if original is None or _same_command(item, original):
                kept = item if original is None else _as_ran(item, original)
                lengthened |= kept is not item
                checks.append(kept)
                continue
            timeout = item.get('timeout_seconds')
            retired = {**original, 'required': False}
            if type(timeout) in {int, float} and timeout > original.get('timeout_seconds', 0):
                retired['timeout_seconds'] = timeout
            if (kept := existing.get(_content(item) or '')) is not None:
                renamed[original['id']] = kept
                checks.append(retired)
                continue
            new_id = next(f"{original['id']}-r{n}" for n in range(1, len(taken) + 2) if f"{original['id']}-r{n}" not in taken)
            taken.add(new_id)
            renamed[original['id']] = new_id
            checks += [retired, {**item, 'id': new_id, 'required': True}]
        # Each replacement carries its retired check's obligations, so it is required.
        replacements = set(renamed.values())
        checks = [{**item, 'required': True} if isinstance(item, dict) and item.get('id') in replacements else item
                  for item in checks]
        if not renamed:
            return {**candidate, 'checks': checks} if lengthened else candidate
        result = {**candidate, 'checks': checks}
        if isinstance(mapping := candidate.get('criterion_checks'), dict):
            result['criterion_checks'] = {criterion: [renamed.get(identifier, identifier) for identifier in ids]
                                          if isinstance(ids, list) else ids for criterion, ids in mapping.items()}
        if isinstance(requirements := candidate.get('requirements'), list):
            result['requirements'] = [{**requirement, 'checks': [renamed.get(identifier, identifier)
                                                                 for identifier in requirement['checks']]}
                                      if isinstance(requirement, dict) and isinstance(requirement.get('checks'), list)
                                      else requirement for requirement in requirements]
        for key in ('install', 'checks'):
            if isinstance(result.get(key), list):
                result[key] = [_redepend(item, renamed) for item in result[key]]
        if isinstance(services := candidate.get('services'), list):
            result['services'] = [{**service, **{phase: _redepend(service[phase], renamed)
                                                 for phase in ('init', 'start', 'health', 'stop')
                                                 if isinstance(service.get(phase), dict)}}
                                  if isinstance(service, dict) else service for service in services]
        return result

    def guard_part(self, base: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
        """Inside an author's own phase, the merge guard's rules over the records this part owns, so the author can
        correct an out-of-closure edit, or a failed command left as it ran, before any merge. Ids only in the
        correction."""
        before, after = part_records(base), part_records(candidate)
        refuse_frozen(frozen_records(before, after, self.triage))
        # A check group's part holds no installs and cannot see the global part: its failed checks are judged only when
        # the closure leaves the global part untouched, so nothing it relies on can change elsewhere.
        refuse_unrepaired_roots(before, after, self.triage, PartView(
            _ids_of(base.get('install')), _ids_of(candidate.get('install')),
            environment_changed='install' not in base and GLOBAL in self.touched))

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
    """The review context a part's standing depends on: no probe history, execution history or cache path.

    Install command text is shown to every part reviewer but is the global part's to judge: as before it was shown,
    only install ids bear on a group part's standing, so an install repair re-reviews the global part alone.
    """
    view = {key: value for key, value in shared.items() if key not in STANDING_EXCLUDED}
    if isinstance(global_part := view.get("global_part"), dict):
        view["global_part"] = {key: value for key, value in global_part.items() if key != "install"}
    return view
