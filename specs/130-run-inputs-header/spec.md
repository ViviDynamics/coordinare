# Feature Specification: Run header with the inputs each run was given

**Issue**: #130 (part of epic #116, "keep and expose all model output for full transparency")
**Depends on**: #129 (run transcripts, `specs/129-run-transcripts/`)
**Blocks**: #133 (transcript viewer)

## Problem

A run transcript (spec 129) records what the model and its tools produced. It does not record what the model was given: which persona text, which card content, which feedback, which model and endpoint, and which routing decision led to the run. Persona edits on the Personas page also overwrite the text, so an older run cannot be matched to the words it actually ran with.

## Goal

1. Every run's transcript begins with one header record, written by the daemon at dispatch, before any performer record. It names the inputs of that run.
2. Every persona edit creates a retained version. A run header names the version it used, and the run keeps the text it was sent, so editing a persona never changes what an older run shows.
3. The header never fails or delays a dispatch, and it is redacted like every other record.

## User scenarios

1. **Operator asks why an agent did something.** They open the run's transcript. The first line says which persona version, which model and endpoint, which mode and routing decision, which card text and which feedback the run was given.
2. **Operator edits a persona.** The edit creates version N+1 with who changed it and when. Runs started before the edit still show version N and its text.
3. **Operator wants the change.** The history API returns the versions of a persona and a unified diff between any version and the one before it.

## Facts that bound the design

- The daemon does not render the final task prompt. Each performer backend builds it inside the performer container (`_build_task_prompt` in `agent/performer/src/performer/backends/*.py`). Most backends emit that text as the run's first `user` record. The daemon header therefore records the exact inputs it sends (persona text, card, feedback, model, routing) and states where the rendered prompt is. A header field the daemon cannot know is null with a reason, never guessed.
- The daemon header is seq 0 and is not a performer record. The performer numbers its records from 1. The writer's cursor (`_load_tail`) reads the last record's seq, so a header-only file resumes at 0 and a restarted writer still resumes after the last performer record.
- Persona text lives in `config.yaml` under `personas.<role>.instructions`. The dashboard personas router (`save_persona`, `reset_persona`) is the only in-product writer. A hand edit of `config.yaml` is also possible and is detected at the next dispatch.

## Requirements

- **FR-001** The header is the first line of the run's JSONL file, with `type: "run_header"` and `seq: 0`. It carries no performer seq and never shifts the performer's numbering.
- **FR-002** The header is written after the performer accepts the job and before any performer record can be appended. A header that cannot be written is logged (`run_transcript.header_failed`) and never fails or delays the dispatch beyond a fixed bound (`RUN_HEADER_WAIT_SECONDS`).
- **FR-003** With `transcripts.enabled: false`, no header is written and the job payload is byte-identical to the pre-130 payload.
- **FR-004** The header is redacted with the same rules as every record (job secrets by exact value, credential-named keys, embedded JSON, token shapes). No secret value appears on disk.
- **FR-005** Feedback and card text in the header are bounded. The bounds are named constants. A truncated field says so.
- **FR-006** Persona versions are append-only per role, survive a daemon restart, and are capped at `PERSONA_HISTORY_MAX_VERSIONS` (200) per role. The oldest version is pruned when the cap is exceeded.
- **FR-007** A persona version records version id, content hash (SHA-256 of the text in effect), changed_at, changed_by, source and the full text. The diff is computed on read.
- **FR-008** `changed_by` is the OIDC session identity (name, else email, else subject) when the request carries a session, and `local` otherwise.
- **FR-009** A persona change found at dispatch that did not go through the dashboard (a hand edit or a code default change) creates a version with `changed_by: "unknown"` and source `outside_dashboard`. The first version ever seen for a role has source `first_seen`.
- **FR-010** The run header carries persona name, version id, content hash, changed_by, changed_at, source, and the exact persona text sent to the performer (with its hash).
- **FR-011** Read API, auth-gated like every dashboard route:
  - `GET /api/personas/{role}/history` lists the retained versions (metadata only).
  - `GET /api/personas/{role}/history/{version}` returns one version with its text, its previous retained version id, and a unified diff against that previous version.
- **FR-012** The new routes are declared in the MCP `DEFERRED_REST_PREFIXES` list, which a test enforces.

## Out of scope

- A dashboard UI for persona history (the transcript viewer, #133, consumes it).
- Recording the rendered task prompt in the header. It is produced inside the performer. It stays the run's first `user` record (see Facts).
- Wider backend coverage of the `user` record. Backends that do not emit it keep their gap, which the existing `capability` record already declares.
- A header for the four dispatch sites outside the card pipeline (env bootstrap, wiki init, intake, documenting side). They pass no transcript target and no persona reference yet. Their runs keep the spec 129 records without a header.

## Pruned versions

A version pruned by the cap is no longer served by the history API (404). Every run header that used it still carries its full text and hash, so no run loses its prompt inputs.
