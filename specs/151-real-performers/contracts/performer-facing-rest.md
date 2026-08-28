# Contract — Performer-facing fake GitHub REST

The `FakeGitHubServer` (aiohttp) MUST answer the calls the performer's
`agent/performer/src/performer/github.py` makes, in the **exact shapes** that file
parses. Each endpoint below cites its performer parse site — a contract test
(`tests/contract/test_151_performer_boundary.py`) drives the real performer client
against the fake and asserts these.

Base URL = the performer's `GITHUB_API_URL`, set at runtime by `main.py:1470-1494` from
`Score.github_api_url`. The bench sets it to `http://127.0.0.1:<port>` (loopback, reached
via `docker run --network host`) — the performer rejects a non-loopback `http` host and
falls back to real GitHub (`main.py:1489`), so the address MUST be `127.0.0.1`/`localhost`.
Auth header is present but not verified (bench token is `fake-token`).

---

## 1. Create pull request — `POST /repos/{owner}/{repo}/pulls`

Performer: `create_pull_request` (`github.py:187-233`). Request body has
`{title, head, base, body}`.

**Response 201** (parsed at `github.py:229-231`):
```json
{ "html_url": "https://fake/bench-org/bench-repo/pull/1", "node_id": "PR_1" }
```
- `html_url` ← `FakeGitHubService._prs[pr_id]["url"]`; `node_id` ← `pr_id` (`PR_N`).
- Handler resolves `head` (branch) → seeded `issue_item_id` (branch index, D3), then
  calls `open_pr(issue_item_id=..., head_ref=head, base_ref=base)`.

**Idempotency**: if a PR already exists for `head`, MAY return **422** with
`{"errors":[{"message":"... already exists ..."}]}` — the performer then falls back to
lookups 2a/2b. Returning **201** with the existing record is also acceptable
(`open_pr` is called once per card in practice).

## 2a. Existing PR lookup — `GET /repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open`

Performer: `get_existing_pull_request` (`github.py:55-73`). **Response 200**, JSON array;
first element parsed for `html_url`, `node_id`. Empty array ⇒ performer treats as 404.

## 2b. PR head sha — `GET /repos/{owner}/{repo}/pulls/{pr_number}`

Performer: `get_pr_head_sha` (`github.py:76-88`). **Response 200**:
```json
{ "head": { "sha": "<40-hex>" } }
```
`sha` ← `FakeGitHubService._rev(pr["head_ref"])`.

## 3. Default branch — `GET /repos/{owner}/{repo}`

Performer: `get_default_branch` (`github.py:43-52`). **Response 200**:
```json
{ "default_branch": "main" }
```

## 4. Check-runs — `GET /repos/{owner}/{repo}/commits/{ref}/check-runs?per_page=100`

Performer: `get_check_runs` (`github.py:106-115`), classified by
`summarise_check_runs` (`github.py:159-184`). **Response 200**:
```json
{ "check_runs": [
  { "name": "pytest", "status": "completed", "conclusion": "success", "id": 1 }
] }
```
- Derived from `FakeGitHubService._ci_rollup(pr)` for the PR whose `head_ref` resolves to
  `ref` (real pytest, cached by head sha — D4). `conclusion ∈ {success, failure}`;
  `status: "completed"`. While pytest hasn't run yet the handler MAY return
  `status:"in_progress"` (no `conclusion`) so the performer polls (verdict `pending`).

## 5. GraphQL — `POST /graphql`

Performer: `resolve_pr_review_threads` (`github.py:399-`), reached via the new
`GITHUB_GRAPHQL_URL` — set at runtime from `Score.github_graphql_url` by the parallel
`main.py` override (same loopback rule as the REST base). Best-effort: the fake MAY return
`{"data":{...empty threads...}}` or any non-2xx; the performer logs and returns 0 either
way. **The only hard requirement is that this call goes to the loopback bench host, never
`api.github.com`** (SC-003).

---

## Shapes NOT required

Review-posting, PR comments, issue comments, and thread resolution beyond the above are
best-effort in a bench run (they degrade to no-ops). The contract test asserts only the
five surfaces the happy-path implement→PR→CI flow depends on.

## Error behavior (spec Edge Cases)

- PR-create failure (handler returns 5xx): the performer raises `GitHubAPIError`; the
  card reaches a terminal non-merge state and an artifact is still emitted (FR-008).
- Server unreachable: the runner's pre-dispatch reachability probe fails fast with an
  actionable error — no hang.
</content>
