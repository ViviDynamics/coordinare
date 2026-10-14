# 133: Transcript viewer in the dashboard

**Issue**: #133 (P1, part of #116). Builds on 129 (per-run JSONL transcripts), 130 (run header and persona history), 146 (run detail at `/performers/<role>`) and 148 (activity feed rows).

## Problem

A run's transcript is on disk as JSONL, and the only way to read it is to open the file. An operator who wants to know what an agent did on a card has to find the file under the state directory, read raw JSON and guess which line belongs to which tool call.

## Goal

A run view, reachable from a card, its stage, and any activity entry, that shows the whole conversation of one run in order. Every event is reachable. Nothing is cut silently. A 2,000-event run stays responsive.

## User stories

1. **Read one run.** From a card or an activity entry, I open the run and see its header (stage, agent, model, endpoint, persona version, duration, tokens, outcome, PR), then its events in order.
2. **Scan the tool work.** Tool calls are one-line rows (`Bash · pytest -q tests/ · exit 1 · 2.3s`) that expand to the full input and output. Thinking is collapsed. Usage is shown per turn.
3. **Find things.** Search inside the run, filter by event type, jump to the next error, copy a link to one event.
4. **Keep a copy.** Download the run as JSONL (as stored) or as Markdown (generated on the server).
5. **Open a large run.** A 2,000-event run renders the visible window only, and loads tool output on demand.

## Requirements

- **FR-001** The API lists the runs for a card, under one symphony or across all symphonies, and reads one run's header, summary and records in pages.
- **FR-002** Every path segment is validated. A segment that the writer would have sanitized is refused, and nothing may resolve outside the transcript root. Symlinks and hard-linked files are refused, as the writer refuses them.
- **FR-003** The server never loads a whole transcript into memory. Files are read line by line.
- **FR-004** A line that does not parse is returned as an unreadable record with its line number. It is never dropped.
- **FR-005** List pages return a bounded preview of long fields and a flag saying so. A separate endpoint returns the full value of one field for one line.
- **FR-006** JSONL download streams the stored file unchanged. Markdown download is generated server side, streaming, with code fences that cannot be broken by the content.
- **FR-007** Every API route sits under `/api/`, so the dashboard authentication applies. The routes are declared in the MCP deferred lists.
- **FR-008** The client escapes every interpolated value. Transcript text is shown as plain text. No transcript value becomes markup, a script, a style, or a URL with a scheme other than http(s).
- **FR-009** Activity entries link into the run. The link lands on the first event at or after the entry's time (see plan, D5).
- **FR-010** Superseded by #211. The run header shows the run's tokens and cost from the #134 token ledger, with the breakdown and pricing date on hover. A model with no price shows "unknown" with the reason, and a run with no usage records shows "not tracked" with the reason.

## Out of scope

- The #147 timeline and the #150 History page are not on main, so they gain no link here. The card detail gains the link instead.
- Rendered markdown in assistant text. Text is plain, with line breaks kept.
- A persisted index of runs. Listing reads the layout and each header line.
- Exact per-event links from activity entries. Entries carry no transcript seq (see plan, D5).
