"""nare-native-032: an install ran the daemon's system-package command as ``python3 -m ...``; the activation PATH
resolved python3 to a virtual environment without the module, and every check behind the install was skipped. Only the
carrier's interpreter, as the documented command gives it, is admitted."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import Activation, Command
from coordinare_service_inference.environment.static_rules import (
    system_packages_interpreter,
    validate_static_rules,
)
from coordinare_service_inference.environment.system_packages import COMMAND
from pydantic_core import PydanticCustomError

MODULE = '-m coordinare_service_inference.environment.system_packages --into {cache}/deps bubblewrap'


def install(script: str) -> Command:
    return Command(id='install-sandbox-tools', argv=['bash', '-c', script])


@pytest.mark.parametrize('script', [f'python3 {MODULE}', f'mkdir -p {{cache}}/deps && python {MODULE}',
                                    f'{{cache}}/uvtools/bin/python3 {MODULE} && bwrap --version'])
def test_another_interpreter_is_refused_with_the_documented_command(script: str) -> None:
    assert system_packages_interpreter(install(script))
    with pytest.raises(PydanticCustomError) as refused:
        validate_static_rules([install(script)], Activation())
    assert refused.value.type == 'system_packages_interpreter' and COMMAND in refused.value.message()


def test_the_documented_command_and_unrelated_modules_pass() -> None:
    for script in (f'/usr/local/bin/python3 {MODULE} && {{cache}}/deps/bin/bwrap --version',
                   'python3 -m venv {cache}/venv', 'python3 -m pip --version'):
        assert not system_packages_interpreter(install(script))
    validate_static_rules([install(f'/usr/local/bin/python3 {MODULE}')], Activation())


def test_the_correction_names_the_documented_command() -> None:
    from coordinare.services.environment_admission import GRAPH_GUIDANCE

    assert COMMAND in GRAPH_GUIDANCE['system_packages_interpreter']
