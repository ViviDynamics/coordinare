# Phase 0 Research — Spec 151 (Real Performers)

All decisions are code-grounded against the current tree. The one PM-level fidelity
question 134 deferred is already resolved in the spec (fully-faked GitHub); what
remains below are implementation decisions. **Zero `NEEDS CLARIFICATION`.**

---

## D1 — How does the performer clone/push a harness-local git remote?

**Decision**: Serve the bare repo with **`git daemon`** (git's own anonymous
smart-transport server) over the `git://` protocol, and **flag-gate a scheme
relaxation** on the performer's `repo_url` validator (`ALLOW_INSECURE_REPO_URL=1`,
set only by the bench) so `git://host:9418/bench-org/bench-repo` is accepted.

```
git daemon --reuseaddr --listen=0.0.0.0 --port=9418 \
  --base-path=<scratch/repos> --export-all \
  --enable=upload-pack --enable=receive-pack <scratch/repos>
```

**Rationale**: `git daemon` ships with git — zero server code, one subprocess. It
supports both clone (`upload-pack`) and push (`receive-pack`), needs no TLS, no cert
distribution, and no CGI. A local, anonymous git server is exactly right for a
throwaway bench. The performer's token / `http.extraHeader` is simply unused on the
`git://` transport (harmless).

**Alternatives rejected**:
- *Smart-HTTP via `git http-backend` behind aiohttp* — keeps `https`/`http` and could
  reuse the REST port, but requires wiring git's CGI (env vars, chunked
  request/response streaming for `receive-pack`) into an async handler. Materially more
  code for no bench benefit.
- *HTTPS git server + self-signed cert + `GIT_SSL_NO_VERIFY=true`* — avoids the regex
  change (the existing regex already allows `https://host:port/...`) but still needs the
  smart-HTTP/CGI server above **plus** cert generation and container trust. Strictly
  more work than D1; only advantage is leaving `models.py` untouched, which the flag
  gate already makes safe.
- *`file://` bind-mount of the bare repo into the container* — laziest of all, but the
  performer's `repo_url` regex rejects `file://` and it couples the run to Docker
  volume-mount plumbing; the spec frames a network "remote" (FR-003). Rejected.

**Safety note**: the scheme relaxation is a production trust-boundary surface. It is
**off by default** — `models.py` keeps `https`-only unless the performer process has
`ALLOW_INSECURE_REPO_URL` set, which only the bench harness does. No production path
changes. (See D5.)

## D2 — How does the performer's PR-create / default-branch / check-runs reach the fake?

**Decision**: Run a small **aiohttp** REST server in the harness that implements the
three endpoints the performer's `github.py` calls, and point the performer at it by
setting `config.github_api_url` to that server's address. The coordinare flows this into
the dispatch, but **not via a container env var or a typed payload field** — the actual
path is: `dispatch_performer.py:1394-1395` writes `card_context["github_api_url"]` →
`http_performer_service._build_job_payload:860` stashes **all** of `card_context` into
`JobInitPayload.metadata` (the payload is `extra="ignore"`; there is no typed field) →
the performer reads `Score.github_api_url` and applies it at runtime in
`main.py:1470-1494`, which then feeds `get_settings().GITHUB_API_URL`
(`agent/performer/src/performer/github.py:15-17`).

**Hard host constraint (main.py:1485-1492)**: that override accepts `https://<any>` or
`http://{localhost,127.0.0.1,::1}` and **rejects `http://<non-loopback>`**, silently
falling back to `https://api.github.com`. Since the fake serves plain http (no TLS), the
performer must see it as `http://127.0.0.1:<port>` — see D6 (`--network host`).

