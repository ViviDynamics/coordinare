"""Executable review need not page again through inputs the independent scope review already read."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.services.environment_agent import (
    EnvironmentAgentContext,
    SourcePhase,
    required_sources,
    run_source_phase,
)
from coordinare.services.environment_discovery import (
    native_review_instructions,
    reviewed_scope_inputs,
)
from coordinare.services.environment_response_schema import final_response_schema
from tests.unit.test_353_discovery_repair import COMMIT
from tests.unit.test_376_source_packet_references import TEXT, CapturedPacketError, setup_inspection


class Scope:
    def __init__(self) -> None:
        self.review_inputs = {'guide': 'd' * 64}


def test_only_inputs_reviewed_at_the_same_digest_are_on_demand() -> None:
    inspection = setup_inspection()
    item = inspection.admit_content('guide', TEXT, fetched=False)
    assert required_sources(inspection) == {'guide'}
    inspection.bound_inputs = {'guide': 'b' * 64}  # Reviewed at another digest: the content changed since.
    assert required_sources(inspection) == {'guide'}
    inspection.bound_inputs = {'guide': item['sha256']}
    assert required_sources(inspection) == frozenset()


def test_reviewed_scope_inputs_come_from_the_scope_review_and_the_prompt_says_so() -> None:
    assert reviewed_scope_inputs(Scope()) == {'guide': 'd' * 64}
    assert reviewed_scope_inputs(None) == {}
    assert 'not required reading' in native_review_instructions()


@pytest.mark.asyncio
async def test_a_reviewed_input_stays_in_the_catalog_but_only_the_packet_is_required() -> None:
    inspection = setup_inspection()
    item = inspection.admit_content('guide', TEXT, fetched=False)
    inspection.bound_inputs = {'guide': item['sha256']}

    class Runtime:
        async def run(self, request: Any) -> Any:
            catalog = request.evidence_catalog
            guide = [page for page in catalog._pages if page.source == 'guide']
            packet = [page for page in catalog._pages if page.source == '@phase-packet']
            assert guide  # Still available to open on demand.
            assert catalog.remaining_required == len(packet)
            raise CapturedPacketError
    inspection.budget.agent_context = EnvironmentAgentContext(Runtime(), 'packet-contract', 'a' * 64)
    with pytest.raises(CapturedPacketError):
        await run_source_phase(inspection, [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '{}'}],
                               SourcePhase(envelope='assessment', schema=final_response_schema('assessment'),
                                           source_commit=COMMIT, validate=lambda _: None, role='reviewer'))
