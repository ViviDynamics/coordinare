# Research: QA Visual Testing and Comment Orchestration

**Branch**: `055-qa-visual-testing-comment-orchestration` | **Date**: 2026-04-26

## Design Decisions

### DR-001: Docker image for browser automation

**Question**: Which Docker image should QA use for Playwright-based screenshot capture?

**Options considered**:
1. `mcr.microsoft.com/playwright:v1.44.0-jammy` — Microsoft's official Playwright image; ships Chromium, Firefox, WebKit + all system deps pre-installed. Used by GitHub Actions `playwright/test`. No extra apt installs needed.
2. `browserless/chrome` — Chrome-only, API-over-HTTP (not Playwright). Requires a different client library.
3. Custom image built from `node:20-slim` — full control, but adds build time per pull.

**Decision**: Use `mcr.microsoft.com/playwright:v1.44.0-jammy` as the default. Configurable via `qa_playwright_image` in config. Playwright gives multi-browser support, a well-maintained CLI, and is already the de facto standard for Node-based UI testing.

**Tradeoff**: Image is large (~1.8 GB). Pull only happens once; after that Docker cache serves it. First-run latency is acceptable given QA is not latency-sensitive.

---

### DR-002: How to execute QA scripts inside Docker

**Question**: Does the QA performer inject a test script into the container, or does it drive the container via Playwright's remote protocol?

**Options considered**:
1. Volume-mount a generated Playwright test file into the container; run `npx playwright test` inside.
2. Expose the app on a known port; run Playwright from outside the container (host Python via `playwright` Python library).
3. Use Playwright's remote WebSocket protocol — connect from Python to a browser running inside Docker.

**Decision**: Option 2 — run the Python `playwright` library on the host, pointed at the app exposed from the Docker container. This avoids injecting scripts into a managed image and keeps the test logic in Python (consistent with the rest of the performer stack).

**Tradeoff**: Requires `playwright` Python library on the host performer. If not installed, fall back to `docker_unavailable`. QA performer should check `import playwright` at startup and warn if absent.

---

### DR-003: GitHub CDN upload mechanism

**Question**: What endpoint uploads images to GitHub's CDN for use in markdown?

**Research**: GitHub does not document a public API for the drag-and-drop asset upload that the UI uses (`https://github.com/upload/policies/assets`). However, the endpoint is discoverable and widely used:

1. `POST https://github.com/upload/policies/assets` — initiates upload, returns a presigned S3 policy + URL.
2. `POST` to the S3 URL with the policy fields — uploads the file.
3. `POST https://github.com/upload/attachments` — marks the upload complete, returns the final CDN URL (`https://github.com/user-attachments/assets/...`).

Alternatively, for repos with releases, `POST /repos/{owner}/{repo}/releases/assets` works but requires a release tag.

**Decision**: Use the undocumented upload flow (steps 1–3). This matches what GitHub's own editor uses and produces `user-attachments` URLs that render inline everywhere. Wrap in a helper with retry logic. Document that this is an unofficial API and may change.

**Fallback**: If the upload fails after 3 retries, embed `*(screenshot unavailable)*` inline.

---

### DR-004: Issue comment polling vs webhook

**Question**: Should coordinare poll GitHub for issue comments, or use webhooks?

**Options considered**:
1. Polling via `GET /repos/{owner}/{repo}/issues/{issue_number}/comments?since={last_seen}` — fits the existing poll-cycle architecture. Simple, no infra changes.
2. GitHub webhooks (`issue_comment` event) — lower latency, but requires a publicly reachable endpoint, which the daemon may not have.

**Decision**: Polling. The coordinare daemon already uses a poll-cycle model. Adding issue comment polling alongside PR comment polling is a natural extension. Store `last_issue_comment_id` per session (analogous to existing `last_pr_comment_id` pattern).

**Tradeoff**: Up to one full poll cycle of latency (default 60s). Acceptable for comment routing.

---

### DR-005: Assessor classification taxonomy

**Question**: What comment classifications should the assessor emit?

**Current state**: The assessor (if it exists as a node) likely classifies PR review threads. Need to confirm and extend if needed.

**Proposed taxonomy**:
- `clarification` — question about requirements, scope, or behavior; performer should respond or log
- `scope_change` — request to add/modify/remove a feature; sets `requirements_changed=True`
- `blocker_update` — comment indicates a dependency was resolved or a new blocker appeared
- `approval` — LGTM / approved / looks good; no performer action needed
- `noise` — bots, emoji-only, greetings, out-of-scope chatter; silently recorded

**Decision**: Extend existing assessor logic with `source` field (`pr` | `issue`) and add `blocker_update` and `approval` to the existing taxonomy if not present.

---

### DR-006: Doc deduplication scope and trigger

**Question**: When should the dedup pass run, and how is "overlap" defined?

**Scope**: Only `*.md` files in the workspace root (not subdirs). Targets: `spec.md`, `requirements.md`, `notes.md`, `scratch.md`, `context.md`. Does not touch `.github/`, `docs/`, or other project markdown.

**Overlap definition**: Two files have an overlapping section if they share a normalized heading (lowercased, stripped of `#` and whitespace) AND the section body is ≥ 60% identical by token overlap (simple bag-of-words, not semantic).

**Canonical priority**: `spec.md` > `requirements.md` > `notes.md` > `scratch.md` > any other `.md`.

**Trigger**: After each performer write cycle that modifies more than one `.md` file.

**Decision**: Implement as a post-write hook in the performer that runs a lightweight Python dedup pass. No LLM call needed — purely structural (heading match + token overlap).

---

## Unknowns Resolved

- GitHub CDN upload: unofficial but stable API; use with retry + fallback. ✅
- Playwright Docker image: `mcr.microsoft.com/playwright:v1.44.0-jammy` confirmed as canonical. ✅
- Issue number linkage: `current_card["issue_number"]` already exists in card metadata from the board fetcher. ✅
- Assessor node: exists in `src/coordinare/graph/nodes/` (needs verification; likely `check_board.py` or a dedicated node). ✅ (to verify during Phase 0)

## Open Questions

- Does the QA performer run on the same host as Docker, or in a remote container? (Assume same host for now.)
- Is there a `last_pr_comment_id` field already on `CardSession` or `CoordinareState`? (Check during Phase 0.)
- Does the existing assessor already classify `scope_change`? (Check `graph/nodes/` during Phase 0.)
