from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from coordinare.graph.nodes.classify_human_feedback import classify_human_feedback
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services.github import GitHubService
from coordinare.services.pr_conversation_feedback import (
    ConversationPollLimits,
    acknowledge_pr_conversation_feedback,
    poll_pr_conversation_feedback,
)


def comment(cid=1, body='Please add a regression test', author='alice', second=1):
    return {'id': cid, 'body': body, 'author': author,
            'updated_at': f'2026-10-08T10:00:{second:02d}Z',
            'html_url': f'https://github.com/acme/repo/pull/21#issuecomment-{cid}'}


def state_for(comments):
    state = initial_state()
    state.update(current_card={'id': 'card', 'pr_node_id': 'PR21', 'pr_number': 21},
                 human_reviewers=['Alice'], trusted_bot_reviewers=['trusted[bot]'])
    github = SimpleNamespace(get_pr_conversation_comments=AsyncMock(return_value=comments),
                             get_pr_reviews=AsyncMock(return_value=[]), move_card=AsyncMock())
    state['github_service'] = github
    return state, github


async def poll(state, github, **kwargs):
    return await poll_pr_conversation_feedback(state, github, pr_node_id='PR21', pr_number=21, limits=ConversationPollLimits(**kwargs))


@pytest.mark.asyncio
async def test_request_replays_until_relay_commit_then_acknowledges_after_restore():
    state, github = state_for([comment()])
    first = await poll(state, github)
    assert len(first) == 1
    assert first[0]['source'] == 'pr_comment'
    assert first[0]['comment_id'] == '1'
    assert first[0]['comment_url'].endswith('#issuecomment-1')
    assert first[0]['state'] == 'COMMENTED'
    assert first[0]['author_type'] == 'HUMAN'
    assert state['pr_comment_tracking']['versions'] == {}
    assert state['pr_comment_tracking']['updated_since'] is None
    assert await poll(copy.deepcopy(state), github) == first
    state['pending_reviews'] = first
    await classify_human_feedback(state)
    assert state['relay_feedback'] == first
    assert first[0]['id'] in state['processed_review_ids']
    restored = copy.deepcopy(state)
    assert await poll(restored, github) == []
    assert restored['pr_comment_tracking']['versions']['1']
    assert restored['pr_comment_tracking']['updated_since'] == comment()['updated_at']
    assert await poll(restored, github) == []
    assert github.get_pr_conversation_comments.call_args.kwargs['since'] == '2026-10-08T10:00:00Z'


@pytest.mark.asyncio
@pytest.mark.parametrize('body,author', [
    ('Thanks, looks good!', 'alice'), ('Approved, ship it', 'alice'),
    ('Build passed; fix count: 0', 'trusted[bot]'),
    ('<!-- coordinare:status --> Please fix the build', 'alice'),
    ('Please fix the bug', 'stranger'), ('FYI the pipeline is running', 'alice'),
])
async def test_noise_and_unauthorized_comments_never_dispatch(body, author):
    state, github = state_for([comment(body=body, author=author)])
    assert await poll(state, github) == []
    assert state['pr_comment_tracking']['versions']['1']


@pytest.mark.asyncio
async def test_trusted_bot_can_request_work():
    state, github = state_for([comment(author='trusted[bot]')])
    assert (await poll(state, github))[0]['author_type'] == 'TRUSTED_BOT'


@pytest.mark.asyncio
async def test_edits_reverts_and_unchanged_body_edits():
    state, github = state_for([comment()])
    a = (await poll(state, github))[0]
    state['processed_review_ids'] = {a['id']}
    await poll(state, github)
    github.get_pr_conversation_comments.return_value = [comment(second=2)]
    assert await poll(state, github) == []
    github.get_pr_conversation_comments.return_value = [comment(body='Please fix the error', second=3)]
    b = (await poll(state, github))[0]
    assert b['id'] != a['id']
    state['processed_review_ids'].add(b['id'])
    await poll(state, github)
    github.get_pr_conversation_comments.return_value = [comment(second=4)]
    reverted = (await poll(state, github))[0]
    assert reverted['id'] not in {a['id'], b['id']}


