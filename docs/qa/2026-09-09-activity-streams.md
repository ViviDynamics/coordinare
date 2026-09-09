# Activity stream presentation (#315, #316)

Consecutive progress, thinking, and tool-use events with matching card, stage,
performer, and session identities share one expandable activity row. The row
shows the latest event kind, a concise English status, and the count of retained
updates. The group header owns the timestamp; raw fragments do not repeat it.
Changing any identity or encountering a lifecycle/error event starts a
new row. Older SSE payloads without session attribution remain individual rows.

Summaries use a fixed English status vocabulary, rather than relaying or claiming
to translate model prose. Expand a row to read its original diagnostic text,
which may contain other languages. Text remains limited to 200 characters per
event (truncation is labelled), with at most 2,000 retained events. Recognised
credentials are redacted before retention and transport. Performer persona
instructions additionally request English for human-facing output, while
preserving code, paths, quotations, and protocol fields.

Live updates preserve the details/summary elements so expansion and keyboard focus
remain intact. Filtering is applied after grouping; replayed sequence IDs do not
inflate the count. Retention also runs when every incoming event is filtered out,
and the selected filter remains available after its retained entries expire.

Validation includes actual Chromium and Firefox tests for a 100-event stream,
replay, identity boundaries, terminal errors, safe text escaping, expansion/focus,
and a filtered 2,000-event retention boundary.

Stream rendering helpers are served as a separate JavaScript resource so the
shared dashboard HTML remains within its existing 160 KB budget.

Raw output is ordered oldest-first within each group. Explicit text deltas from
Codex and Pi are concatenated without inserting or removing whitespace. Stream
identities preserve message boundaries, and tool activity uses separate code
blocks. Source-event timestamps distinguish repeated delta text while still
suppressing replay; whole-message events retain content-based deduplication.
Older events without delta metadata remain separate chronological paragraphs.
Already-discarded or truncated fragments cannot be reconstructed retroactively.