Endpoints (verified against the performer's parse sites):
- `POST /repos/{owner}/{repo}/pulls` → `{html_url, node_id}` (create_pull_request,
  `github.py:229-231`) — delegates to `FakeGitHubService.open_pr(...)` (D3).
- `GET  /repos/{owner}/{repo}` → `{default_branch}` (get_default_branch, `github.py:52`).
- `GET  /repos/{owner}/{repo}/commits/{ref}/check-runs` → `{check_runs:[...]}`
  (get_check_runs, `github.py:115`) — each run `{status, conclusion, ...}` derived from
  the fake's real pytest result (D4). Also `GET .../pulls` (existing-PR lookup) and
  `GET .../pulls/{n}` (head sha) for the 422/idempotent paths in `create_pull_request`.

**Rationale**: aiohttp is already a project dependency (the proxy shims use it); the
REST base is already an injected config value — so this is "stand up handlers + set one
config field," not a performer rewrite (FR-004). Serving the *exact* JSON shapes is
pinned by a contract test (Principle II).

**Alternatives rejected**: intercepting httpx at the transport layer (monkeypatch) —
brittle, doesn't run in the container, and the performer is a separate process. A real
network endpoint is the only thing the container can actually reach.

## D3 — One PR identity shared between the performer-facing fake and `FakeGitHubService`

**Decision**: The REST `POST .../pulls` handler calls the **same**
`FakeGitHubService.open_pr(issue_item_id, head_ref)` the stub path uses, returning that
record's `url` as `html_url` and its `pr_id` (`PR_N`) as `node_id`. The coordinare later
reads that identity back verbatim (`monitor_performer` stores the performer-reported
`pr_url`/`pr_node_id`), so `check_mergeability`/`get_pr_reviews`/`squash_merge` operate
on the same `PR_N` (FR-005, US3, SC-004).

**Open detail resolved**: the REST handler must map the incoming `head` branch (+ the
`Closes #N` body / issue linkage) back to the seeded card's `issue_item_id`. Mapping is
by **head branch → card**: the branch name is derived from the card
(`make_branch_name(card_id, title)`), and `seed_board` knows the card↔branch relation.
The handler resolves `head_ref → card_id` via a branch index the runner passes to the
server (the same `fixtures_by_card` mapping already in `runner.py:152`).

**Rationale**: reuses the existing, tested `open_pr` state model (`fake_github.py:189`)
— no second PR store, no divergence. The stub path and real path mint identical records.

## D4 — CI reported to the performer is the same real pytest as `FakeGitHubService`

**Decision**: The `check-runs` handler returns a verdict derived from
`FakeGitHubService._ci_rollup(pr)` (`fake_github.py:474`), which runs real pytest
against a clean checkout of the PR head and caches by head sha
(`fake_github.py:493-525`). One source of truth: the same rollup governs the
coordinare's CI gate and the approver's `ci_green` (`_maybe_approve`,
`fake_github.py:593`). Maps `conclusion=success/failure` → check-run
`{status:"completed", conclusion:"success"|"failure"}` (FR-006, US2 scenario 3, US4).

**Rationale**: the fake already owns the authoritative CI result; the REST surface just
projects it into the check-runs shape the performer's `summarise_check_runs`
(`github.py:159`) classifies. No second CI runner.

## D5 — Close the two real-`api.github.com` leaks (FR-010 / SC-003)

**Decision**: Make both performer leaks config-driven, default unchanged:
1. **graphql URL** — `resolve_pr_review_threads` hardcodes
   `https://api.github.com/graphql` (`github.py:399,422,465`). This must ride the **same
   path** `github_api_url` uses (D2 — `card_context` → `metadata` → `Score`), not an env
   var: add `GITHUB_GRAPHQL_URL` to the performer settings (`config.py`, default
   `https://api.github.com/graphql`), add a `Score.github_graphql_url` field, and add a
   parallel runtime override in `main.py` (mirroring the `github_api_url` block at
   `:1470-1494`, same https-any / http-loopback validation) that sets
   `settings.GITHUB_GRAPHQL_URL`; `resolve_pr_review_threads` then reads it. The coordinare
   sets `card_context["github_graphql_url"] = config.github_graphql_url` in
   `dispatch_performer` (which is stashed into `metadata` automatically). The bench points
   it at the loopback fake host (a 404 there is fine — the function is best-effort and
   returns 0 on failure, so the run is unaffected, and crucially **no packet reaches real
   GitHub**).
2. **repo_url scheme** — the `https`-only regex (`models.py:68`) is relaxed **only**
   when `ALLOW_INSECURE_REPO_URL` is set (D1), so a real run with outbound GitHub
   blocked still completes and targets no real host.

**Rationale**: SC-003 verifies "zero requests reach a real GitHub host" with outbound
blocked — a *failed connection attempt* to `api.github.com` still violates it. Both
leaks must be routed to the fake host. Each change defaults to today's behavior;
production is untouched.

**Verification hook**: SC-003's test runs the fake with outbound GitHub blocked (e.g.
container `--dns`/egress denylist or a null-route) and asserts the run still reaches a
terminal state and emits an artifact — proving nothing depends on real GitHub.

