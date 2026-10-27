"""The emulated guest must not starve native install work, and authors see the session's real budget.

A diagnostic install gave the guest every CPU of the container quota: while the guest emulated container image
builds, native compiles of a language runtime and a database got almost no CPU and were unfinished at the
60-minute session limit.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.runtime import RuntimeResources
from coordinare_service_inference.environment.runtime_guest import guest_cpus, qemu_arguments
from pydantic import ValidationError

from coordinare.services.environment_capabilities import carrier_process_policy


@pytest.mark.parametrize(('cpus', 'guest'), [(8, 4), (2, 1), (1, 1), (5, 2)])
def test_the_guest_gets_half_the_cpus(cpus, guest):
    assert guest_cpus(SimpleNamespace(cpus=cpus)) == guest


def test_qemu_is_started_with_the_guest_share_of_cpus():
    resources = SimpleNamespace(memory_mib=4096, cpus=8)
    spec = SimpleNamespace(policy=SimpleNamespace(control_port=2222, resources=resources), endpoints=[])
    bundle = SimpleNamespace(emulator='/usr/bin/qemu-system-aarch64', firmware='/fw', guest_platform='linux/aarch64')
    paths = {name: Path('/' + name) for name in ('disk', 'seed', 'repo', 'cache', 'scratch', 'control')}
    arguments = qemu_arguments(spec, bundle, install=True, **paths)
    assert arguments[arguments.index('-smp') + 1] == '4'


def _resources(**changes):
    values = {'cpus': 8, 'memory_mib': 4096, 'cache_bytes': 8 * 1024**3, 'scratch_bytes': 16 * 1024**3,
              'reserve_bytes': 16 * 1024**3, 'max_seconds': 3600, **changes}
    return RuntimeResources.model_validate(values)


def test_a_two_hour_session_is_allowed_and_longer_is_not():
    assert _resources(max_seconds=7200).max_seconds == 7200
    with pytest.raises(ValidationError):
        _resources(max_seconds=7201)


def test_authors_see_the_session_budget_and_the_prebuilt_preference():
    runtime = SimpleNamespace(resources=SimpleNamespace(cpus=8, memory_mib=4096, max_seconds=7200))
    policy = carrier_process_policy(SimpleNamespace(runtime=runtime))
    assert policy['session_budget'] == {'seconds': 7200, 'container_cpus': 8, 'guest_cpus': 4}
    assert 'prebuilt' in policy['installation_guidance']
