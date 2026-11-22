"""nare-native-028: three authors re-read every bound file (250-370 pages) on each of eight passes, 23-51% of the
attempt's wall time. An author's first pass reads everything; a later pass of the same author in the attempt is made
to read only what it has not read at the current digest and what open findings cite. The rest stays on demand."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services.environment_agent import (
    EnvironmentAgentContext,
    SourcePhase,
    author_required,
    open_finding_sources,
    record_author_reads,
    run_source_phase,
)
from coordinare.services.environment_response_schema import final_response_schema
from tests.unit.test_353_discovery_repair import COMMIT
from tests.unit.test_376_source_packet_references import TEXT, CapturedPacketError, setup_inspection


def _two_files() -> Any:
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_inspection import SourceInspection

    budget = setup_inspection().budget

    async def fetch(path: str) -> str:
        return TEXT

    inspection = SourceInspection(['guide', 'notes'], fetch, DiscoveryLimits(), budget)
    inspection.admit_content('guide', TEXT, fetched=False)
    inspection.admit_content('notes', TEXT + ' notes', fetched=False)
    return inspection


def _ledger(*, open_ids: list[str], records: list[tuple[str, list[str]]],
            decisions: list[tuple[str, str, list[str]]] = ()) -> Any:
    return SimpleNamespace(
        open_ids=lambda: list(open_ids),
        records=[SimpleNamespace(id=identifier, proposal=SimpleNamespace(sources=sources)) for identifier, sources in records],
        reviews=[SimpleNamespace(decisions=[SimpleNamespace(finding_id=identifier, status=status, sources=sources)
                                            for identifier, status, sources in decisions])])


def test_the_first_pass_reads_everything_and_a_later_pass_nothing_already_read() -> None:
    inspection = _two_files()
    budget = inspection.budget
    first = author_required(inspection, budget, 'discovery')
    assert first == {'guide', 'notes'}
    record_author_reads(inspection, budget, 'discovery', first)
    assert author_required(inspection, budget, 'discovery') == frozenset()
    # Another author has its own reads.
    assert author_required(inspection, budget, 'executable_checks:c00-') == {'guide', 'notes'}


def test_a_file_read_at_another_digest_is_required_again() -> None:
    inspection = _two_files()
    budget = inspection.budget
    budget.author_reads['discovery'] = {'guide': 'b' * 64, 'notes': inspection.inputs['notes']}
    assert author_required(inspection, budget, 'discovery') == {'guide'}


def test_files_open_findings_cite_are_required_again() -> None:
    inspection = _two_files()
    budget = inspection.budget
    budget.author_reads['discovery'] = dict(inspection.inputs)
    budget.finding_ledger = _ledger(open_ids=['F0002'], records=[('F0001', ['guide']), ('F0002', ['notes'])])
    assert open_finding_sources(budget) == {'notes'}
    assert author_required(inspection, budget, 'discovery') == {'notes'}
    budget.finding_ledger = _ledger(open_ids=['F0001'], records=[('F0001', ['notes'])],
                                    decisions=[('F0001', 'open', ['guide']), ('F0003', 'open', ['other'])])
    assert open_finding_sources(budget) == {'notes', 'guide'}


@pytest.mark.asyncio
async def test_a_later_pass_keeps_read_files_in_the_catalog_and_says_they_are_on_demand() -> None:
    inspection = _two_files()
    inspection.budget.author_reads['executable_checks:c00-'] = dict(inspection.inputs)
    seen: dict[str, Any] = {}

    class Runtime:
        async def run(self, request: Any) -> Any:
            catalog = request.evidence_catalog
            seen['sources'] = {page.source for page in catalog._pages}
            seen['required'] = catalog.remaining_required
            seen['packet'] = len([page for page in catalog._pages if page.source == '@phase-packet'])
            seen['task'] = request.task
            raise CapturedPacketError
    inspection.budget.agent_context = EnvironmentAgentContext(Runtime(), 'packet-contract', 'a' * 64)
    with pytest.raises(CapturedPacketError):
        await run_source_phase(inspection, [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '{}'}],
                               SourcePhase(envelope='shard_checks', schema=final_response_schema('shard_checks'),
                                           source_commit=COMMIT, validate=lambda _: None, role='author',
                                           read_key='executable_checks:c00-'))
    assert {'guide', 'notes'} <= seen['sources']
    assert seen['required'] == seen['packet']
    assert '2 catalog sources are not required here' in seen['task']
    # A pass that did not finish records nothing new.
    assert inspection.budget.author_reads['executable_checks:c00-'] == dict(inspection.inputs)


def test_author_reads_survive_the_repair_journal() -> None:
    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_repair import (
        _restore_agent_accounting,
        _save_agent_accounting,
    )

    budget = AttemptBudget()
    budget.author_reads = {'discovery:global': {'guide': 'a' * 64}}
    journal = SimpleNamespace()
    for name in ('agent_usage_receipts', 'agent_evidence_error_receipts'):
        setattr(journal, name, set())
    for name in ('agent_evidence_errors', 'trial_runs', 'shard_schema_repairs'):
        setattr(journal, name, 0)
    _save_agent_accounting(journal, budget)  # type: ignore[arg-type]
    restored = AttemptBudget()
    journal.calls = 1
    _restore_agent_accounting(restored, journal)  # type: ignore[arg-type]
    assert restored.author_reads == {'discovery:global': {'guide': 'a' * 64}}
