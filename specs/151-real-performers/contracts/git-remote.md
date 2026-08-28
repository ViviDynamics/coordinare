# Contract — Performer-facing git remote

The performer clones the target repo and pushes its work branch, then reports the
branch it pushed. In a real bench run this MUST hit the harness-local git server, never
`github.com` (FR-003, SC-003).

---

## Endpoint

`git daemon` serves the bare repo at:

```
git://127.0.0.1:<git_port>/bench-repo
```

started with `--export-all --enable=upload-pack --enable=receive-pack --port=<git_port>`
against `FakeGitHubService._bare_repo` (134's real `materialize_repo` bare repo).
`<git_port>` defaults to git's well-known **9418**; if that port is unavailable the runner
picks a free port (bind-test a socket) and threads the actual value into
`config.git_base_url` (`git://127.0.0.1:<git_port>/bench-repo`) — no hardcoded-port
collision with a stale daemon or another local `git://` service.

The performer **container** runs with `docker run --network host` (Linux) so `127.0.0.1`
inside the container reaches the host's git daemon. Loopback is required because the
performer only accepts an `http` REST host on `{localhost,127.0.0.1,::1}`
(`main.py:1489`) — the git and REST hosts are addressed consistently over loopback.

## What the performer does (and the contract must satisfy)

| Performer op | Requirement on the server |
|--------------|---------------------------|
| clone `repo_url` | `upload-pack` enabled; the bare repo has a default branch (`main`) with the fixture's base tree |
| create + push work branch | `receive-pack` enabled; push of a new ref succeeds anonymously |
| (coordinare) later reads pushed head | the pushed branch is visible to `FakeGitHubService._rev(head_ref)` — same bare repo, so it is |

The performer's `repo_url` comes from `WorkspaceInfo.repo_url` (built from
`config.git_base_url`, D7) and flows through `http_performer_service` (`:762-765`)
unchanged.

## Scheme relaxation (required, flag-gated)

`repo_url` = `git://127.0.0.1:9418/bench-repo` does **not** match the performer's
`https`-only validator (`agent/performer/src/performer/models.py:68`). The validator is
relaxed to also accept `git://` **only when** `ALLOW_INSECURE_REPO_URL` is set in the
performer environment (the bench sets it via `config.env`). Default (unset) keeps
`https`-only — production is unchanged.

Auth: the git daemon is anonymous; the performer's token / `http.extraHeader` is unused
on `git://` (harmless).

## No-real-GitHub guarantee (SC-003)

With outbound access to `github.com` / `api.github.com` blocked, a real run MUST still:
clone, push, open its PR (REST), poll checks, and reach a terminal state. Any attempt to
reach a real GitHub host is a contract violation. Verified by running the fixture with
egress to GitHub denied and asserting terminal completion + artifact emission.
</content>
