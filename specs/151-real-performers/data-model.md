# Phase 1 Data Model — Spec 151 (Real Performers)

No new persisted coordinare state. This spec adds one harness component
(`FakeGitHubServer`) that projects the **existing** `FakeGitHubService` state onto the
performer's network boundary, plus three small config/flag fields. Entities below are
the ones the spec names (Key Entities) grounded in the code they touch.

---

## Performer-facing fake GitHub (`FakeGitHubServer` — NEW)

The harness-local surface a performer container talks to during a run. Two listeners,
one backing store.

| Part | Backed by | Serves |
|------|-----------|--------|
| Git remote | `FakeGitHubService._bare_repo` (real bare repo, 134's `materialize_repo`) | `git daemon` on `git://<bench_host>:9418` — clone (`upload-pack`) + push (`receive-pack`) |
| REST surface | `FakeGitHubService` (`_prs`, CI rollup) | aiohttp on `http://<bench_host>:<port>` — PR create, default branch, check-runs (see [contracts/performer-facing-rest.md](./contracts/performer-facing-rest.md)) |

**Lifecycle** (owned by the runner, teardown-safe — D9):
`start()` → bind git daemon + aiohttp, reachability probe → *(dispatch real performers)*
→ `stop()` → kill daemon, close server, remove ephemeral containers, `rmtree` scratch.

**Invariant**: the server holds **no PR/CI state of its own** — every read/write
delegates to `FakeGitHubService`, so the performer-side and coordinare-side views can
never diverge (FR-005).

## Shared PR/CI state (`FakeGitHubService` — EXTENDED, reused)

The single record of a card's PR and its CI, read by both boundaries.

- **PR record** (`_prs[pr_id]`, `fake_github.py:205-217`): `pr_id` (`PR_N`, the
  `node_id` returned to the performer), `pr_number`, `url` (the `html_url`), `head_ref`,
  `base_ref`, `issue_item_id`, `reviews[]`, `merged`, `merge_commit`. Minted by
  `open_pr(...)` — **called from the REST handler in real mode** (D3), the stub path in
  stub mode. Same shape either way.
- **CI result** (`_ci_cache[head_sha]` → `CheckRollup`, `fake_github.py:474-491`): one
  real pytest run per head sha, authoritative for the coordinare CI gate, the approver's
  `ci_green`, **and** the performer's check-runs poll (D4).
- **Branch index** (NEW, passed to the server): `head_ref → issue_item_id`, so the REST
  `POST .../pulls` resolves the pushed branch back to the seeded card (D3). Derived from
  the runner's existing `fixtures_by_card` (`runner.py:152`).

**Identity chain (SC-004)**: performer pushes `head_ref` → `POST .../pulls` →
`open_pr` mints `PR_N` → performer reports `node_id=PR_N` back → coordinare's
`check_mergeability(PR_N)` / `get_pr_reviews(PR_N)` / `squash_merge(PR_N)` act on the
same record → real local merge into the fixture's default branch.

## Git-host configuration (NEW, injection points)

| Field | Where | Default | Bench value |
|-------|-------|---------|-------------|
| `git_base_url` | coordinare `config.py` → `WorkspaceManager.prepare()` (replaces hardcode `workspace.py:263,284`) | `https://github.com` | `git://127.0.0.1:9418` |
| `github_api_url` | coordinare `config.py:794` → `card_context` (`dispatch_performer.py:1395`) → `metadata` → `Score.github_api_url` → `main.py:1470-1494` override | `https://api.github.com` | `http://127.0.0.1:<port>` |
| `github_graphql_url` (coordinare) / `GITHUB_GRAPHQL_URL` (performer) | coordinare `config.py:795` → `card_context` (`dispatch_performer`) → `metadata` → **new** `Score.github_graphql_url` → **new** `main.py` override → `settings.GITHUB_GRAPHQL_URL` → `github.py:399` | `https://api.github.com/graphql` | `http://127.0.0.1:<port>/graphql` |
| `ALLOW_INSECURE_REPO_URL` | performer env (via `config.env` `-e`); gates the `repo_url` scheme relaxation (`models.py:68`) | unset (`https`-only) | `1` (admits `git://`) |
| ~~`network_mode`~~ | **REMOVED in review.** Host networking shares the host netns, so the entrypoint's egress iptables rules would flush + DROP the *host's* OUTPUT chain, published ports collide 1:1 across concurrent performers, and Docker Desktop shares the VM netns anyway. Containers stay bridged; `extra_hosts` covers the need. | — | — |

**Loopback is mandatory, not cosmetic**: the performer accepts an `http` GitHub URL only
for `{localhost,127.0.0.1,::1}` (`main.py:1489`); a non-loopback http host is rejected and
falls back to real GitHub. So the bench uses `127.0.0.1` URLs + `--network host` (D6).

**Rule**: every field defaults to today's production behavior. A run is "real bench
mode" iff the harness sets all of these. No production path changes.

## Run artifact (134's `RunArtifact` — reused, richer content)

Unchanged schema (validates against 134). In real mode `CardOutcome.dispatches` now
carries **>1** real `PersonaDispatch` (stage, role, model/backend, status, timing,
best-effort token usage) instead of the stub's single synthetic entry — the observable
difference SC-002 asserts. `Cost` stays a token×rate estimate (FR-012).
</content>
