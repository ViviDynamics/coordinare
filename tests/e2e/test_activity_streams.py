from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

# Include cold browser setup in the budget without racing Playwright's own
# 30-second operation deadline with the unit suite's 30-second interrupt.
pytestmark = [pytest.mark.e2e, pytest.mark.timeout(90)]


def _event(seq, **extra):
    return dict(seq=seq, timestamp='2026-09-09T18:00:00Z', card_id='card',
                card_number=315, stage='implementing', session_id='session',
                performer_id='codex', activity_type='progress', text=f'正在处理 {seq}',
                truncated=False, **extra)


def test_stream_groups_english_summary_and_live_expansion(page, live_server_url):
    page.goto(live_server_url)
    # The first state_update starts a new run (afSyncRun) and clears the feed. Wait
    # for that run id, so the clear cannot land after the rows are drawn and detach
    # the summary under the test. #phase is no signal: the server renders it.
    page.wait_for_function('() => typeof _afRunId === "string" && _afRunId.length > 0', timeout=10_000)
    page.evaluate('entries => afAppend(entries)', [_event(i) for i in range(100)])
    rows = page.locator('#activity-feed > .af-row')
    expect(rows).to_have_count(1)
    summary = rows.locator('summary')
    # The expandable entry spans the feed, including the old timestamp column.
    assert summary.bounding_box()['width'] > rows.bounding_box()['width'] * 0.9
    expect(summary).to_contain_text('100 updates')
    # 353: the collapsed line leads with the newest entry's own text instead of
    # the generic English sentence, so the operator can tell WHAT happened.
    expect(summary).to_contain_text('正在处理 99 (100 updates)')
    expect(rows.locator('.af-raw')).not_to_be_visible()
    summary.click()
    summary.focus()
    expect(rows.locator('.af-raw')).to_be_visible()
    expect(rows.locator('.af-raw')).to_contain_text('正在处理 99')
    # One timestamp belongs to the group header, not to every raw fragment.
    assert rows.locator('.af-time').count() == 1
    assert rows.locator('.af-time').inner_text() not in rows.locator('.af-raw').inner_text()
    page.evaluate('entries => afAppend(entries)', [_event(100)])
    expect(rows).to_have_count(1)
    expect(summary).to_be_focused()
    expect(rows.locator('details')).to_have_attribute('open', '')
    expect(summary).to_contain_text('101 updates')
    # Replayed SSE backfill cannot inflate the count.
    page.evaluate('entries => afAppend(entries)', [_event(i) for i in range(101)])
    expect(summary).to_contain_text('101 updates')
    # Failures interrupt grouping and stay visible; the collapsed line carries
    # the escaped failure text itself (353), never a script element.
    failure = _event(101)
    failure.update(activity_type='error', text='失败 <script>alert(1)</script>')
    page.evaluate('entries => afAppend(entries)', [failure, _event(102)])
    expect(rows).to_have_count(3)
    expect(rows.nth(1).locator('summary')).to_contain_text('失败 <script>alert(1)</script>')
    assert page.locator('#activity-feed script').count() == 0


def test_stream_boundaries_filter_and_retention(page, live_server_url):
    page.goto(live_server_url)
    events = [_event(0), _event(1)]
    for seq, changes in [(2, {'session_id':'new'}), (3, {'performer_id':'claude'}),
                         (4, {'card_id':'other'}), (5, {'stage':'reviewing'}),
                         (6, {'activity_type':'completed'})]:
        event = _event(seq)
        event.update(changes)
        events.append(event)
    page.evaluate('entries => afAppend(entries)', events)
    expect(page.locator('#activity-feed > .af-row')).to_have_count(6)
    page.locator("#af-filter").select_option("other")
    expect(page.locator('#activity-feed > .af-row')).to_have_count(1)
    # Flood a filtered-out stream: raw and DOM retention must both stay bounded.
    page.evaluate('entries => afAppend(entries)', [_event(i) for i in range(7, 2020)])
    assert page.evaluate('_afEntries.length') == 2000
    expect(page.locator('#activity-feed > .af-row')).to_have_count(0)
    page.locator("#af-filter").select_option("")
    expect(page.locator('#activity-feed > .af-row')).to_have_count(1)
    expect(page.locator('#activity-feed summary')).to_contain_text('2000 updates')


