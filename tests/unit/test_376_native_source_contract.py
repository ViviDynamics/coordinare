"""Native source phases advertise executable tools and preserve evidence identity."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from coordinare_service_inference.environment.findings import FindingLedger
from nare.schema import validate

from coordinare.services.environment_agent import (
    EnvironmentAgentContext,
    ObservationTools,
    PinnedSourceTools,
)
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_scope import (
    _messages,
    _ResponseReferenceError,
    _validate_author,
)
from coordinare.services.environment_shard_construction import DividedConstruction
from coordinare.services.environment_shard_scope import (
    MERGE_REVIEW_PREAMBLE,
    SHARD_REVIEW_PREAMBLE,
    AgentContext,
    ReviewMaterial,
    ShardedScope,
)
from tests.unit.test_353_scope_contract import COMMIT, criterion, proposal
from tests.unit.test_353_sharded_executable import SUMMARY, CheckGroup, make_criterion


class CapturedPhaseError(Exception):
    pass


def assert_source_instructions(instructions: str) -> None:
    # These remain valid domain operation tags, distinct from callable tool names.
    instructions = instructions.replace('read_file/list_files requests', 'inspection requests')
    assert 'read_files' not in instructions
    assert 'list_files' not in instructions
    assert 'read_file' not in instructions
    assert 'read_source' in instructions
    assert 'Inspect available execution mechanisms through capability probes' not in instructions


class CaptureRuntime:
    async def run(self, request: Any) -> Any:
        assert_source_instructions(request.instructions)
        assert {'read_source', 'list_sources'} <= {tool.name for tool in request.allowed_tools}
        raise CapturedPhaseError


def native_budget() -> AttemptBudget:
    budget = AttemptBudget()
    budget.scope_findings = FindingLedger(source_commit=COMMIT)
    budget.agent_context = EnvironmentAgentContext(CaptureRuntime(), 'source-contract', 'a' * 64)
    return budget


@pytest.mark.asyncio
@pytest.mark.parametrize('review', [False, True])
async def test_scope_messages_expose_only_available_navigation(review: bool) -> None:
    inspection = SourceInspection(['guide'], lambda _: None, DiscoveryLimits(), native_budget())
    instructions = _messages(inspection, review=review, definition=None, evidence=[])[0]['content']
    assert_source_instructions(instructions)
    if review:
        assert 'read_file/list_files requests' in instructions


@pytest.mark.asyncio
@pytest.mark.parametrize('preamble', [SHARD_REVIEW_PREAMBLE, MERGE_REVIEW_PREAMBLE], ids=['shard', 'merge'])
async def test_shard_review_messages_keep_operation_tags_but_expose_native_tools(preamble: str) -> None:
    budget = native_budget()
    inspection = SourceInspection(['guide'], lambda _: None, DiscoveryLimits(), budget)
    inspection.admit_content('guide', 'A documented environment.', fetched=False)
    definition = _validate_author(proposal(criterion()), author=inspection, commit=COMMIT, budget=budget)
    scope = SimpleNamespace(budget=budget, file_list=lambda paths: [])
    messages = ShardedScope._review_messages(scope, inspection, preamble=preamble, definition=definition,
        material=ReviewMaterial(definition.criteria, [], [], 16))
    assert_source_instructions(messages[0]['content'])
    assert 'read_file/list_files requests' in messages[0]['content']


@pytest.mark.asyncio
async def test_check_author_native_request_uses_available_source_tools() -> None:
    budget = native_budget()
    group = CheckGroup('c00-', (make_criterion(1),))
    content = 'The documented local check must run.'
    async def fetch(path: str) -> str:
        return content
    scope = SimpleNamespace(all_inputs={'src/001.md': hashlib.sha256(content.encode()).hexdigest()},
        definition=SimpleNamespace(criteria=list(group.criteria)), finding_ledger=budget.scope_findings)
    build = DividedConstruction(AgentContext(budget, DiscoveryLimits(), None), ['src/001.md'], fetch, scope, 1)
    with pytest.raises(CapturedPhaseError):
        await build.author(group, summary=SUMMARY, probes=[])


@pytest.mark.parametrize('native,tool', [(False, 'read_files'), (True, 'read_source')])
def test_source_citation_feedback_names_the_active_transport(native: bool, tool: str) -> None:
    budget = native_budget() if native else AttemptBudget()
    budget.scope_findings = FindingLedger(source_commit=COMMIT)
    inspection = SourceInspection(['guide', 'other'], lambda _: None, DiscoveryLimits(), budget)
    with pytest.raises(_ResponseReferenceError) as caught:
        _validate_author(proposal(criterion('other')), author=inspection, commit=COMMIT, budget=budget)
    guidance = caught.value.errors[0]['guidance']
    assert tool in guidance
    if native:
        assert 'read_file' not in guidance


@pytest.mark.asyncio
async def test_new_unavailable_source_does_not_poison_navigation() -> None:
    async def fetch(path: str) -> str | None:
        return None if path == 'assets/blob' else 'A source-defined requirement.'
    inspection = SourceInspection(['assets/blob', 'guide'], fetch, DiscoveryLimits())
    tools = PinnedSourceTools(inspection)
    result = json.loads(await tools.read({'path': 'assets/blob'}))
    assert result['error'] == 'source_unavailable'
    assert tools.failure is None and not tools.issues()
    assert 'assets/blob' not in inspection.inputs
    result = json.loads(await tools.read({'path': 'guide'}))
    assert result['unread_count'] == 0
    assert inspection.inputs.keys() == {'guide'}


@pytest.mark.asyncio
@pytest.mark.parametrize('retained', [False, True])
async def test_unavailable_bound_source_remains_a_terminal_identity_failure(retained: bool) -> None:
    async def fetch(path: str) -> None:
        return None
    digest = hashlib.sha256(b'Pinned content').hexdigest()
    inspection = SourceInspection(['guide'], fetch, DiscoveryLimits(),
        retained_inputs={'guide': digest} if retained else None)
    if not retained:
        inspection.inputs['guide'] = digest
    tools = PinnedSourceTools(inspection)
    with pytest.raises(EnvironmentDiscoveryError):
        await tools.read({'path': 'guide'})
    with pytest.raises(EnvironmentDiscoveryError):
        tools.issues()


def test_empty_observation_reread_is_rejected_by_the_advertised_schema() -> None:
    observations = ObservationTools()
    key = observations.register({'result': 'Untrusted observation.'})
    assert validate({'observation_id': key, 'pages': []}, observations.reread_tool().input_schema)
    assert not validate({'observation_id': key, 'pages': [0]}, observations.reread_tool().input_schema)
