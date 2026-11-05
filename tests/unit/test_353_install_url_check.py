"""An install URL the daemon finds missing reaches the reviewer before execution, and blocks its acceptance.

Dettmore e2e-56 spent an install generation (22 minutes) on install-ruby fetching a guessed Debian mirror path that
answered 404 (`debian/bookworm/main/...` for `debian/dists/bookworm/main/...`).
"""
from __future__ import annotations

import copy
import json

import httpx
import pytest
from coordinare_service_inference.environment.models import (
    Command,
    CoverageAssessmentProposal,
    EnvironmentManifest,
)

from coordinare.services import environment_url_check as check
from coordinare.services.environment_findings import FindingValidationError
from tests.unit.test_353_discovery_repair import (
    PATH,
    Replies,
    assessment,
    candidate,
    finding_responses,
    run,
)

BAD = 'http://deb.debian.org/debian/bookworm/main/binary-arm64/Packages.gz'
GOOD = 'https://cache.ruby-lang.org/pub/ruby/3.4/ruby-3.4.2.tar.gz'


def _manifest(*scripts: str):
    install = [Command(id=f'install-{index}', argv=['bash', '-c', script]) for index, script in enumerate(scripts)]
    return type('Manifest', (), {'install': install})()


def test_only_literal_urls_are_checked_each_with_the_commands_naming_it():
    manifest = _manifest(f"curl -fsSL {BAD} -o p.gz; curl -fsSL '{GOOD}' -o r.tgz",
                         f'curl -fsSL "{GOOD}"; curl https://x.example/v${{VER}}/a.tgz; curl {{cache}}/x; '
                         'curl https://x.example/a-*.tgz; wget https://x.example/{arch}/b')
    assert check.install_urls(manifest) == {BAD: ['install-0'], GOOD: ['install-0', 'install-1']}


@pytest.mark.parametrize('script', [
    # Review of 0fcecb85: a URL split by quotes, a printf template, a heredoc and a comment were all checked.
    'curl -fsSL "https://example.com/dl/"$NAME".tar.gz" -o a.tgz',
    "printf 'https://example.org/pkg-%s.tgz' 1.0 | xargs curl -O",
    'cat <<EOF > notes\nhttps://example.net/docs/old\nEOF',
    'curl -fsSL https://example.com/ok.tgz -o a.tgz # see https://example.net/docs/old',
    'echo https://example.net/docs/old',
    'curl -O "https://example.com/file[1-3].tgz"',
])
def test_only_whole_literal_arguments_of_a_fetch_tool_are_checked(script):
    found = check.install_urls(_manifest(script))
    assert 'https://example.net/docs/old' not in found and not any('%s' in url or '[' in url for url in found)
    assert set(found) <= {'https://example.com/ok.tgz'}


def _client(routes):
    def answer(request: httpx.Request) -> httpx.Response:
        status, headers = routes[(request.method, str(request.url))]
        return httpx.Response(status, headers=headers)
    return httpx.AsyncClient(transport=httpx.MockTransport(answer), follow_redirects=False)


@pytest.fixture
def public(monkeypatch):
    async def only_public(host, _port):
        return host != 'internal.example'
    monkeypatch.setattr(check, '_public', only_public)


@pytest.mark.asyncio
async def test_a_missing_url_is_reported_and_a_present_one_is_not(public):
    routes = {('HEAD', BAD): (404, {}), ('GET', BAD): (404, {}), ('HEAD', GOOD): (200, {})}
    async with _client(routes) as client:
        missing = await check.missing_install_urls(_manifest(f'curl {BAD}', f'curl {GOOD}'), client=client)
    assert missing == [{'url': BAD, 'status': 404, 'command_ids': ['install-0']}]


@pytest.mark.asyncio
async def test_a_server_refusing_head_is_asked_for_one_byte(public):
    routes = {('HEAD', GOOD): (405, {}), ('GET', GOOD): (206, {})}
    async with _client(routes) as client:
        assert await check.url_status(client, GOOD) == 206


@pytest.mark.asyncio
async def test_redirects_are_followed_but_never_to_a_private_address(public):
    moved = 'https://moved.example/ruby.tgz'
    routes = {('HEAD', GOOD): (302, {'location': moved}), ('HEAD', moved): (404, {}), ('GET', moved): (404, {}),
              ('HEAD', BAD): (301, {'location': 'http://internal.example/secret'})}
    async with _client(routes) as client:
        assert await check.url_status(client, GOOD) == 404
        assert await check.url_status(client, BAD) is None


@pytest.mark.asyncio
async def test_a_head_404_is_confirmed_with_a_get(public):
    routes = {('HEAD', GOOD): (404, {}), ('GET', GOOD): (206, {})}
    async with _client(routes) as client:
        assert await check.url_status(client, GOOD) == 206


