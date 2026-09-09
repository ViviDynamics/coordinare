from __future__ import annotations

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
    page.evaluate('entries => afAppend(entries)', [_event(i) for i in range(100)])
    rows = page.locator('#activity-feed > .af-row')
    expect(rows).to_have_count(1)
    summary = rows.locator('summary')
    # The expandable entry spans the feed, including the old timestamp column.
    assert summary.bounding_box()['width'] > rows.bounding_box()['width'] * 0.9
    expect(summary).to_contain_text('100 updates')
    expect(summary).to_contain_text('implementing Performer')
    assert '正在' not in summary.inner_text()
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
    # Failures interrupt grouping and stay visible in English.
    failure = _event(101)
    failure.update(activity_type='error', text='失败 <script>alert(1)</script>')
    page.evaluate('entries => afAppend(entries)', [failure, _event(102)])
    expect(rows).to_have_count(3)
    expect(rows.nth(1).locator('summary')).to_contain_text('An error was reported.')
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
    row = page.locator('#activity-feed > .af-row')
    expect(row).to_have_count(1)
    row.locator('summary').click()
    paragraphs = row.locator('.af-raw p')
    expect(paragraphs).to_have_count(2)
    assert paragraphs.nth(0).text_content() == "We can inspect the plan.\nThen test."
    expect(row.locator('.af-tool code')).to_have_text("cat plan.md && echo '<script>'")
    assert paragraphs.nth(1).text_content() == "Done."
    assert row.locator('script').count() == 0
    # A live fragment extends the last message without rebuilding earlier paragraphs as rows.
    update = _event(10)
    update.update(text=" Ready for review.", is_delta=True, stream_id="m2")
    page.evaluate('entries => afAppend(entries)', [update])
    assert paragraphs.nth(1).text_content() == "Done. Ready for review."
    expect(row.locator('details')).to_have_attribute('open', '')

    separate = _event(11)
    separate.update(text="Another message.", is_delta=True, stream_id="m3")
    page.evaluate('entries => afAppend(entries)', [separate])
    expect(paragraphs).to_have_count(3)
    assert paragraphs.nth(2).text_content() == "Another message."
