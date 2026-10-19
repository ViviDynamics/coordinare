"""Stage-aware synthetic source reviewers for pre-existing constructor unit tests.

These mocks run the real scope pipeline and consume its real call/read budgets.
They never run in the repository-validation harness or establish project readiness.
Constructor/reviewer scripts retain their original messages and assertions; dedicated
scope tests use explicitly scripted scope judgments instead of this convenience mock.
"""
from __future__ import annotations

import json
from dataclasses import replace

from coordinare.services.environment_attempt import DiscoveryChatReply
from coordinare.services.environment_discovery import discover_and_review
from coordinare.services.environment_function_wire import load_unique
from coordinare.services.environment_response_schema import invoke_chat


class FixtureAssertionError(BaseException):
    """A mock assertion must remain visible through production error redaction."""


class ScopeChatFixture:
    def __init__(self, chat, path):
        self.chat = chat
        self.path = path
        self.scope_messages = []

    async def __call__(self, messages, *, response_schema):
        system = messages[0]['content']
        context = json.loads(messages[1]['content'])
        if 'Author source-defined obligations' in system:
            self.scope_messages.append(messages)
            read_paths = {entry['path'] for entry in context.get('mandatory_evidence', [])}
            for message in messages[2:]:
                if message['role'] != 'user':
                    continue
                result = json.loads(message['content']).get('tool_result', {})
                read_paths.update(item['path'] for item in result.get('files', []))
                if 'path' in result:
                    read_paths.add(result['path'])
            if self.path not in read_paths:
                return json.dumps({'tool': 'read_file', 'path': self.path})
            return json.dumps({'scope': {'criteria': [{'description': 'Synthetic source fixture obligation',
                'sources': [self.path], 'applicability': 'required', 'rationale': 'Synthetic fixture source evidence',
                'usability': 'Exercise the fixture source constraints through required consumer checks', 'runtime_needs': []}]},
                'finding_responses': [{'finding_id': record['id'], 'rationale': 'Fixture retains source obligation',
                    'sources': [self.path], 'affected_ids': []} for record in context['finding_ledger']['records']]})
        if 'Independently review source-defined scope' in system:
            self.scope_messages.append(messages)
            return json.dumps({'scope_assessment': {'verdict': 'accept', 'rationale': 'Synthetic independent scope judgment',
                'sources': [self.path], 'findings': [], 'decisions': [{'finding_id': record['id'], 'status': 'resolved',
                    'rationale': 'Independent fixture evidence covers obligation', 'sources': [self.path], 'duplicate_of': None}
                    for record in context['finding_ledger']['records']]}})
        try:
            reply = await invoke_chat(self.chat, messages, schema=response_schema)
        except AssertionError as exc:
            raise FixtureAssertionError(str(exc)) from exc
        text = reply.text if isinstance(reply, DiscoveryChatReply) else reply
        try:
            value = load_unique(text)
        except (ValueError, TypeError):
            return reply
        if isinstance(value, dict) and isinstance(value.get('manifest'), dict):
            manifest = value['manifest']
            if 'criterion_checks' not in manifest and 'scope_contract' in context:
                checks = [c['id'] for c in manifest.get('checks', [])
                          if isinstance(c, dict) and 'id' in c and c.get('required', True)]
                manifest['criterion_checks'] = {criterion['id']: checks[:1]
                    for criterion in context['scope_contract']['definition']['criteria'] if criterion['applicability'] == 'required'}
                text = json.dumps(value)
                return replace(reply, text=text) if isinstance(reply, DiscoveryChatReply) else text
        return reply


async def discover_with_fixture_scope(**kwargs):
    chat, paths = kwargs.get('chat'), kwargs.get('paths')
    if callable(chat) and paths:
        kwargs['chat'] = ScopeChatFixture(chat, paths[0])
    return await discover_and_review(**kwargs)
