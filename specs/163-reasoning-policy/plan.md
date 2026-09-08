# Implementation Plan: Reasoning Policy and Truncation Classification

Branch: `163-reasoning-policy`. Closes #244. Both classification and policy ship together.

## Design and analysis

Speckit analysis covered the specification, research, data model, tasks and constitution before implementation. All 17 functional requirements have task coverage. It found three inconsistencies: unconditional shim routing contradicted absent-policy request identity; the data model retained a stale blocked label; and no measurable overhead budget existed. These are resolved by conditional proxy activation, correcting the label, and SC-007. No unresolved requirement decisions remain.

No new dependencies or persisted state. Existing Python, Pydantic, httpx and aiohttp components provide all required behavior.

## US1: classification and behavior

`classify_assessor_failure` accepts structured `finish_reason`; `length` and `max_tokens` take precedence over malformed/empty prose. When structured metadata is absent, existing prose matching remains, with truncation taking precedence. A normal structured finish suppresses stale prose truncation markers.

`monitor_performer` classifies every role and sends truncation to the existing output-token-limit block. It does not repeat an unchanged request. The assessor-only parse retry condition is explicit, so ordinary malformed/empty responses do not gain retries on other roles. `handle_system_error` also blocks persisted truncation records before retrying, while its assessor-empty-body ENV_BLOCKED condition remains scoped to assessing.

## US2: policy transport and requests

`ModelEndpoint.reasoning_policy` accepts only optional `disable_thinking`. Native endpoints reject this self-hosted extension. Dispatch includes the policy only when set; each orchestration upstream carries its own policy. A single-model opt-in gets a single-strategy proxy block. Absent policy preserves the prior no-proxy/route selection and request behavior.

The existing canonical `DualModelProxy` serves Anthropic Messages, Chat Completions and Responses. It is the policy injection path, replacing the earlier unconditional SelfHostedShim proposal: routing environment variables alone cannot establish correct Responses translation. When a policy is active, existing routing targets still supply their base URL, upstream model, bearer auth and response normalizers for each model leg. Policy does not leak from executor to planner/classifier.

Canonical parsing preserves array-form Claude system instructions and configured generation budgets. The policy-active path retains generation parameters for every leg, including a leg without its own policy. `HttpUpstream` applies `chat_template_kwargs: {enable_thinking: false}` only to the opted-in model, then normalizes its response before parsing.

Workflow model calls apply the policy directly. Implementer/env_bootstrap workflow CLI turns still use the live proxy. The adapter receives the resolved gateway base/token separately from Score so direct model calls retain gateway access after CLI environment overrides. Credentials are never serialized into Score or reports.

## US3: evidence and guard

`policy-evidence.json` records measured beneficial Qwen models, measured harmful GLM and unmeasured defaults from research R2. GLM opt-in is rejected at configuration load and again at the upstream request boundary, including when routing rewrites a model. No shipped model is opted in.

## Validation and performance

Tests cover structured/prose classification, nine lifecycle stages, persisted truncation, unchanged retry eligibility, config validation, per-model dispatch, transport-to-Score survival, workflow gateway isolation, all three real HTTP proxy front doors with streaming, planner isolation, and routing/auth preservation.

Actual-source mutations remove structured exhaustion handling, Score policy, request-body policy and the harmful-model guard. Each is detected; source bytes are restored and hashes verified. The Score mutation first revealed a missing schema assertion, which is now covered by a dedicated transport-to-Score test.

10,000 actual classifier + policy request/render + mocked HTTP/parse operations took 1.060 s (0.1060 ms/operation), below SC-007's 1 ms budget. No real network was used for this overhead measurement. Existing CI performance benchmarks guard broader runtime behavior.

Run `make lint`, `make test-all`, full performer tests and CI before merge. Record final counts in the PR. Independent review substitutes for unavailable `/review`, followed by a received Copilot review; this is recorded explicitly rather than claiming a human approval.

Local validation completed: make lint passed; make test-all passed 8,102 tests (13 skipped, 99 deselected); full performer suite passed 1,529 tests. Independent final review found no remaining issues after correcting workflow gateway isolation, array system content, and per-leg routing/generation handling. Copilot review was received and its null-reason finding fixed; hosted CI on the final integrated head remains a merge prerequisite.

Full local CI validation (2026-09-08): `make ci` ran every stage. It caught internal model prefixes in three new test fixtures; portable names now preserve namespaced and mixed-case coverage, and the evidence assertion selects the recorded model by basename without changing its data. All 42 affected policy/onboarding checks and lint pass. The full Coordinare suite plus coverage was rerun after the correction: 8,103 passed, 13 skipped, 99 deselected, 91.15% coverage (517.20s). The other full-build stages passed: 1,529 performer tests (370.16s), 196 Chromium/Firefox tests (149.42s), base/full/extra image builds, image-size verification, RTK execution/compression/hook checks, and routing-schema checks in both full and extra images. The 90% coverage floor and all timing/assertion gates are unchanged.

Final integration onto main 70e0333c1059e446da7ec82c35d24aa4e2e28c71 preserves scanner/repair/scope fields and workflow scope insertion. Both conflict resolutions retained every contract and combined the scope, policy and explicit-gateway test dimensions. Post-rebase checks passed: 356 coordinare/contract/onboarding tests, 299 proxy/backend tests, and lint.
