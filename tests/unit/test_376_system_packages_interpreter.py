"""nare-native-013: the advertised system-package helper ran under the project's own virtualenv and was not found.

An install runs with the manifest's activation PATH first, so a bare ``python3`` is whatever interpreter the project
put there. The helper is advertised with the carrier interpreter it is installed into.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_helper_is_advertised_with_the_carrier_interpreter_never_a_path_lookup() -> None:
    from coordinare_service_inference.environment.system_packages import CARRIER_PYTHON, COMMAND

    interpreter = COMMAND.split()[0]
    assert interpreter == CARRIER_PYTHON and interpreter.startswith('/')
    assert COMMAND.endswith('-m coordinare_service_inference.environment.system_packages --into {cache}/deps <packages>')


def test_the_carrier_interpreter_is_the_one_the_carrier_image_installs_the_package_into() -> None:
    from coordinare_service_inference.environment.system_packages import CARRIER_PYTHON

    dockerfile = (ROOT / 'agent' / 'performer' / 'Dockerfile.base').read_text()
    # Official python images install their interpreter, and its pip, under /usr/local/bin.
    assert re.search(r'^FROM python:3\.\d+(-slim)?\s*$', dockerfile, re.MULTILINE)
    assert CARRIER_PYTHON == '/usr/local/bin/python3'
    assert re.search(r'WORKDIR /pkgs/service_inference\n(?:.*\n){0,3}RUN pip install --no-cache-dir \.', dockerfile)


def test_guidance_and_correction_give_the_same_command() -> None:
    from coordinare_service_inference.environment.system_packages import COMMAND

    from coordinare.services.environment_admission import GRAPH_GUIDANCE

    assert COMMAND in GRAPH_GUIDANCE['system_package_install']
    assert 'python3 -m coordinare_service_inference' not in GRAPH_GUIDANCE['system_package_install'].replace(COMMAND, '')