@pytest.mark.asyncio
async def test_an_unreachable_url_is_not_judged_missing(public):
    def refuse(request):
        raise httpx.ConnectError('refused', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        assert await check.missing_install_urls(_manifest(f'curl {BAD}'), client=client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(('host', 'public_address'), [('127.0.0.1', False), ('169.254.169.254', False),
                                                      ('10.0.0.1', False), ('::1', False), ('8.8.8.8', True),
                                                      # IPv6 forms that carry a private IPv4 address.
                                                      ('64:ff9b::a00:1', False), ('2002:a00:1::1', False),
                                                      ('::ffff:10.0.0.1', False), ('2606:4700:4700::1111', True)])
async def test_only_globally_routable_addresses_are_requested(host, public_address):
    assert await check._public(host, 443) is public_address


def _proposal(verdict='accept', affected=(), decisions=()):
    findings = [{'category': 'execution_definition', 'issue': 'The Packages.gz path answers 404', 'sources': [PATH],
                 'affected_ids': list(affected)}] if affected else []
    return CoverageAssessmentProposal.model_validate({
        'verdict': verdict, 'rationale': 'judged', 'sources': [PATH], 'capability_unavailable': False,
        'findings': findings, 'decisions': list(decisions)})


MISSING = [{'url': BAD, 'status': 404, 'command_ids': ['install-ruby']}]


def test_an_accept_is_refused_until_a_finding_names_the_command():
    with pytest.raises(FindingValidationError) as caught:
        check.require_missing_urls_named(_proposal(), MISSING, [])
    assert caught.value.code == 'review_findings_invalid' and 'install-ruby' in str(caught.value)
    check.require_missing_urls_named(_proposal('revise', ['install-ruby']), MISSING, [])
    check.require_missing_urls_named(_proposal(), [], [])


def test_a_url_an_earlier_manifest_was_corrected_for_is_evidence_only():
    """A daemon wrong about one URL costs one revision, never the attempt."""
    check.require_missing_urls_named(_proposal(), [{**MISSING[0], 'reported_before': True}], [])


def test_the_review_part_error_names_the_command_and_its_url():
    error = check.missing_url_error(['install-ruby'], MISSING)
    assert error['unnamed_command_ids'] == ['install-ruby'] and error['daemon_url_checks'] == MISSING


@pytest.mark.asyncio
async def test_the_reviewer_is_told_and_cannot_accept_until_it_reports_the_missing_url():
    original = candidate()
    target = original['checks'][0]['id']
    revised = copy.deepcopy(original)
    revised['checks'][0]['argv'].append('--strict')
    missing = [{'url': BAD, 'status': 404, 'command_ids': [target]}]
    answers = [missing, []]

    async def checker(manifest: EnvironmentManifest):
        return answers.pop(0)

    flagged = assessment(original, 'revise')
    flagged['assessment']['findings'][0].update(category='execution_definition', affected_ids=[target])
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': original},
        {'tool': 'read_file', 'path': PATH}, assessment(original), flagged,
        {'manifest': revised, 'finding_responses': finding_responses()},
        {'tool': 'read_file', 'path': PATH}, assessment(revised, retained=True)])
    manifest, review = await run(replies, url_checker=checker)
    assert review.verdict == 'accept' and manifest.digest() == review.manifest_digest
    reviewer_prompt = json.loads(replies.messages[2][1]['content'])
    assert reviewer_prompt['daemon_url_checks'] == missing and 'daemon_url_check_note' in reviewer_prompt
    correction = json.loads(replies.messages[4][-1]['content'])
    assert correction['validation_errors'][0]['code'] == 'review_findings_invalid'
    assert target in correction['validation_errors'][0]['guidance']
    assert 'daemon_url_checks' not in json.loads(replies.messages[6][1]['content'])


def test_the_global_review_part_cannot_accept_while_a_missing_url_is_unnamed():
    from types import SimpleNamespace

    from coordinare.services.environment_scope import _ResponseReferenceError
    from coordinare.services.environment_shard_construction import ReviewIds, validate_review_part

    inspection = SimpleNamespace(inputs={PATH: 'a' * 64})
    accepted = {'assessment': _proposal().model_dump(mode='json')}
    told = ReviewIds(obligations={'install-ruby'}, missing_urls=MISSING)
    with pytest.raises(_ResponseReferenceError) as caught:
        validate_review_part(accepted, inspection=inspection, owned=[], capacity=4, known=told)
    assert caught.value.errors[0]['unnamed_command_ids'] == ['install-ruby']
    flagged = {'assessment': _proposal('revise', ['install-ruby']).model_dump(mode='json')}
    validate_review_part(flagged, inspection=inspection, owned=[], capacity=4, known=told)
    # A part with no finding capacity left cannot report one.
    validate_review_part(accepted, inspection=inspection, owned=[], capacity=0, known=told)
    # A check group's part is never told about install URLs, so it is never held for them.
    validate_review_part(accepted, inspection=inspection, owned=[], capacity=4, known=ReviewIds(obligations={'install-ruby'}))
