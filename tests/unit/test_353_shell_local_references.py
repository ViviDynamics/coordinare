"""A ${NAME} for a variable the command assigns itself is corrected at admission, not refused at installation.

v37, v39 and v40 each lost an execution repair: install-gh assigned A (or HA and GHV) and then wrote ${A}, which the
runner treats as a template filled from its environment and refuses when the name is absent.
"""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import Command, validate_shell_locals
from pydantic_core import PydanticCustomError


def command(script: str, **env: str) -> Command:
    return Command(id='install-gh', argv=['bash', '-c', script], env=env)


@pytest.mark.parametrize(('script', 'names'), [
    ('case $(uname -m) in aarch64) HA=arm64;; esac; curl -o gh.tgz https://example/gh_${HA}.tgz', ['HA']),
    ('GHV=2.42.0; curl https://example/v${GHV}/gh_${GHV}.tgz', ['GHV']),
    ('for f in *.deb; do dpkg -x ${f} out; done', ['f']),
    ('read -r LINE < file; echo ${LINE}', ['LINE']),
])
def test_a_variable_the_command_assigns_is_named_when_written_as_a_template(script, names):
    with pytest.raises(PydanticCustomError) as caught:
        validate_shell_locals([command(script)], [])
    assert caught.value.type == 'shell_local_template_reference'
    assert caught.value.context == {'command_id': 'install-gh', 'unavailable': names}


@pytest.mark.parametrize('script', [
    'curl -H "Authorization: Bearer ${API_TOKEN}" https://example',  # a runtime credential the command never assigns
    'BILLINGVENDOR_API_KEY=${BILLINGVENDOR_API_KEY} bin/rails runner "exit 0"',  # passes a runtime value through to a child
    'HA=arm64; curl https://example/gh_$HA.tgz',  # a shell variable written without braces
    'export RBENV_ROOT=/x; echo ${RBENV_ROOT}',  # declared by activation
    'curl https://example/?token=${TOKEN}',  # a query parameter is not an assignment of TOKEN
])
def test_templates_the_runner_can_fill_are_never_flagged(script):
    validate_shell_locals([command(script)], ['RBENV_ROOT'])


def test_a_name_the_command_declares_in_its_own_env_is_never_flagged():
    validate_shell_locals([command('LEVEL=debug; run --level ${LEVEL}', LEVEL='info')], [])