def test_raw_stream_reconstructs_sentences_and_separates_messages_and_tools(page, live_server_url):
    page.goto(live_server_url)
    entries = []
    for i, chunk in enumerate(["We", " can", " inspect", " the", " plan", ".\nThen ", "test", "."]):
        event = _event(i)
        event.update(text=chunk, is_delta=True, stream_id="m1")
        entries.append(event)
    command = _event(8)
    command.update(activity_type="tool_use", text="cat plan.md && echo '<script>'")
    next_message = _event(9)
    next_message.update(text="Done.", is_delta=True, stream_id="m2")
    page.evaluate('entries => afAppend(entries)', [*entries, command, next_message])
    # 148: a tool call is its own row, and Tools starts collapsed, so show it first.
    page.locator("#activity-feed-card .af-chip[data-chip='tools']").click()
    rows = page.locator('#activity-feed > .af-row')
    expect(rows).to_have_count(3)
    # Newest first: the second message, the tool call, then the first message.
    done_row, tool_row, message_row = rows.nth(0), rows.nth(1), rows.nth(2)
    expect(tool_row).to_have_class(re.compile(r'\baf-row-tools\b'))
    expect(message_row).to_have_class(re.compile(r'\baf-row-agent\b'))
    message_row.locator('summary').click()
    expect(message_row.locator('.af-raw p')).to_have_count(1)
    assert message_row.locator('.af-raw p').nth(0).text_content() == "We can inspect the plan.\nThen test."
    tool_row.locator('summary').click()
    expect(tool_row.locator('.af-tool code')).to_have_text("cat plan.md && echo '<script>'")
    assert tool_row.locator('script').count() == 0
    done_row.locator('summary').click()
    paragraphs = done_row.locator('.af-raw p')
    assert paragraphs.nth(0).text_content() == "Done."
    # A live fragment extends the last message without rebuilding earlier paragraphs as rows.
    update = _event(10)
    update.update(text=" Ready for review.", is_delta=True, stream_id="m2")
    page.evaluate('entries => afAppend(entries)', [update])
    assert paragraphs.nth(0).text_content() == "Done. Ready for review."
    expect(done_row.locator('details')).to_have_attribute('open', '')

    separate = _event(11)
    separate.update(text="Another message.", is_delta=True, stream_id="m3")
    page.evaluate('entries => afAppend(entries)', [separate])
    expect(paragraphs).to_have_count(2)
    assert paragraphs.nth(1).text_content() == "Another message."




def _usage_event(seq, **changes):
    result = _event(seq)
    result.update(changes)
    return result