@pytest.mark.asyncio
async def test_relay_commit_acknowledges_version_before_another_poll():
    state, github = state_for([comment()])
    first = await poll(state, github)
    state['pending_reviews'] = first
    await classify_human_feedback(state)
    assert state['pr_comment_tracking']['versions']['1']
    github.get_pr_conversation_comments.return_value = [comment(second=2)]
    assert await poll(state, github) == []
    github.get_pr_conversation_comments.return_value = [comment(body='Please fix the error', second=3)]
    changed = await poll(state, github)
    state['pending_reviews'] = changed
    await classify_human_feedback(state)
    github.get_pr_conversation_comments.return_value = [comment(second=4)]
    reverted = await poll(state, github)
    assert len(reverted) == 1 and reverted[0]['id'] != first[0]['id']


@pytest.mark.asyncio
async def test_inference_timeout_does_not_accept_request():
    state, github = state_for([comment()])
    cancelled = asyncio.Event()

    async def slow_classify(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    state['conducting_backend'] = SimpleNamespace(prompt=slow_classify)
    assert await poll(state, github, budget_seconds=0.01) == []
    assert cancelled.is_set()
    assert state['pr_comment_tracking']['versions'] == {}
    assert state['pr_comment_tracking']['updated_since'] is None


@pytest.mark.asyncio
async def test_real_monitor_classifier_dispatch_delivers_provenance():
    from coordinare.graph.nodes.dispatch_performer import dispatch_performer
    from coordinare.workspace import WorkspaceInfo

    state, _ = state_for([comment()])
    service = SimpleNamespace(check_health=AsyncMock(return_value={'status': 'accepted'}),
                              dispatch_card=AsyncMock(return_value={'status': 'accepted', 'session_id': 'job'}))
    state.update(performer_services={'implementing': service}, lifecycle_sequence=['implementing'],
                 workspace_manager=SimpleNamespace(prepare=AsyncMock(return_value=WorkspaceInfo(
                     path=None, branch='coordinare/card', repo_url='https://github.com/acme/repo.git', github_token='test',
                 ))))
    await monitor_pr(state)
    assert state['phase'] == 'relay_feedback'
    await classify_human_feedback(state)
    await dispatch_performer(state)
    service.dispatch_card.assert_awaited_once()
    delivered = service.dispatch_card.call_args.args[0]['relay_feedback'][0]
    assert delivered['source'] == 'pr_comment' and delivered['comment_id'] == '1'
    assert delivered['author_login'] == 'alice' and delivered['comment_url'] == comment()['html_url']
    assert state['phase'] == 'monitoring_performer'


@pytest.mark.asyncio
async def test_new_pr_scope_resets_tracking_only_after_successful_fetch():
    state, github = state_for([comment(body='Thanks')])
    await poll(state, github)
    before = copy.deepcopy(state['pr_comment_tracking'])
    github.get_pr_conversation_comments.side_effect = RuntimeError('offline')
    assert await poll_pr_conversation_feedback(state, github, pr_node_id='PR22', pr_number=22) == []
    assert state['pr_comment_tracking'] == before
    github.get_pr_conversation_comments.side_effect = None
    github.get_pr_conversation_comments.return_value = [comment()]
    assert len(await poll_pr_conversation_feedback(state, github, pr_node_id='PR22', pr_number=22)) == 1
    assert state['pr_comment_tracking']['pr_node_id'] == 'PR22'


@pytest.mark.asyncio
async def test_requests_survive_review_author_supersession_and_defer_approval():
    state, github = state_for([comment(1), comment(2, body='Please fix the error', second=2)])
    github.get_pr_reviews.return_value = [
        {'id': 'old', 'author_login': 'alice', 'state': 'CHANGES_REQUESTED', 'submitted_at': '2026-10-08T09:00:00Z'},
        {'id': 'approval', 'author_login': 'alice', 'state': 'APPROVED', 'submitted_at': '2026-10-08T10:01:00Z'},
        {'id': 'review', 'author_login': 'trusted[bot]', 'state': 'COMMENTED', 'body': 'fix typo'},
    ]
    await monitor_pr(state)
    assert state['phase'] == 'relay_feedback'
    assert [r['id'] for r in state['pending_reviews']][:1] == ['review']
    assert [r['comment_id'] for r in state['pending_reviews'][1:]] == ['1', '2']
    assert not any(r['state'] == 'APPROVED' for r in state['pending_reviews'])
    await classify_human_feedback(state)
    assert len(state['relay_feedback']) == 3


@pytest.mark.asyncio
async def test_conversation_approval_alone_cannot_merge():
    state, _ = state_for([comment(body='Approved, ship it')])
    await monitor_pr(state)
    assert state['phase'] == 'monitoring_pr'


@pytest.mark.asyncio
async def test_failed_conversation_fetch_defers_submitted_approval():
    state, github = state_for([])
    github.get_pr_reviews.return_value = [{'id': 'approval', 'author_login': 'alice', 'state': 'APPROVED'}]
    github.get_pr_conversation_comments.side_effect = RuntimeError('unreadable')
    await monitor_pr(state)
    assert state['phase'] == 'monitoring_pr'
    assert not state.get('processed_review_ids')


@pytest.mark.asyncio
async def test_classification_limit_cannot_merge_past_unread_requests():
    state, github = state_for([comment(i, body='Thanks', second=i) for i in range(1, 6)] + [comment(6, second=6)])
    github.get_pr_reviews.return_value = [{'id': 'approval', 'author_login': 'alice', 'state': 'APPROVED'}]
    await monitor_pr(state)
    assert state['phase'] == 'monitoring_pr'
    await monitor_pr(state)
    assert state['phase'] == 'relay_feedback'
    assert state['pending_reviews'][0]['comment_id'] == '6'


@pytest.mark.asyncio
async def test_recovered_pr_url_supplies_pr_number_when_missing():
    state, github = state_for([comment()])
    state['current_card'].pop('pr_number')
    state['current_card']['pr_url'] = 'https://github.com/acme/repo/pull/21'
    await monitor_pr(state)
    assert state['phase'] == 'relay_feedback'
    github.get_pr_conversation_comments.assert_awaited_once_with(21, since=None)


@pytest.mark.asyncio
async def test_missing_pr_number_defers_approval_when_conversation_api_is_available():
    state, github = state_for([])
    state['current_card'].pop('pr_number')
    github.get_pr_reviews.return_value = [{'id': 'approval', 'author_login': 'alice', 'state': 'APPROVED'}]
    await monitor_pr(state)
    assert state['phase'] == 'monitoring_pr'
    github.get_pr_conversation_comments.assert_not_awaited()


@pytest.mark.asyncio
async def test_classification_limit_and_chronological_prefix():
    comments = [comment(i, second=i) for i in range(7, 0, -1)]
    state, github = state_for(comments)
    batch = await poll(state, github)
    assert [r['comment_id'] for r in batch] == ['1', '2', '3', '4', '5']
    assert state['pr_comment_tracking']['updated_since'] is None
    state['processed_review_ids'] = {r['id'] for r in batch}
    batch2 = await poll(state, github)
    assert [r['comment_id'] for r in batch2] == ['6', '7']
    assert state['pr_comment_tracking']['updated_since'] == comment(second=5)['updated_at']


@pytest.mark.asyncio
async def test_inference_and_deterministic_fallback():
    state, github = state_for([comment(body='The new path needs coverage')])
    backend = SimpleNamespace(prompt=AsyncMock(return_value={'data': {'classification': 'request'}}))
    state['conducting_backend'] = backend
    assert len(await poll(state, github)) == 1
    backend.prompt.side_effect = RuntimeError('unavailable')
    github.get_pr_conversation_comments.return_value = [comment(body='Please add coverage')]
    assert len(await poll(state, github)) == 1


@pytest.mark.asyncio
async def test_classification_budget_preserves_unhandled_suffix():
    state, github = state_for([comment(body='Thanks'), comment(2, second=2)])
    ticks = iter([0.0, 0.0, 21.0])
    assert await poll(state, github, clock=lambda: next(ticks)) == []
    assert state['pr_comment_tracking']['updated_since'] == comment()['updated_at']
    assert '2' not in state['pr_comment_tracking']['versions']


@pytest.mark.asyncio
async def test_overall_timeout_cancels_fetch_without_advancing():
    state, github = state_for([])
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_fetch(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    github.get_pr_conversation_comments.side_effect = slow_fetch
    assert await poll(state, github, budget_seconds=0.01) == []
    assert entered.is_set() and cancelled.is_set()
    assert not state.get('pr_comment_tracking')


@pytest.mark.asyncio
async def test_activity_records_source_provenance():
    from coordinare.services.activity_log import ActivityLog

    state, github = state_for([comment()])
    state['activity_log'] = ActivityLog()
    await poll(state, github)
    entry = state['activity_log'].snapshot()[0]
    assert 'alice' in entry['text'] and '#issuecomment-1' in entry['text']


@pytest.mark.asyncio
async def test_edited_request_has_distinct_activity_and_duplicate_api_rows_do_not_repeat():
    from coordinare.services.activity_log import ActivityLog

    state, github = state_for([comment(), comment()])
    state['activity_log'] = ActivityLog()
    first = await poll(state, github)
    assert len(first) == 1
    state['pending_reviews'] = first
    await classify_human_feedback(state)
    github.get_pr_conversation_comments.return_value = [comment(body='Please fix the error', second=2)]
    assert len(await poll(state, github)) == 1
    assert len(state['activity_log'].snapshot()) == 2


@pytest.mark.asyncio
async def test_acknowledgement_accepts_only_committed_id_in_mixed_batch():
    state, github = state_for([comment(), comment(2, second=2)])
    batch = await poll(state, github)
    state['processed_review_ids'] = {batch[0]['id']}
    acknowledge_pr_conversation_feedback(state, batch)
    assert set(state['pr_comment_tracking']['versions']) == {'1'}
    assert [r['comment_id'] for r in await poll(state, github)] == ['2']


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{'not': 'a list'}, [{'id': 1}],
                                    [dict(comment(), updated_at='invalid')]])
