"""The replay extractor redacts consistently and refuses to write a fixture its denylist scan matches."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from scripts import replay_extract as extract

from tests.replay.fixtures import ReplayStep, all_fixture_paths, load_fixture, parse_fixture
from tests.replay.transport import ReplayDiverged, ReplayEnded, ReplayTransport

PLANTED_HOST = 'models.' + 'internal-example' + '.lan'


def _fixture(**state: Any) -> dict[str, Any]:
    return {'format': 1, 'run': 'synthetic-run', 'head': 'unknown', 'hold': {'code': None, 'stage': None},
            'seam': 'nare_transport', 'redaction': {'version': 1}, 'state': state,
            'steps': [{'call': 'c', 'session': None, 'turn': 1, 'reply': {'text': '<text:00000000>'},
                       'recorded': {'refusal_codes': [], 'guidance_ids': [], 'charged': False}}]}


def test_the_scan_refuses_a_planted_hostname_and_a_home_path(tmp_path: Path) -> None:
    out = tmp_path / 'fixtures' / 'synthetic-run' / 'case.json'
    with pytest.raises(extract.RefusedFixture) as refused:
        extract.write_fixture(_fixture(note=f'served by {PLANTED_HOST}'), out, denied=[PLANTED_HOST], local=False)
    assert 'denied literal #1' in str(refused.value)
    assert PLANTED_HOST not in str(refused.value)  # The refusal never repeats the value it refused.
    home = '/' + 'Users' + '/someone/checkout'
    with pytest.raises(extract.RefusedFixture) as refused:
        extract.write_fixture(_fixture(cwd=home), out, denied=[], local=False)
    assert 'home path' in str(refused.value)
    assert not out.exists()
    extract.write_fixture(_fixture(cwd='{repo}'), out, denied=[PLANTED_HOST], local=False)
    assert parse_fixture(json.loads(out.read_text()), out).run == 'synthetic-run'


@pytest.mark.parametrize(('value', 'problem'), [
    ('see https://' + 'gateway.corp.invalid/v1', 'url host outside allowlist'),
    ('mail ' + 'someone' + '@' + 'corp.invalid', 'email address'),
    ('key ' + 'sk9Qw2Lr7Tz4Xv1Bn8Mc5Kp3Hd6Jf0Gs', 'high-entropy string'),
])
def test_the_scan_refuses_urls_emails_and_high_entropy_strings(value: str, problem: str) -> None:
    assert any(item.startswith(problem) for item in extract.scan(json.dumps({'note': value})))


@pytest.mark.parametrize('field', ['serving_route', 'base_cache_dir'])
def test_the_scan_refuses_private_route_and_cache_fields(field: str) -> None:
    assert extract.scan(json.dumps({field: 'x'})) == [f'forbidden field {field}']


def test_the_scan_allows_digests_ids_and_redacted_values() -> None:
    text = json.dumps({'id': 'c00-replay-done-jsonl', 'digest': 'a' * 64, 'argv': ['t01', 't02'],
                       'text': '<text:5c591403>', 'url': 'https://example.com/x', 'criterion': 'S9550150d5c470822'})
    assert extract.scan(text) == []


def test_every_committed_fixture_parses_and_passes_the_scan() -> None:
    paths = all_fixture_paths()
    assert {path.parent.name for path in paths} >= {'nare-native-038', 'nare-native-041', 'nare-native-045',
                                                     'nare-native-046'}
    denied = extract.local_denylist(extract._repo_root())
    for path in paths:
        assert extract.scan(path.read_text(encoding='utf-8'), denied=denied) == [], path
        load_fixture(path.parent.name, path.stem)


def test_redaction_is_consistent_so_duplicate_commands_stay_duplicates() -> None:
    redactor = extract.Redactor()
    first = {'id': 'check-a', 'argv': ['bash', '-c', 'run thing'], 'cwd': '{repo}', 'env': {'HOME': '/root/x'},
             'depends_on': ['inst-a'], 'required': True, 'scope': []}
    second = {**first, 'id': 'c00-a'}
    other = {**first, 'id': 'check-b', 'argv': ['bash', '-c', 'run other'], 'cwd': '{cache}/sub'}
    redacted = [redactor.value(item) for item in (first, second, other)]
    assert redacted[0]['argv'] == redacted[1]['argv'] == ['t01', 't02', 't03']
    assert redacted[2]['argv'][:2] == ['t01', 't02'] and redacted[2]['argv'][2] not in redacted[0]['argv']
    assert redacted[0]['cwd'] == '{repo}' and redacted[2]['cwd'].startswith('{cache}/t')
    assert redacted[0]['env'] == {'ENV_01': 't04'} == redacted[1]['env']
    assert [item['id'] for item in redacted] == ['check-a', 'c00-a', 'check-b']
    assert redacted[0]['depends_on'] == ['inst-a'] and redacted[0]['required'] is True
    requirement = redactor.value({'id': 'req-a', 'description': 'Python 3.12 environment', 'source': 'pyproject.toml',
                                  'checks': ['check-a']})
    assert requirement == {'id': 'req-a', 'description': '<text:' + hashlib.sha256(
        b'Python 3.12 environment').hexdigest()[:8] + '>', 'source': 'src/f01', 'checks': ['check-a']}


def test_local_copies_go_only_to_an_ignored_replay_corpus(tmp_path: Path) -> None:
    with pytest.raises(extract.RefusedFixture):
        extract.write_fixture(_fixture(), tmp_path / 'elsewhere' / 'case.json', denied=[], local=True)


def _write_run(root: Path) -> Path:
    """A miniature run directory: one session with a refused candidate and an admitted one, and one transcript."""
    run = root / 'mini-run'
    identity = {'attempt_id': 'a' * 64, 'phase': 'discovery', 'role': 'author'}
    candidate = {'manifest': {'install': [{'id': 'inst-a', 'argv': ['make'], 'cwd': '{repo}'}],
                              'checks': [{'id': 'check-a', 'argv': ['make', 'test'], 'depends_on': ['inst-a']}],
                              'requirements': [{'id': 'req-a', 'description': 'tests run', 'source': 'Makefile',
                                                'checks': ['check-a']}]}}
    refusal = {'code': 'clean_dependency_install_only', 'command_id': 'check-a', 'command_ids': ['check-a'],
               'unavailable': ['inst-a'], 'guidance': 'remove the install ids', 'location': []}
    session = {'messages': [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'task'}]},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-1', 'name': 'read_source',
                                           'input': {'path': 'Makefile'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-1', 'is_error': True,
                                      'content': 'read_source: unknown parameter path2'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '```json\n' + json.dumps(candidate) + '\n```'}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': extract.CORRECTION_PREFIX + ' the schema: ...'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': json.dumps(candidate)}]},
    ], 'schema_correction_log': [{'kind': 'domain', 'charged': True, 'codes': [json.dumps(refusal)]}]}
    checkpoint = {'version': 1, 'nare_version': 'x', 'identity': identity, 'session_text': json.dumps(session)}
    folder = run / 'daemon-state' / 'bootstrap-agents' / identity['attempt_id']
    folder.mkdir(parents=True)
    (folder / ('v1-' + 'b' * 64 + '.json')).write_text(json.dumps(checkpoint, separators=(',', ':')))
    transcripts = run / 'daemon-transcripts' / '_conductor' / 'system'
    transcripts.mkdir(parents=True)
    rows = [{'seq': 0, 'type': 'run_header', 'conductor': {'kind': 'environment_discovery', 'parameters': {
                'native_request': {'tools': [{'name': 'read_source'}], 'tool_choice': 'auto'}}}},
            {'seq': 1, 'type': 'tool_call', 'tool_name': 'read_source', 'call_id': 'call-1', 'input': {'path': 'Makefile'}}]
    (transcripts / ('c' * 32 + '.jsonl')).write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    (run / 'nare').mkdir()
    (run / 'nare' / 'environment-attempt.json').write_text(json.dumps({'last_error': 'attempt_schema_limit'}))
    return run


def test_extraction_joins_replies_to_their_refusals_and_redacts(tmp_path: Path) -> None:
    run = _write_run(tmp_path)
    out = tmp_path / 'out'
    assert extract.main([str(run), '--case', 'mini', '--out', str(out), '--checkpoint', 'aaaa/bbbb',
                         '--turns', '2-3', '--transcript', 'cccc']) == 0
    fixture = load_fixture('mini-run', 'mini', root=out)
    refused, admitted, transcript = fixture.steps
    assert refused.turn == 2 and refused.recorded == {'refusal_codes': ['clean_dependency_install_only'],
                                                      'guidance_ids': ['check-a', 'inst-a'], 'charged': True}
    assert admitted.turn == 3 and admitted.recorded['refusal_codes'] == []
    check = refused.reply['candidate']['manifest']['checks'][0]
    assert check['id'] == 'check-a' and check['depends_on'] == ['inst-a'] and check['argv'][0].startswith('t')
    assert refused.reply['candidate']['manifest']['requirements'][0]['source'] == 'src/f01'
    assert transcript.request == {'tools': ['read_source'], 'tool_choice': 'auto'}
    assert transcript.turn == 1 and transcript.recorded['refusal_codes'] == ['invalid_tool_arguments']
    assert transcript.reply['tool_calls'] == [{'name': 'read_source', 'args': {'path': '<text:' + hashlib.sha256(
        b'Makefile').hexdigest()[:8] + '>'}}]
    assert fixture.hold['code'] == 'attempt_schema_limit'
    assert 'Makefile' not in (out / 'mini-run' / 'mini.json').read_text()


@pytest.mark.asyncio
async def test_the_replay_transport_stops_where_the_harness_leaves_the_recording() -> None:
    step = ReplayStep('c', None, 1, {'tool_calls': [{'name': 'continue_required_reads', 'args': {}}]},
                      {'refusal_codes': [], 'guidance_ids': [], 'charged': False},
                      {'tools': ['continue_required_reads'], 'tool_choice': 'required'})
    diverging = ReplayTransport([step])
    with pytest.raises(ReplayDiverged):
        await diverging.turn([], [{'name': 'review_evidence'}], tool_choice=None)
    with pytest.raises(ReplayDiverged):
        diverging.assert_followed()
    following = ReplayTransport([step])
    reply = await following.turn([], [{'name': 'continue_required_reads'}], tool_choice='required')
    assert [call.name for call in reply.tool_calls] == ['continue_required_reads']
    following.assert_followed()
    with pytest.raises(ReplayEnded):
        await following.turn([], [{'name': 'continue_required_reads'}], tool_choice='required')