def test_usage_reports_stay_with_their_stream(page, live_server_url):
    page.goto(live_server_url)
    events = [
        _usage_event(0, text='We', is_delta=True, stream_id='message'),
        _usage_event(1, activity_type='cost', text='100 tokens, $0.01'),
        _usage_event(2, text=' can proceed.', is_delta=True, stream_id='message'),
        _usage_event(3, activity_type='tool_use', text='pytest tests/'),
        _usage_event(4, activity_type='cost', text='250 tokens, $0.03'),
    ]
    page.evaluate('entries => afAppend(entries)', events)
    # 148: the tool call is its own row, hidden until Tools is on.
    page.locator("#activity-feed-card .af-chip[data-chip='tools']").click()
    rows = page.locator('#activity-feed > .af-row')
    expect(rows).to_have_count(3)
    # Newest first: the usage report after the tool, the tool, then the message.
    message_row = rows.nth(2)
    summary = message_row.locator('details > summary').first
    # 353: the collapsed line carries the newest non-cost entry's own text.
    expect(summary).to_contain_text('can proceed. (3 updates)')
    summary.click()
    summary.focus()
    assert message_row.locator('.af-raw > p').text_content() == 'We can proceed.'
    expect(rows.nth(1).locator('.af-tool code')).to_have_text('pytest tests/')
    usage = message_row.locator('.af-usage')
    expect(usage.locator('summary')).to_have_text('Usage reports (1)')
    usage.locator('summary').click()
    expect(usage.locator('p')).to_have_text(['100 tokens, $0.01'])
    # A new usage report joins the usage row above it and leaves the focused message alone.
    summary.focus()
    update = _usage_event(5, activity_type='cost', text='300 tokens, <script>$0.04</script>')
    page.evaluate('entries => afAppend(entries)', [update])
    expect(summary).to_be_focused()
    expect(rows.nth(0).locator('.af-usage summary')).to_have_text('Usage reports (2)')
    expect(rows.nth(0).locator('.af-usage p').last).to_have_text('300 tokens, <script>$0.04</script>')
    assert page.locator('#activity-feed script').count() == 0
    # A replayed SSE backfill of the whole stream, the update included, cannot inflate the rows.
    page.evaluate('entries => afAppend(entries)', [*events, update])
    expect(rows).to_have_count(3)
    expect(rows.nth(0).locator('.af-usage summary')).to_have_text('Usage reports (2)')
    expect(rows.nth(0).locator('.af-usage p')).to_have_count(2)
    # Focus on the usage summary survives a rebuild of its raw text by a new report.
    # The usage summary sits inside the group, so the group must be open to take focus.
    rows.nth(0).locator('details > summary').first.click()
    rows.nth(0).locator('.af-usage summary').focus()
    page.evaluate('entries => afAppend(entries)', [_usage_event(6, activity_type='cost', text='400 tokens')])
    expect(rows.nth(0).locator('.af-usage summary')).to_be_focused()
    expect(rows.nth(0).locator('.af-usage summary')).to_have_text('Usage reports (3)')


def test_usage_events_never_bridge_failures_lifecycle_or_sessions(page, live_server_url):
    page.goto(live_server_url)
    boundaries = [
        # A message, then its usage report: the report stays with the message it followed.
        _usage_event(0, text='Working on it.', is_delta=True, stream_id='message'),
        _usage_event(1, activity_type='cost', text='10 tokens'),
        _usage_event(7, activity_type='error'), _usage_event(8, activity_type='cost'),
        _usage_event(9, activity_type='blocked'), _usage_event(10, activity_type='cost'),
        _usage_event(11, activity_type='completed'), _usage_event(12, activity_type='cost'),
        _usage_event(13, activity_type='cost', session_id='next'),
        _usage_event(14, activity_type='cost', card_id='other'),
        _usage_event(15, activity_type='cost', performer_id='other'),
        _usage_event(16, activity_type='cost', stage='reviewing'),
    ]
    page.evaluate('entries => afAppend(entries)', boundaries)
    rows = page.locator('#activity-feed > .af-row')
    # Each boundary starts a new row, so no usage report joins across one.
    # The message and its usage report share the oldest row; the other ten are boundaries.
    expect(rows).to_have_count(11)
    expect(rows.nth(10).locator('.af-usage summary')).to_have_text('Usage reports (1)')
    for label in ['ERROR', 'BLOCKED', 'COMPLETED']:
        expect(page.locator('#activity-feed .af-kind').filter(has_text=label)).to_have_count(1)
    page.locator('#af-filter').select_option('other')
    expect(rows).to_have_count(1)


def test_usage_only_traffic_is_coalesced_and_still_respects_the_retention_cap(page, live_server_url):
    page.goto(live_server_url)
    page.evaluate('entries => afAppend(entries)', [
        _usage_event(i, activity_type='cost') for i in range(17, 2021)
    ])
    assert page.evaluate('_afEntries.length') == 2000
    rows = page.locator('#activity-feed > .af-row')
    expect(rows).to_have_count(1)
    expect(rows.locator('.af-usage summary')).to_have_text('Usage reports (2000)')
