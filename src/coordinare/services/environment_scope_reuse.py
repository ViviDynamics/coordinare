"""Reuse of an independently accepted source scope by a later attempt on the same commit.

A new attempt (after an operator restart) may start from the scope contract an earlier attempt had accepted,
instead of authoring it again. The reused definition is never accepted as it stands: the new attempt begins
at a fresh independent review of it (throughput amendment, section 5).

That review re-reads only what changed. Dettmore e2e-71 reused e2e-39's contract on the same commit, inventory and
shard plan, with all 594 reviewer input digests identical, and still ran 41 shard reviews and 25 merge reviews
(3 h 17 min) because reuse dropped the earlier reviewer's reads. The earlier reads now travel with the seed, so a
shard is reviewed again only when its criteria or one of its inputs changed, exactly as within one attempt. The
merge review still runs on every reuse, so each attempt keeps one fresh independent acceptance.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.scope import ScopeContract, ScopeDefinition
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coordinare.services.environment_attempt import AttemptBudget

SCOPE_AUTHOR_PROTOCOL = 4
SCOPE_POLICY_VERSION = '353-scope-v1'
# The scope rules a journal's scope was authored and accepted under. Rules 2 (2026-11-09) limit test and build
# operations to sampled configuration proof, so a scope accepted under rules 1 is not offered for reuse.
SCOPE_RULES = 2
# The earlier acceptance's reader digests, kept with the shard progress; each stands only for the digest it read.
REUSED_REVIEWER_INPUTS = 'reused_reviewer_inputs'


def inventory_digest(paths: Sequence[str]) -> str:
    """The digest a source inspection computes for its inventory."""
    return hashlib.sha256(json.dumps(sorted(paths), ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def reusable_scope(archive: Sequence[Any], commit: str) -> dict[str, Any] | None:
    """The newest archived attempt's accepted scope for this commit, as a seed for a fresh review."""
    for data in reversed(archive):
        # Protocol 4 journals that predate the recorded policy version were all written under 353-scope-v1.
        if (not isinstance(data, dict) or data.get('source_commit') != commit
                or data.get('scope_author_protocol') != SCOPE_AUTHOR_PROTOCOL
                or data.get('scope_policy_version', SCOPE_POLICY_VERSION) != SCOPE_POLICY_VERSION
                or data.get('scope_rules', 1) != SCOPE_RULES):
            continue
        try:
            contract = ScopeContract.model_validate(data.get('scope_contract'))
        except ValidationError:
            continue
        record = contract.source_assessment
        if (record is None or record.assessment.verdict != 'accept' or record.scope_digest != contract.definition.digest()
                or contract.definition.source_commit != commit):
            continue
        shards = {**(data.get('scope_shards') or {}), REUSED_REVIEWER_INPUTS: dict(record.reviewer_inputs)}
        inputs = data.get('scope_inputs')
        return {'definition': contract.definition.model_dump(mode='json'),
                'inputs': dict(inputs) if isinstance(inputs, dict) else {},
                'shards': shards or None,
                'from': {'contract_digest': contract.digest(), 'started_at': data.get('started_at')}}
    return None


def adopt_reused_scope(budget: AttemptBudget, *, commit: str, paths: Sequence[str]) -> None:
    """Start a fresh attempt at independent review of a reused definition when its source is unchanged."""
    seed, budget.scope_reuse = budget.scope_reuse, None
    if seed is None or budget.scope_definition is not None or budget.scope_phase != 'initial':
        return
    try:
        definition = ScopeDefinition.model_validate(seed['definition'])
    except (KeyError, TypeError, ValidationError):
        return
    if definition.source_commit != commit or definition.inventory_digest != inventory_digest(paths):
        return
    budget.scope_definition = definition
    budget.scope_inputs = {**dict(seed.get('inputs') or {}), **budget.scope_inputs}
    budget.scope_shards = seed.get('shards')
    budget.scope_phase = 'review'
    budget.scope_reused_from = dict(seed.get('from') or {})
