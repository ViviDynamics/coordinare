"""Bind executable proposals to reviewed source scope and configured facilities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.models import (
    EnvironmentManifest,
    EnvironmentManifestProposal,
)
from coordinare_service_inference.environment.runtime import RuntimeSpec

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_scope import discover_scope

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from coordinare_service_inference.environment.inventory import InventoryEntry
    from coordinare_service_inference.environment.models import ExecutionCapabilities
    from coordinare_service_inference.environment.scope import ScopeContract

    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_discovery import ChatJson, DiscoveryLimits, Fetch


class ScopeReopenedError(EnvironmentDiscoveryError):
    """The executable proposal must be reconstructed against a newly reviewed scope."""

    def __init__(self) -> None:
        super().__init__('source evidence reopened independent scope review', code='scope_review_reopened')


@dataclass
class ReviewedConstruction:
    source_identity: tuple[str, str]
    paths: Sequence[str]
    fetch: Fetch
    chat: ChatJson
    limits: DiscoveryLimits
    budget: AttemptBudget
    capabilities: ExecutionCapabilities
    entries: Mapping[str, InventoryEntry] | None = None

    def retain_inputs(self, inputs: dict[str, str]) -> None:
        if any(path not in self.paths or self.budget.scope_inputs.get(path, digest) != digest for path, digest in inputs.items()):
            message = 'source evidence differs from the retained scope snapshot'
            raise EnvironmentDiscoveryError(message, code='source_identity')
        self.budget.scope_inputs.update(inputs)

    async def prepare(self, inputs: dict[str, str] | None = None) -> ScopeContract:
        if inputs is not None:
            self.retain_inputs(inputs)
        return await discover_scope(paths=self.paths, fetch=self.fetch, chat=self.chat, commit=self.source_identity[1],
            limits=self.limits, budget=self.budget, entries=self.entries)

    def require_scope(self) -> ScopeContract:
        scope = self.budget.scope_contract
        if scope is None:
            message = 'independently reviewed source scope is required'
            raise EnvironmentDiscoveryError(message, code='scope_review_required')
        return scope

    def context(self) -> dict[str, Any]:
        scope = self.require_scope()
        return {'scope_contract': scope.model_dump(mode='json'), 'scope_contract_digest': scope.digest(),
                'scope_mapping_instruction': 'Map exactly every required source criterion ID to one or more required, '
                    'unscoped checks[].id in criterion_checks. Do not change criteria or requiredness. '
                    'Install-only, optional, invented or role-scoped checks cannot satisfy whole-environment obligations. '
                    'A version check cannot replace operational usability. Declare runtime_endpoints only within '
                    'the explicitly configured private-service policy; runtime identity, resources and privileges '
                    'are daemon-owned. Supply one complete executable manifest.'}

    async def observe_sources(self, inputs: dict[str, str]) -> None:
        scope = self.budget.scope_contract
        if scope is None:
            self.context()
            return
        if any(scope.all_inputs.get(path, digest) != digest for path, digest in inputs.items()):
            message = 'source evidence differs from reviewed scope'
            raise EnvironmentDiscoveryError(message, code='source_identity')
        additional = {path: digest for path, digest in inputs.items() if path not in scope.all_inputs}
        if not additional:
            return
        self.budget.reserve_repair('coverage')
        self.retain_inputs(additional)
        self.budget.scope_contract = None
        self.budget.scope_phase = "review"
        self.budget.scope_revision_base_digest = None
        await self.budget.flush()
        await self.prepare()
        raise ScopeReopenedError

    def admit(self, candidate: dict[str, Any], inspected: dict[str, str]) -> EnvironmentManifest:
        proposal = EnvironmentManifestProposal.model_validate(candidate)
        scope = self.require_scope()
        if not inspected or any(requirement.source not in inspected for requirement in proposal.requirements):
            message = 'discovery requirement cites unread source'
            raise EnvironmentDiscoveryError(message, code='source_identity')
        runtime = None
        if self.capabilities.runtime is not None:
            runtime = RuntimeSpec(schema_version=1, policy=self.capabilities.runtime, endpoints=proposal.runtime_endpoints)
        elif proposal.runtime_endpoints:
            message = 'private runtime endpoints require a configured platform policy'
            raise EnvironmentDiscoveryError(message, code='required_capability_unavailable')
        return EnvironmentManifest.model_validate({**proposal.model_dump(exclude={'runtime_endpoints'}),
            'schema_version': 3, 'policy_version': '353-scope-v1',
            'scope_contract': scope.model_dump(mode='json'), 'scope_contract_digest': scope.digest(),
            'symphony_name': self.source_identity[0], 'source_commit': self.source_identity[1],
            'inventory_digest': scope.definition.inventory_digest, 'inputs': dict(inspected),
            'capabilities_digest': self.capabilities.digest(), 'runtime': runtime.model_dump(mode='json') if runtime else None})
