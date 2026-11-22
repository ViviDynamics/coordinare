"""nare-native-028: each of three divided coverage revisions re-ran every check author (about 30 minutes) though the
findings named only some groups. A revision now authors the groups its findings or feedback name; every other group
keeps its reviewed part when that part still admits under the new global part, and is authored again otherwise."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services import environment_discovery as discovery_module
from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_shard_construction import CheckGroup, DividedConstruction
from tests.unit.test_353_sharded_executable import (
    FakeBuild,
    global_part,
    group_reply,
    make_criterion,
)

GROUPS = [CheckGroup('c00-', (make_criterion(1),)), CheckGroup('c01-', (make_criterion(2),))]


class RevisingBuild(FakeBuild):
    def __init__(self, *, feedback: dict[str, Any], admits: bool = True) -> None:
        super().__init__(GROUPS)
        self.feedback = feedback
        self.obligations = {group.prefix: group_reply(group)['shard_checks'] for group in GROUPS}
        self.revising = True
        self._admits = admits

    def untouched(self) -> list[CheckGroup]:
        return DividedConstruction.untouched(self)  # type: ignore[arg-type]

    def admits(self, group: CheckGroup, part: Any, *, summary: Any) -> bool:
        return self._admits


async def _revise(monkeypatch: pytest.MonkeyPatch, build: RevisingBuild) -> dict[str, Any]:
    async def phase(**_kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
        return global_part(), {}

    async def observe(_inputs: Any) -> None:
        return None

    monkeypatch.setattr(discovery_module, '_phase', phase)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
    attempt = SimpleNamespace(budget=budget, required_ids={}, discovery=SimpleNamespace(budget=budget, inputs={},
                              finding_responses=[]), obligation_ledger=discovery_module._ObligationLedger(),
                              messages=[{'role': 'system', 'content': ''}, {'role': 'user', 'content': '{}'}],
                              chat=None, delta=None, prior_manifests=[],
                              construction=SimpleNamespace(observe_sources=observe))
    return await discovery_module._divided_candidate(build, attempt, lambda data: data)


@pytest.mark.asyncio
async def test_a_revision_authors_only_the_group_its_findings_name(monkeypatch: pytest.MonkeyPatch) -> None:
    build = RevisingBuild(feedback={'c01-': {'open_findings': [{'id': 'F0001'}]}})
    merged = await _revise(monkeypatch, build)
    assert build.rounds == [['c01-']]
    assert build.revising is False
    assert {check['id'] for check in merged['checks']} == {'toolchain', 'c00-0', 'c01-0'}


@pytest.mark.asyncio
async def test_a_carried_part_that_no_longer_admits_is_authored_again(monkeypatch: pytest.MonkeyPatch) -> None:
    build = RevisingBuild(feedback={'c01-': {'open_findings': [{'id': 'F0001'}]}}, admits=False)
    await _revise(monkeypatch, build)
    assert build.rounds == [['c01-'], ['c00-']]


@pytest.mark.asyncio
async def test_without_a_revision_every_group_is_authored(monkeypatch: pytest.MonkeyPatch) -> None:
    build = RevisingBuild(feedback={'c01-': {'open_findings': [{'id': 'F0001'}]}})
    build.revising = False
    await _revise(monkeypatch, build)
    assert build.rounds == [['c00-', 'c01-']]


def test_feedback_under_a_split_prefix_touches_its_group() -> None:
    build = RevisingBuild(feedback={'c00a-': {'merge_errors': []}})
    assert [group.prefix for group in build.untouched()] == ['c01-']
    build.feedback = {}
    build.obligations.pop('c01-')
    assert [group.prefix for group in build.untouched()] == ['c00-']  # No reviewed part: nothing to carry.
