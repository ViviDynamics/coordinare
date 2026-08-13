# Spec 134 — real-performer path: scope finding & follow-up

**Status:** Phase 1 lands the evaluation substrate (acceptance criteria 1, 2, 4, 5,
6) verified with a **stubbed performer** — which the ticket explicitly sanctions for
the integration test ("cheap/**stub** model"). Acceptance criterion 3's
**"real performers"** is **deferred** to a scoped follow-up for the reason below.

## What we found (code-grounded)

The ticket's **Section B fidelity model does not hold as written.** It assumes
"performers push to `file://` bare repos and PR diffs are read from real git." In
reality, a real performer does **not** interact with the coordinare's in-process
`FakeGitHubService` at all — it talks to GitHub over the network, itself:

1. **Clone/push over HTTPS only.** The performer clones and pushes
   `Score.repo_url`, which is validated `^https://[host]/owner/repo(.git)?$`
   (`agent/performer/src/performer/models.py:8`) — `file://` and `http://` are
   rejected. The coordinare's `WorkspaceManager` hardcodes
   `https://github.com/{org}/{project}.git` with **no host override**
   (`src/coordinare/workspace.py:263,284`).
2. **The performer opens the PR itself**, via a real REST call
   `POST {GITHUB_API_URL}/repos/{owner}/{repo}/pulls`
   (`agent/performer/src/performer/github.py:63`), then polls check-runs
   (`github.py:109`). It reports `pr_url` + `pr_node_id` back in its `pr_opened`
   response; the coordinare only *reads those back*
   (`monitor_performer.py:894-918`) — it never creates or discovers the PR on the
   happy path.

So the coordinare-side `FakeGitHubService` (which this spec delivers, and which is
correct and sufficient for the coordinare's own reads and for the stubbed path) is
**not on the performer's HTTP path**. "Fake the GitHub API only, with real
performers" therefore requires faking GitHub at the **performer's boundary** too.

## What a real-performer run actually requires

- A **local git remote** the performer container can clone/push over — either relax
  the performer's HTTPS-only `repo_url` regex to allow a local endpoint, or stand up
  a real HTTPS git server (self-signed cert + container trust).
- A **fake GitHub REST service** answering at `config.github_api_url` (which already
  allows `http://localhost`): at minimum `POST /repos/{o}/{r}/pulls`,
  `GET /repos/{o}/{r}` (default branch), and
  `GET /repos/{o}/{r}/commits/{ref}/check-runs` (a passing verdict) — **sharing PR +
  CI state with `FakeGitHubService`** so the coordinare's later
  `check_mergeability`/`get_pr_reviews`/`squash_merge` on the same `pr_node_id`
  agree.
- A **coordinare change** to make the hardcoded git host configurable
  (`src/coordinare/workspace.py`).
- **Docker networking** so the performer container reaches those host services.

This spans **both** the coordinare and performer packages plus Docker/networking —
materially larger than an in-process fake, and it contradicts a stated design
assumption. That is a **PM-level scoping decision**, not a silent expansion.

## Recommendation

Land the substrate now (it is a clean, self-contained, tested unit that unblocks the
Protocol/fake/artifact/runner surface 135–137 build on), and open a follow-up to
build the performer-boundary GitHub fake for criterion 3. The runner already carries
a `stub` flag and a `# ponytail:` marker at the seam
(`src/coordinare/bench/runner.py`) where the real path plugs in.

Open question for the PM: is the intended fidelity (a) a fully-faked GitHub the
performer container talks to (the work above), or (b) real performers against a real
throwaway GitHub repo with only the *board* simulated — which changes the
architecture and the "simulated board" guarantee?
