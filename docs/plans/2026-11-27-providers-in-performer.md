# Run the Progettare and QAre stage providers inside a performer, so Coordinare never clones

Issue #423

## Scope
In:
- A stage job: one ephemeral container of the role's performer image per request id,
  started by the daemon with `docker run -d --rm`, the request's directory mounted at the
  same path, running `python -m performer.stage_jobs <qare|progettare> <dir>`
  (`services/stage_jobs.py`, `performer/stage_jobs/`).
- The checkout made by the performer's workspace layer (`performer/stage_checkout.py`,
  built on `workspace._run_git`), in the container's own filesystem, never the mounted
  directory. The token goes to git as an `http.extraHeader` scoped to the clone URL's host.
- QAre and Progettare providers start and find their runs through the stage job: the
  same request ids, the same job directory files, verdict mapping, head and pin checks,
  blocked and system-error handling. Liveness is the container's (by a name derived from
  the job directory), not a host pid or an `flock`.
- QAre evidence publishing stays on the daemon (`qare_publish.py` reads the job directory
  and writes through the REST Git Data API; it needs no checkout).
- The pinned CLIs in `Dockerfile.full`: QAre copied from its digest-pinned core image,
  Progettare and its nare from SHA-256-checked release wheels, and `gh`.
- `stage_providers.<name>.image` (optional) for both providers; doctor and preflight check
  the CLI version in a container of that image.
- AGENTS.md lists no exception to 107 and 555; a test fails if either provider spawns git
  on the daemon host.

Out:
- Stage jobs under the Kubernetes transport: the provider reports itself unavailable with
  the reason. The two providers shipped the same day as this change, and the stage job
  needs a mounted directory shared with the daemon.
- A browser driver or a container runtime for QAre inside the performer: the full image
  carries QAre's core (command and mail checks). A profile that boots an app needs a
  performer image that adds one, and handing a run the Docker socket is an operator's
  trust decision.
- Changes to the QAre or Progettare repositories. Scrutare (#402), which needs no checkout.

## Assumptions
- A Docker container outlives the daemon, so a restarted daemon finds a run in flight by
  asking Docker about its name. The container is not labelled `coordinare.performer.id`,
  so the startup orphan sweep leaves it running.
- `docker run --name` is the start guard: a second start of the same request finds the
  container (Docker refuses the name) instead of starting another run.
- The container's output is appended to `runner.log` in the job directory, so a run that
  dies before it reports still says why after `--rm` removed its container.
- The container runs as root and gives what it wrote back to the job directory's owner
  when it ends, so the daemon can publish and prune it.
- The production daemon image ships no performer package, so the job directory's file
  names live on both sides and a test checks that they agree.

## Tasks
- [x] 1. Performer side: `performer.stage_jobs` (QAre and Progettare runs) and
  `performer.stage_checkout` (tests/unit/test_404_qare_runner.py, retargeted).
- [x] 2. Daemon side: `services/stage_jobs.py` with the Docker runtime
  (tests/unit/test_423_providers_in_performer.py).
- [x] 3. QAre provider on stage jobs (tests/unit/test_404_qare_provider.py).
- [x] 4. Progettare provider on stage jobs (tests/unit/test_403_progettare_provider.py,
  tests/unit/test_403_progettare_wiring.py).
- [x] 5. No git on the daemon host, restart resumption, contract agreement
  (tests/unit/test_423_providers_in_performer.py).
- [x] 6. Config `image`, doctor fix text, `Dockerfile.full`, docs, AGENTS.md.