**Bench-only confirmation (FR-013, T030)**: every relaxation defaults to production
behavior and takes effect only when the bench opts in:
- `git_base_url` (coordinare config) defaults `https://github.com`; only the runner's
  real path overrides it to the loopback `git://` daemon.
- `ALLOW_INSECURE_REPO_URL` gates the performer's `git://` `repo_url` acceptance
  (`models.py` `_validate_repo_url`); unset ⇒ `https`-only. The bench sets it via
  `config.env`.
- `network_mode` (new `PerformerEndpointConfig` field) defaults `None` (bridge); the
  bench config sets `host`.
- The http-loopback GitHub-URL rule (`apply_github_url_override`) only accepts an
  `http` host on `127.0.0.1`/`localhost`/`::1`; a non-loopback http host is ignored
  (no real-GitHub leak), and `https` is always accepted (production default).
- **Note (beyond the plan):** `JobInitPayload.repo_url` was `HttpUrl` (rejects
  `git://`), so it was relaxed to `str` with an http/https/git **scheme** validator
  on BOTH the coordinare and performer copies. This is transport-level only — the
  enforced trust boundary stays the performer's `Score.repo_url` validator (gated by
  `ALLOW_INSECURE_REPO_URL`). Production still emits `https` URLs, so its behavior is
  unchanged; the relaxation only lets the bench's `git://` remote transport.

## D6 — Docker networking: the container must reach the host's fake services

> **SUPERSEDED (post-first-light, portability):** `--network host` only shares the host
> loopback on **native-Linux Docker**; every Docker Desktop user (macOS/Windows/WSL2) sees
> `--network host` share the *VM's* netns, not the host's, so the coordinare's readiness
> probe to `127.0.0.1:<port>` never reaches the container. To make the bench run on *any*
> Docker, the performer now runs on the **default bridge** and reaches the host's fakes via
> **`host.docker.internal`** (`--add-host=host.docker.internal:host-gateway`, portable on
> Docker 20.10+). The fakes bind `0.0.0.0` and advertise **two** views: host-facing
> `127.0.0.1` (coordinare clone + rebase + probe) in `git_base_url`, and container-facing
> `host.docker.internal` in `performer_git_base_url` / `github_api_url` /
> `github_graphql_url`. The loopback-only http-URL rule (D2) is relaxed to also accept
> `host.docker.internal` **only** under the new `ALLOW_HOST_GATEWAY_GITHUB` bench opt-in
> (prod default unchanged, FR-013). `network_mode: host` remains a supported config knob but
> is no longer what the bench uses. The original loopback decision below is kept for history.

**Decision**: Launch the bench performer container with **`--network host`** (Linux) and
address every fake service over **loopback** — `http://127.0.0.1:<port>` (REST/graphql)
and `git://127.0.0.1:9418` (git daemon). This is forced by D2's host constraint: the
performer accepts an `http` GitHub URL **only** for `{localhost,127.0.0.1,::1}`
(`main.py:1489`), so a `host.docker.internal`/`--add-host` address (non-loopback) would be
**rejected and silently fall back to real GitHub**. With `--network host` the container
shares the host's network namespace, so `127.0.0.1` reaches the host's servers *and*
passes the loopback validation. The runner threads a single loopback `bench_host` into
`git_base_url` / `github_api_url` / `github_graphql_url`, and fails fast via a
pre-dispatch reachability probe if the services aren't up (spec Edge Cases — no hang).

**Rationale**: loopback is the only http host the performer will accept without TLS, and
`--network host` is the simplest way to make loopback reach the host on Linux (the plan's
target platform). It also avoids `--add-host` and cert distribution entirely.

**Confirmed launch-path gap (was the tracked risk)**: `performer_lifecycle.start_ephemeral`
(`:131`) builds the `docker run` args and supports `-e`/`-v`/`--label` but has **no**
network-mode option. The one real addition is a bench-scoped `network_mode` parameter that
emits `--network <mode>`; `ALLOW_INSECURE_REPO_URL=1` rides the existing `config.env`
`-e` path. Still a config/injection change, not a performer rewrite (FR-004).

**Note on Docker Desktop (macOS/Windows)**: `--network host` does not share loopback there;
the bench targets Linux. A Desktop path would need an https fake + cert trust — out of
scope.

## D7 — Git host injection point (FR-004)

