"""Static admission rules: a reachability gate, an uncreated activation path, each corrected during discovery.

v40's gen1 manifest gated four installs on install-connectivity-probe, which exits 1 when get.helm.sh is unreachable.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import Activation, Command
from coordinare_service_inference.environment.static_rules import (
    connectivity_probe,
    validate_static_rules,
)
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from coordinare.services import environment_discovery as discovery_module
from coordinare.services.environment_admission import GRAPH_GUIDANCE, _validation_feedback
from coordinare.services.environment_shard_construction import validate_global

GATE_SCRIPT = ('set -e; for url in https://deb.debian.org/ https://get.helm.sh/; do curl -fsIL --retry 2 --max-time 30 "$url" '
               '> /dev/null || { echo "CONNECTIVITY-FAIL: $url"; exit 1; }; done; echo install-network-ok')


def install(id_: str, script: str, *, depends_on: list[str] | None = None, **env: str) -> Command:
    return Command(id=id_, argv=['bash', '-c', script], env=env, depends_on=depends_on or [])


@pytest.mark.parametrize('script', [
    GATE_SCRIPT,
    'curl -I https://example.com/',
    'curl -sf https://example.com/ >/dev/null',
    'curl -sSf -o /dev/null https://example.com/',
    'wget -q --spider https://example.com/',
    'ping -c 1 example.com',
    'nc -z example.com 443',
    'set -e; ping -c1 example.com; echo ok',
])
def test_a_reachability_test_is_connectivity_only(script):
    assert connectivity_probe(install('gate', script))


@pytest.mark.parametrize('script', [
    'curl -fsSL -o /tmp/helm.tgz https://get.helm.sh/helm.tgz',  # a download that keeps the artifact
    'set -e; curl -fsI https://example.com/ && mkdir -p {cache}/bin',  # also writes something
    'curl https://example.com/ | tar -x',  # output consumed, not discarded
    'nc example.com 443',  # no -z: not a reachability-only probe
    'echo ok; exit 0',  # no network test at all
    'apt-get install -y curl',  # installs, never a gate
])
def test_a_command_that_does_more_than_test_reachability_is_not_a_gate(script):
    assert not connectivity_probe(install('step', script))


def test_a_gate_depended_on_by_another_install_is_a_named_correctable_error():
    gate = install('install-connectivity-probe', GATE_SCRIPT)
    apt = install('install-apt-prereqs', 'apt-get update', depends_on=['install-connectivity-probe'])
    with pytest.raises(PydanticCustomError) as caught:
        validate_static_rules([gate, apt], Activation())
    assert caught.value.type == 'connectivity_gate'
    assert caught.value.context == {'command_id': 'install-apt-prereqs', 'unavailable': ['install-connectivity-probe']}


def test_a_gate_nobody_depends_on_is_allowed():
    validate_static_rules([install('install-connectivity-probe', GATE_SCRIPT),
                           install('install-apt', 'apt-get update')], Activation())


def test_a_download_that_others_depend_on_is_allowed():
    download = install('install-helm', 'curl -fsSL -o /tmp/helm.tgz https://get.helm.sh/helm.tgz')
    validate_static_rules([download, install('install-ruby', 'ruby --version', depends_on=['install-helm'])], Activation())


def test_an_activation_path_no_install_creates_is_named_by_its_position():
    activation = Activation(path=['{cache}/bin', '{cache}/tools/bin'])
    with pytest.raises(PydanticCustomError) as caught:
        validate_static_rules([install('install-bin', 'mkdir -p {cache}/bin')], activation)
    assert caught.value.type == 'activation_path_uncreated'
    assert caught.value.context == {'command_id': 'activation', 'unavailable': ['activation-path-1']}


@pytest.mark.parametrize(('path', 'script', 'env'), [
    ('{cache}/rbenv/shims', 'mkdir -p {cache}/rbenv/shims', {}),  # the path itself
    ('{cache}/rbenv/versions/3.4.2/bin', 'mkdir -p {cache}/rbenv/versions/3.4.2/bin/extra', {}),  # something below it
    ('{cache}/pgroot/usr/bin', 'dpkg -x pkg.deb {cache}/pgroot', {}),  # an extraction into an ancestor populates it
    ('{cache}/rbenv/shims', 'true', {'RBENV_ROOT': '{cache}/rbenv'}),  # an ancestor named in the command's env
])
def test_an_install_that_creates_or_populates_the_path_satisfies_it(path, script, env):
    validate_static_rules([install('install-ruby', script, **env)], Activation(path=[path]))


def test_a_sibling_sharing_a_prefix_does_not_create_the_path():
    with pytest.raises(PydanticCustomError) as caught:
        validate_static_rules([install('install-bin', 'mkdir -p {cache}/bin2')], Activation(path=['{cache}/bin']))
    assert caught.value.context['unavailable'] == ['activation-path-0']


def test_paths_outside_the_cache_and_templated_paths_are_never_flagged():
    validate_static_rules([install('install-none', 'true')],
                          Activation(path=['/usr/local/bin', '${RBENV_BIN}/bin', '{cache}/$LAYER/bin']))


def test_validate_global_rejects_a_connectivity_gate_in_the_global_phase():
    data = {
        'requirements': [], 'empty_reason': 'nothing to install', 'activation': {'path': []},
        'checks': [{'id': 'check-ok', 'argv': ['true']}],
        'install': [{'id': 'install-connectivity-probe', 'argv': ['bash', '-c', GATE_SCRIPT]},
                    {'id': 'install-apt', 'argv': ['bash', '-c', 'apt-get update'], 'depends_on': ['install-connectivity-probe']}],
        'services': [],
    }
    with pytest.raises(ValidationError) as failure:
        validate_global(data)
    [error] = _validation_feedback(failure.value)
    assert error == {'location': [], 'code': 'connectivity_gate', 'command_id': 'install-apt',
                     'unavailable': ['install-connectivity-probe'], 'guidance': GRAPH_GUIDANCE['connectivity_gate']}
    assert 'CONNECTIVITY-FAIL' not in str(error)


def test_guidance_names_the_rule_without_echoing_model_text():
    assert 'depends_on' in GRAPH_GUIDANCE['connectivity_gate']
    assert 'activation' in GRAPH_GUIDANCE['activation_path_uncreated']


def test_the_candidate_validator_admits_a_connectivity_gate_only_as_a_correctable_error():
    gate = install('install-connectivity-probe', GATE_SCRIPT)
    apt = install('install-apt-prereqs', 'apt-get update', depends_on=['install-connectivity-probe'])
    construction = SimpleNamespace(budget=SimpleNamespace(scope_contract=None), capabilities=SimpleNamespace(platform='linux/x86_64', runtime=None),
        admit=lambda data, inputs: SimpleNamespace(install=[gate, apt], activation=Activation(), checks=[], services=[]))
    validator = discovery_module._CandidateValidator(
        discovery=SimpleNamespace(inputs={}), construction=construction, required_ids={}, prior_manifests=[],
        obligation_ledger=discovery_module._ObligationLedger(), budget=SimpleNamespace())
    with pytest.raises(ValidationError) as caught:
        validator({'requirements': []})
    [error] = _validation_feedback(caught.value)
    assert error['code'] == 'connectivity_gate' and error['command_id'] == 'install-apt-prereqs'
