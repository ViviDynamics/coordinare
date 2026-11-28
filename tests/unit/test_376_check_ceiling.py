"""Maintainer ruling 2026-11-28 (nare-native-047): a check runs one small sample and is given at most 900 seconds.

047 accepted bin/build (nare's whole suite) and a 60-minute benchmark tier as checks on prompt guidance alone; they
spent hours of the installer session and the first debug resume's whole hour.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import Activation, Command
from coordinare_service_inference.environment.static_rules import (
    CHECK_TIMEOUT_CEILING,
    validate_static_rules,
)
from pydantic_core import PydanticCustomError

from coordinare.services import environment_discovery as discovery
from coordinare.services.environment_discovery_prompts import TIMEOUT_CEILINGS


def _command(identifier: str, timeout: float) -> Command:
    return Command.model_validate({'id': identifier, 'argv': ['true'], 'timeout_seconds': timeout})


def test_every_check_over_the_ceiling_is_named_at_once():
    checks = [_command('c-sample', 300), _command('c-suite', 1800), _command('c-bench', 3600)]
    with pytest.raises(PydanticCustomError) as refused:
        validate_static_rules([], Activation(), all_checks=checks)
    assert refused.value.type == 'check_timeout_ceiling'
    assert refused.value.context['command_ids'] == ['c-suite', 'c-bench']
    assert str(int(CHECK_TIMEOUT_CEILING)) in refused.value.message()


def test_installs_keep_the_command_limit_and_a_check_at_the_ceiling_is_admitted():
    validate_static_rules([_command('inst-image', 3600)], Activation(),
                          all_checks=[_command('c-sample', CHECK_TIMEOUT_CEILING)])


def test_admission_judges_every_check_including_executed_ones():
    manifest = SimpleNamespace(checks=[_command('c-ran', 600), _command('c-new', 300)], services=[], local_credentials=[])
    budget = SimpleNamespace(executed_manifests=[SimpleNamespace(checks=[_command('c-ran', 600)])])
    rules = discovery._carrier_rules(manifest, SimpleNamespace(runtime=None), budget)
    assert [check.id for check in rules['all_checks']] == ['c-ran', 'c-new']
    assert str(int(CHECK_TIMEOUT_CEILING)) in TIMEOUT_CEILINGS
    assert TIMEOUT_CEILINGS in discovery.EXECUTABLE_REVIEW_CRITERIA