**Decision**: Add `git_base_url` to the coordinare config (default
`https://github.com`) and have `WorkspaceManager.prepare()` build `repo_url`/`clone_url`
from it instead of the hardcoded `https://github.com/{org}/{project}.git`
(`workspace.py:263,284`). The bench sets `git_base_url=git://<bench_host>:9418`. The
`WorkspaceInfo.repo_url` flows to the performer via
`http_performer_service` (`:762-765`) unchanged.

**Rationale**: single, obvious injection point; default preserves production. The
performer already consumes `workspace_info.repo_url` verbatim — no performer change on
the git-URL path beyond the D5 scheme flag.

## D8 — Recording real per-persona dispatches + token usage (FR-008 / SC-002)

**Decision**: In real mode the runner does **not** override the model nodes; it records
one `PersonaDispatch` per real dispatch (stage, role, model/backend, status,
started/finished, best-effort token usage) via the existing
`bench/recording_performer.py` wrapper, populating 134's `dispatch_log`/artifact. Token
usage is read best-effort from the performer response's cost/usage events
(`BackendEventType.cost`); absent → recorded null (FR-008 "best-effort").

**Rationale**: reuses 134's artifact + recording seam; the only new content is that the
log now has multiple real entries (distinguishable from the stub's single synthetic
one, SC-002). Cost stays a token×rate estimate (134 FR-012; authoritative USD deferred).

**Known limitation — the `opencode` backend never reports tokens.** `tokens_processed`
is only populated by backends that surface usage on their terminal status (`junie`,
`codex`, `hermes`, `claude_code`). The `opencode` adapter returns
`BackendStatus(state="done", output=…)` with no `tokens_processed`
(`agent/performer/src/performer/backends/opencode.py`), so every opencode dispatch
records `tokens_processed: null` → `cost`/`totals` null. This is a backend-capability
gap, not a run error: an opencode real run terminates and emits a valid artifact, it
just carries no token/cost numbers. To exercise the cost path, run the bench on a
token-reporting backend (e.g. the `claude_code` variant of the litellm example config).
Making opencode emit tokens would require parsing usage from its SDK message events and
a performer image rebuild (out of scope here).

**Verified on the `claude_code` + LiteLLM variant (green run 20260821-051513, 1/1 merged,
8/8 personas, 1,006,996 tokens).** Three things were required to get real numbers:

1. **`MAX_THINKING_TOKENS: "0"` in the endpoint env.** The claude CLI (2.1.238) sends a
   `thinking` block by default and LiteLLM rejects it for a self-hosted non-reasoning
   model (`400 … "qwen3-coder:30b" does not support thinking`) — on *every* request, so
   stages terminate `blocked` with `pytest_exit: 2` and `tokens_processed: 0`. Bench-only:
   a real Claude endpoint should keep extended thinking.
2. **The terminal response must carry metrics.** `claude_code` learns its token count at
   the CLI `result` event, i.e. on the terminal `BackendStatus` — no `working` response
   ever carries it, and `working` was the only path that attached `metrics`
   (`main.py:3419`). The in-job loop now stamps `collect_metrics(...)` on the terminal
   response before it is serialised into `JobResult.summary`, which is exactly what
   coordinare's `check_status` returns. Performer change → image rebuild.
3. **The recorder joins by session, not by run-end.** `check_status`'s terminal return is
   the parsed `PerformerResponse`, so its `status` is the *marker*
   (`pr_opened` / `changes_requested` / `qa_failed` / …) and tokens live at
   `result["metrics"]["tokens_processed"]` — there is no `summary` key on that return, and
   the old job-state filter (`succeeded`/`failed`/…) never matched a marker at all.
   `_records_to_dispatch_log` now joins each dispatch to its terminal record by
   `session_id`, giving real `finished_at`/`seconds`, the job/session/container ids, and an
   honest status (a `changes_requested` closer reports `failed` + `terminal_marker`, not
   `succeeded`; a dispatch the budget cut off reports `cancelled`).

## D9 — Teardown (FR-009)

**Decision**: The runner owns a context that, on exit (success or exception), stops the
`git daemon` subprocess, closes the aiohttp server, removes any ephemeral performer
containers it launched, and `rmtree`s the scratch/repos/ci dirs — mirroring 134's
`finally`/`aclose` discipline (`runner.py:195-196`). Teardown never raises.

**Rationale**: the fake now owns OS resources (a listening socket, a subprocess), so
cleanup is mandatory and must be exception-safe so a mid-run failure still tears down
and still emits the artifact (FR-008).
</content>