async def test_incomplete_or_malformed_response_preserves_entire_tracking(payload):
    state, github = state_for([comment(body='Thanks')])
    await poll(state, github)
    previous = copy.deepcopy(state['pr_comment_tracking'])
    github.get_pr_conversation_comments.return_value = payload
    assert await poll(state, github) == []
    assert state['pr_comment_tracking'] == previous


@pytest.mark.asyncio
async def test_api_refuses_cross_origin_pagination_without_forwarding_token():
    from coordinare.services.github import GitHubError

    github = GitHubService(org='acme', project_number=1, token='test')
    github._project_name = 'repo'
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[dict(comment(), user={'login': 'alice'})],
                              headers={'link': '<https://untrusted.example/page2>; rel="next"'})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch('coordinare.services.github.httpx.AsyncClient', return_value=client), pytest.raises(GitHubError):
        await github.get_pr_conversation_comments(21)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_dedicated_api_complete_pagination_or_failure(failure):
    github = GitHubService(org='acme', project_number=1, token='test')
    github._project_name = 'repo'
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            assert request.url.path == '/repos/acme/repo/issues/21/comments'
            assert request.url.params['since'] == '2026-10-08T10:00:00Z'
            raw = dict(comment(), user={'login': 'alice'})
            return httpx.Response(200, json=[raw], headers={'link': '<https://api.github.com/page2>; rel="next"'})
        return httpx.Response(503 if failure else 200, json=[dict(comment(2), user={'login': 'alice'})])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch('coordinare.services.github.httpx.AsyncClient', return_value=client):
        if failure:
            with pytest.raises(httpx.HTTPStatusError):
                await github.get_pr_conversation_comments(21, since='2026-10-08T10:00:00Z')
        else:
            result = await github.get_pr_conversation_comments(21, since='2026-10-08T10:00:00Z')
            assert [r['id'] for r in result] == [1, 2]
            assert result[0]['updated_at'] and result[0]['html_url'] and result[0]['author'] == 'alice'
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_accepted_conversation_command_commits_override_before_acknowledgement():
    from coordinare.daemon import _persist_one_session
    from coordinare.session import state_to_session
    from coordinare.state_store import PersistedSession

    state, github = state_for([comment(body="Please fix this\n/coordinare restart-from implementing"), comment(2, second=2)])
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    batch = await poll(state, github)
    state["pending_reviews"] = batch
    await classify_human_feedback(state)
    persisted = _persist_one_session("card", state_to_session(state))
    restored = PersistedSession.model_validate_json(persisted.model_dump_json())
    assert restored.pending_override is not None
    assert batch[0]["id"] in restored.processed_review_ids
    assert batch[1]["id"] not in restored.processed_review_ids
    assert set(restored.pr_comment_tracking["versions"]) == {"1"}
    assert [review["comment_id"] for review in await poll(state, github)] == ["2"]
