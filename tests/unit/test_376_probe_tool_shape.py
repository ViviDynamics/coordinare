"""The native probe tool accepts each command's question and citations inside the command (nare-native-005).

A repair author sent `questions` inside each command; the strict tool schema refused the call, the author returned its
manifest without the required preflight, and the attempt held after 3.7 hours. Each per-command value has exactly one
place to go, so it is moved into the keyed maps before strict validation; a conflicting value is still refused.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.services.agent_runtime.schema import native_tool_schema
from coordinare.services.environment_probes import (
    ProbeRequest,
    probe_tool_schema,
    settle_probe_arguments,
)


def command(identifier: str, **extra: object) -> dict[str, object]:
    return {'id': identifier, 'argv': ['python3', '--version'], **extra}


def test_per_command_questions_and_citations_settle_into_the_strict_request() -> None:
    sent = {'commands': [command('a1', question='Which python3?', citations=['pyproject.toml']),
                         command('a2', question='Is docker reachable?', citations=['README.md'])]}
    settled = settle_probe_arguments(sent)
    request = ProbeRequest.model_validate(settled)
    assert request.questions == {'a1': 'Which python3?', 'a2': 'Is docker reachable?'}
    assert request.citations == {'a1': ['pyproject.toml'], 'a2': ['README.md']}
    assert all('question' not in item for item in settled['commands'])
    assert 'question' in sent['commands'][0], 'the caller arguments are not mutated'


def test_mixed_forms_settle_and_the_canonical_form_is_unchanged() -> None:
    mixed = {'commands': [command('a1', question='q1'), command('a2')],
             'questions': {'a2': 'q2'}, 'citations': {'a1': ['x'], 'a2': ['y']}}
    assert ProbeRequest.model_validate(settle_probe_arguments(mixed)).questions == {'a1': 'q1', 'a2': 'q2'}
    canonical = {'commands': [command('a1')], 'questions': {'a1': 'q'}, 'citations': {'a1': ['x']}}
    assert settle_probe_arguments(canonical) == canonical


def test_both_singular_and_plural_per_command_questions_conflicting_is_refused() -> None:
    both = {'commands': [command('a1', question='one', questions='two', citations=['x'])]}
    with pytest.raises(ValidationError):
        ProbeRequest.model_validate(settle_probe_arguments(both))


def test_a_conflicting_per_command_value_is_left_for_strict_validation() -> None:
    conflicting = {'commands': [command('a1', question='inside')], 'questions': {'a1': 'outside'},
                   'citations': {'a1': ['x']}}
    with pytest.raises(ValidationError):
        ProbeRequest.model_validate(settle_probe_arguments(conflicting))


@pytest.mark.parametrize('data', [None, [], {'commands': 'x'}, {'commands': [None, 3, {'argv': []}]}])
def test_unexpected_shapes_pass_through_untouched(data: object) -> None:
    assert settle_probe_arguments(data) == data


def test_the_tool_schema_accepts_the_shape_the_author_sent() -> None:
    jsonschema = pytest.importorskip('jsonschema')
    schema = native_tool_schema(probe_tool_schema())
    sent = {'commands': [command('a1', question='Which python3?', citations=['pyproject.toml'])]}
    jsonschema.validate(sent, schema)
    jsonschema.validate({'commands': [command('a1')], 'questions': {'a1': 'q'}, 'citations': {'a1': ['x']}}, schema)


def test_nare_accepts_the_author_shape_it_refused_in_nare_native_005() -> None:
    from nare.tools import check_arguments

    from coordinare.services.environment_probes import ProbeRequest as Strict

    sent = {'commands': [command(f'a{index}', questions='Which tool?', question='Which tool?', citations=['README.md'])
                         for index in range(1, 3)]}
    before = check_arguments('probe', native_tool_schema(Strict.model_json_schema()), dict(sent))[1]
    assert any('missing required parameter questions' in error for error in before)
    # The live call: `questions` (plural) inside each command, no top-level maps.
    sent = {'commands': [command(f'a{index}', questions='Which tool?', citations=['README.md']) for index in range(1, 3)]}
    args, errors = check_arguments('probe', native_tool_schema(probe_tool_schema()), sent)
    assert errors == []
    assert Strict.model_validate(settle_probe_arguments(args)).questions == {'a1': 'Which tool?', 'a2': 'Which tool?'}
