"""nare-native-012: a probe batch whose tools were absent left its effect pending, and 621 later probes were refused.

Three faults, each fixed on its own: the unprivileged carrier's PATH lookup failed with EACCES and was read as a
probe machinery failure; that refusal left the batch's settled receipt pending; and the pending receipt reached the
model as a tool error to retry instead of holding the phase.
"""
from __future__ import annotations

import os
import stat
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_353_discovery_repair import CAPABILITIES, COMMIT, PATH
from tests.unit.test_353_environment_probes import plan, report_data, request_data


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX PATH search')
def test_a_tool_behind_an_unsearchable_path_entry_is_observed_as_absent(tmp_path: Any) -> None:
    from coordinare_service_inference.environment import observation

    locked = tmp_path / 'locked'
    locked.mkdir()
    locked.chmod(0)
    try:
        command = {'id': 'tool', 'argv': ['coordinare-absent-tool-376', '--version'], 'cwd': str(tmp_path),
                   'env': {'PATH': f'{locked}:/usr/bin:/bin'}, 'timeout_seconds': 5}
        result = observation.execute(command, 5, roots={'repo': str(tmp_path), 'cache': str(tmp_path),
                                                        'scratch': str(tmp_path)})
    finally:
        locked.chmod(stat.S_IRWXU)
    # Root searches any directory, so the lookup then fails with ENOENT; either way it is an observation.
    assert result['status'] == 'completed' and result['exit_code'] == 127, result
    if os.geteuid() != 0:
        assert result['output'] == 'executable or working directory unavailable or not accessible to this user'


def test_a_report_identity_is_valid_even_when_a_probe_saw_nothing() -> None:
    from coordinare.services.environment_probes import (
        ProbeReport,
        validate_probe_identity,
        validate_probe_report,
    )

    p = plan()
    data = report_data(p)
    data['results'][0].update(status='execution_error', exit_code=None, output='guest boot failed')
    report = ProbeReport.model_validate(data)
    validate_probe_identity(p, report)
    with pytest.raises(EnvironmentDiscoveryError) as held:
        validate_probe_report(p, report)
    assert held.value.code == 'probe_observation_incomplete'


@pytest.mark.asyncio
async def test_an_unobserved_probe_settles_its_receipt_so_a_new_batch_can_run() -> None:
    from coordinare.services import environment_discovery as discovery
    from coordinare.services.agent_runtime.effects import EffectJournal
    from coordinare.services.environment_probes import ProbeReport

    effects = EffectJournal()

    async def persist() -> None:
        pass

    effects.persist = persist
    budget = AttemptBudget()

    async def flush() -> None:
        pass

    budget.flush = flush  # type: ignore[method-assign]
    executed = []

    async def executor(executed_plan: Any) -> ProbeReport:
        executed.append(executed_plan.request.commands[0].argv)
        data = report_data(executed_plan)
        if len(executed) == 1:
            data['results'][0].update(status='execution_error', exit_code=None, output='guest boot failed')
        return ProbeReport.model_validate({**data, 'plan': None})

    inspection = SimpleNamespace(budget=budget, inventory_digest='b' * 64, inputs={PATH: 'c' * 64},
                                 native_effects=effects, require_preflight=False)
    options = SimpleNamespace(probe_executor=executor, require_preflight=False, execution_source_digest='1' * 64,
                              capabilities=CAPABILITIES)
    probe = discovery._probe_handler(inspection, options, COMMIT)  # type: ignore[arg-type]
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await probe(request_data())
    assert held.value.code == 'probe_observation_incomplete'
    assert [receipt.status for receipt in effects.receipts()] == ['completed']
    second = request_data()
    second['commands'][0]['argv'] = ['bash', '-c', 'command -v unfamiliar-tool']
    report = await probe(second)
    assert report.results[0].status == 'completed' and len(executed) == 2
    assert budget.probe_batches == 2 and len(budget.probe_reports) == 1


@pytest.mark.asyncio
async def test_an_unsettled_effect_holds_the_phase_before_the_model_is_asked_again(tmp_path: Any) -> None:
    import json
    from dataclasses import replace

    from nare import Reply, Tool, ToolCall, Usage

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.agent_runtime.effects import EffectJournal
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime
    from tests.unit.test_376_agent_accounting import admit
    from tests.unit.test_376_agent_runtime import ScriptedTransport, request

    effects = EffectJournal()
    executed = []

    async def execute() -> dict[str, Any]:
        executed.append('probe')
        message = 'synthetic connection loss after dispatch'
        raise OSError(message)

    async def call(args: dict[str, Any]) -> str:
        return json.dumps(await effects.run('probe', 'c' * 64, execute))

    class Prober(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            self.requests.append({})
            tool = ToolCall(f'p{len(self.requests)}', 'probe', {})
            return Reply([{'type': 'tool_use', 'id': tool.id, 'name': tool.name, 'input': {}}],
                         [tool], Usage(input=10, output=10), 'tool_use')

    transport = Prober('')
    req = replace(request(admit), effects=effects, checkpoint_store=CheckpointStore(tmp_path),
        budget=AttemptBudget(max_calls=8, max_schema_repairs=0),
        allowed_tools=(Tool(name='probe', description='Bound test probe', input_schema={'type': 'object'}, call=call),))
    result = await NareRuntime(transport=transport).run(req)
    assert result.status == 'held' and result.hold_code == 'agent_effect_pending'
    assert executed == ['probe'] and len(transport.requests) == 1
