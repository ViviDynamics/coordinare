# Let the reviewer persona run on Scrutare, opt-in

Issue #402

## Scope
In:
- A role-level stage contract (`services/stage_contract.py`): input is the repository,
  pull request and head SHA; output is the verdict, findings, the head that was reviewed
  and a reference to the record. An adapter turns any contract provider into the
  performer-service protocol the dispatch and monitor nodes already speak, so the
  existing verdict cache (125), feedback loop and system-error retry are reused.
- A Scrutare provider (`services/scrutare.py`) for the reviewing stage: submits
  `POST /api/reviews` with a request id derived from card, stage, head and attempt,
  polls `GET /api/jobs/<id>`, maps the result onto the contract.
- Config: a root `stage_providers` catalog and `performers.<role>.provider`. Token by
  environment variable name only. Pydantic validation with clear errors.
- Dispatch routing by the symphony's effective config, so a symphony opts in with
  `overrides.performers` and the default never changes.
- `monitor_pr` ignores reviews written by a configured provider's review login, so the
  stage output is never routed a second time and never counts as a human approval.
- `doctor` (and the shared startup preflight) checks the provider is reachable and the
  token is accepted.
- Docs and `config.example.yaml`.

Out:
- Progettare as architect (#403) and QAre as QA (#404). The contract and adapter are
  shaped for them, but only the reviewing stage is wired.
- Harness selection (#381). A provider-backed role ignores `backend`; validation says so.
- The dashboard note that `backend` has no effect for a provider-backed role. Left as
  remaining work on the issue.
- Changes in the Scrutare repository. The webhook/API double review gap is filed there.

## Assumptions
- Scrutare deduplicates performer API requests by request id only, and never merges an
  API job with a webhook job (docs/app.md, "Performer API jobs are never merged or
  skipped"). One review per head therefore needs the App's webhook not to review the
  same repository. Documented, and filed on Scrutare.
- `posted` and `unchanged` carry the CLI result document, with `head_sha` and `verdict`.
- `failed`, `interrupted` and `not_started` with `delivery: not_sent` are safe to retry
  with the next attempt's request id. `confirmed` or `uncertain` delivery must not be
  resubmitted (Scrutare's contract), so the card blocks with the reason.
- The reviewed head must equal the pull request's current head when the result is read.
  Otherwise the result is refused as a retryable error and the next dispatch reviews the
  current head.
- A Scrutare approval is never a human approval: monitor_pr skips the provider's login.

## Tasks
- [x] 1. Stage contract and adapter: request id derivation, verdict mapping, head refusal,
  retry and block paths (tests/unit/test_402_stage_contract.py).
- [x] 2. Scrutare provider over httpx: submit, poll, attempt probing, status mapping,
  health (tests/unit/test_402_scrutare_provider.py, fake HTTP via httpx.MockTransport).
- [x] 3. Config: catalog, role field, validation errors (tests/unit/test_402_stage_provider_config.py).
- [x] 4. Wiring: service registration and dispatch routing by effective config
  (tests/unit/test_402_stage_provider_routing.py).
- [x] 5. monitor_pr skips provider reviews (tests/unit/test_402_monitor_pr_provider_review.py).
- [x] 6. doctor reachability check (tests/unit/test_402_stage_provider_doctor.py).
- [x] 7. Docs and config example.
