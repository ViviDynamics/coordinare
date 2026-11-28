# Stage providers

A stage provider is an outside service that fills one lifecycle stage in place of a
performer. Coordinare hands it a pull request and a head, and takes back a verdict. The
providers are [Scrutare](https://github.com/ViviDynamics/scrutare) for the reviewing stage
(issue #402), [QAre](https://github.com/ViviDynamics/qare) for the QA stage (#404), and
[Progettare](https://github.com/ViviDynamics/progettare) for the architecting stage (#403),
where the input is a work item instead of a pull request.

Nothing changes until a role names a provider. A config without `stage_providers`, or
whose roles name none, runs every stage on performers exactly as before.

A role that names a provider runs no local harness and dispatches to no performer, so
`coordinare doctor` and the startup preflight leave it out of the harness and performer
protocol checks, and the startup preflight probes no model for it; the provider's own
checks (below) speak for it. A performer endpoint whose roles are all filled by providers
serves none of them (an endpoint may name a role or its stage): an ephemeral one has its
image checked but runs no harness, and a persistent one is not probed. A config whose
every role is filled by a provider needs no performer at all (issue #451). A role that
names no provider is checked as before, so one with neither a provider nor a performer
still fails.

## The stage contract

Every provider fills the same contract (`src/coordinare/services/stage_contract.py`):

| | |
| --- | --- |
| Input | the repository, the pull request, and the head SHA to judge; for QA also the base, the work item, and its acceptance criteria, each with an id that is stable across heads |
| Output | a verdict (`pass`, `changes_requested` or `needs_human`), the findings, the head that was actually judged, and a reference to the record; for QA also the outcome of every criterion with its evidence |

A stage that plans (architecting) takes a work item instead: the issue, pinned to the text
the planner reads (its title and body, and the clarifications coordinare recorded). Its
output is a blueprint, and the verdict belongs to the issue text it was planned from. See
[Progettare for the architecting stage](#progettare-for-the-architecting-stage).

Coordinare adapts the contract to the performer protocol, so everything downstream is the
same as for a performer: the verdict cache keyed by head (spec 125), the feedback loop to
the implementer, the system-error retry budget, and resumption after a restart.

The adapter enforces these rules for every provider:

- **One request per card, stage and head.** The request id is derived from the card, the
  stage, the head and an attempt number. Submitting again (a restart, a re-dispatch) finds
  the job that already exists instead of starting another.
- **The verdict belongs to the head that was judged.** When the result arrives, its head is
  compared with the pull request's current head. A result for an older head is refused, and
  the next dispatch judges the current head.
- **Failure is never a pass.** An unreachable provider, a GitHub read that fails, or a job
  that ended without delivering anything is a retryable system error: the card is retried on
  the daemon's usual schedule and blocks with the reason once that budget is spent. A job
  that ended without delivering anything is retried under the next attempt's request id.
- **A finished attempt is never resubmitted automatically.** A job that may already have
  posted its record, or that escalated to a person, blocks the card with the reason, and any
  automatic dispatch finds that job again. Only an operator restart of the stage starts the
  next attempt for the same head, whatever the last attempt said. A restart that has not
  been dispatched yet survives a daemon restart (the override is persisted). What a daemon
  restart forgets is which later attempt a restart already started: the next dispatch finds
  the earlier, finished job again, which is the side that never posts twice.
- **A QA pass needs every criterion proven.** A pass that names no criterion, or leaves one
  of them failed or unverified, holds the card for a person (the spec 120 evidence floor).
- **Evidence is posted only for the current head.** A provider that leaves posting to
  coordinare (QAre) is asked to post its record after the head check, never before.

## Scrutare for the reviewing stage

### Configure

```yaml
stage_providers:
  scrutare:
    kind: scrutare
    url: http://scrutare:8080   # reachable from coordinare
    token_env: SCRUTARE_API_TOKEN       # NAME of the env var holding the token
    installation: 12345678              # GitHub App installation id
    review_login: "vivi-scrutare[bot]"  # the login Scrutare's reviews are posted under

performers:
  reviewer:
    provider: scrutare
```

- `url` is Scrutare's App service as coordinare reaches it. Scrutare's docs keep
  `/api/reviews` and `/api/jobs` off the public route, so this is normally the cluster
  Service.
- `token_env` names the environment variable that holds Scrutare's performer API token
  (`SCRUTARE_APP_API_TOKEN` on the Scrutare side). Put the value in `.env` or the
  deployment's secret store, never in `config.yaml`. A value that is not a variable name is
  refused at load, and the error does not repeat it.
- `installation` must be the installation Scrutare's `SCRUTARE_APP_INSTALLATIONS` lists for
  the repository, or Scrutare refuses the request.
- `review_login` (required) tells coordinare which reviews are Scrutare's. It cannot also be listed in
  `human_reviewers`. Either form of an App's login (`vivi-scrutare[bot]` or `vivi-scrutare`)
  matches.

To opt in one symphony only, leave the global reviewer alone and override it:

```yaml
symphonies:
  - name: widgets
    github_project_number: 12
    overrides:
      performers:
        implementer: {}
        reviewer:
          provider: scrutare
```

`overrides.performers` replaces the global `performers` block for that symphony, so list
every role the symphony runs. The `stage_providers` catalog is read from the global config
only; a symphony that overrides it is refused at load. A provider-backed stage takes no
performer slot while the provider works.

Scrutare runs its own harness, model and configuration (`scrutare.yaml` on the reviewed
repository's default branch), so the reviewer's `backend`, `mode`, `workflow` and tuning
have no effect while it is opted in. The Scrutare repository needs that `scrutare.yaml`, or
every job fails before it starts.

### Check

`coordinare doctor` checks every provider a role uses: `GET /healthz` must answer, and an
authenticated read of `/api/jobs` must reach Scrutare's performer API and accept the token. The same check runs in the startup preflight.
`coordinare config validate` checks the configuration itself: the provider exists, it can
fill the role, the token is referenced by name, and the review login is not a human reviewer.

### Verdicts

| Scrutare | Coordinare |
| --- | --- |
| `approve` | the reviewing stage passes; the verdict is recorded against the reviewed head |
| `changes_requested` | the review's body and inline comments go to the implementer through the existing feedback loop; the next head is reviewed again |
| `escalated` | the card blocks for a person, with a link to Scrutare's review; an operator restart of the reviewing stage starts a fresh Scrutare review |

Scrutare's GitHub review is the reviewing stage's own output. In a symphony whose reviewer
uses Scrutare, `monitor_pr` never routes it a second time as outside feedback, and never
counts it as an approval, even if its login is on `trusted_bot_reviewers` or
`human_reviewers`. Only a human approval merges. A symphony that keeps its performer
reviewer treats Scrutare's reviews like any other bot's.

Feedback queued for the reviewing stage (for example a correction for the reviewer) is not
forwarded: Scrutare judges each head afresh. Coordinare logs
`stage_provider.feedback_not_forwarded` when that happens.

### Failures

| Job ends | What coordinare does |
| --- | --- |
| Scrutare unreachable, 5xx, queue full | retryable system error |
| `failed`, `interrupted` or `not_started` with `delivery: not_sent` | retryable system error; the retry submits the next attempt |
| any end with `delivery: confirmed` | blocks: the review was posted and a later step failed, and Scrutare says not to request another |
| any end with `delivery: uncertain` | blocks: the review may have been posted; inspect `posting.json` in the retained run first |
| `posted` without a receipt bound to the reviewed head, or posted under a login other than `review_login` | blocks: not a pass, and not safe to request again |
| 401 or 403 | at dispatch, blocks as a configuration error (check the token and the installation); while polling, a retryable system error |
| result for a head the pull request has moved past | refused as a retryable error; the current head is reviewed |

### One review per head, and Scrutare's webhook

Scrutare deduplicates performer API requests by request id only. Its docs say performer API
jobs "are never merged or skipped", so a webhook review of a head and coordinare's request
for the same head are two reviews. For a repository whose reviews coordinare drives, keep
Scrutare's App webhook from reviewing it (for example, run the App with its webhook
inactive). The gap is tracked in Scrutare's issue tracker.

### Timing

A review can take as long as Scrutare's `SCRUTARE_APP_JOB_TIMEOUT` (55 minutes by default)
and reports no progress events while it runs. If `dispatcher_dedup.stall_timeout_seconds`
or a reviewer `timeout_seconds` is set, set it above Scrutare's job timeout.

## QAre for the QA stage

QAre is a CLI, not a service. Coordinare runs the pinned QAre in an ephemeral performer
container (a [stage job](#stage-jobs-where-qare-and-progettare-run)) against a checkout the
performer makes, with QAre's posting turned off, and posts the evidence itself where the
built-in QA posts its own. Coordinare's own host never clones or fetches.

### Configure

```yaml
stage_providers:
  qare:
    kind: qare
    version: "2026.11.45"            # the exact release `qare --version` must print
    command: ["qare"]                # how to start it in the performer (default), as an argument list
    # image: ghcr.io/vividynamics/coordinare-performer-full:<tag>  # default: the qa performer's image
    # sandbox: sysbox                # each run gets its own Docker engine (Linux hosts with Sysbox)
    profile: .qa                     # the QA profile's directory in the repository (default)
    env_passthrough: [OPENAI_API_KEY]  # NAMES of variables QAre's nare may read
    # work_dir: /var/lib/coordinare/qare  # default: stage-providers/<name> beside the state file
    # timeout_seconds: 3600
    # retain_days: 14                 # how long a finished run's evidence and logs are kept

performers:
  qa:
    provider: qare
```

- QAre runs in the performer image: `image`, else the image of the performer endpoint that
  serves `qa`, else the first endpoint's. The full performer image ships QAre (copied from
  QAre's published `qare-web` image, pinned by digest, with the base image's nare); its
  release is `QARE_VERSION` in `agent/performer/Dockerfile.full`. `version` must match
  exactly, because QAre's interfaces can change between releases; a run on any other
  version holds the card.
- QAre needs no token from coordinare. Its nare reads its model endpoint and key from the
  environment, and only the variables named in `env_passthrough` reach it (the container
  takes them from coordinare's environment by name), along with a short list of basics
  (`PATH`, `HOME`, the locale, the proxy variables and `NODE_USE_ENV_PROXY`). Coordinare's own secrets never do. A
  value written where a name belongs is refused at load without being repeated.
- QAre runs the pull request's code as root in the run's container, so that code can read
  anything QAre can, including the values `env_passthrough` hands it (a model key, for
  example). Pass a key scoped to what QA needs, such as a gateway key with a spending limit,
  not an operator-wide one. Coordinare's GitHub token is not readable there: see
  [What the pull request's code can read](#what-the-pull-requests-code-can-read).
- The repository needs a QAre profile (`qare init` writes one). The base revision's profile
  is used when it has one, so the profile that runs is one the pull request cannot change,
  as in QAre's own pipeline; a pull request that adds the first profile uses its own. With
  no profile on either side the card is held with a message that says to run `qare init`.
  It does not fall back to the built-in QA: the symphony chose QAre, and a silent fallback
  would hide that its repository is not set up.
- QAre pins its own harness, so the qa role's `backend`, `mode`, `workflow` and tuning have
  no effect while it is opted in.

### Running QAre in the performer

The performer container is where the pull request's code runs: QAre's checks, and the app a
profile boots. The full performer image ships QAre with its browser driver (#442): command
and mail checks, the ledger and the judge, and browser flows. It has no container runtime
for QAre to use.

| Profile | In the full performer image |
| --- | --- |
| Command and mail checks | run |
| Browser flows, app started by a command or a `package.json` script | run (#442) |
| Anything whose app boots with Docker (`docker compose`), browser or not | runs with `sandbox: sysbox` and the sandbox image ([Docker sandbox](#docker-sandbox)); not in the full image alone |

A profile whose app boots with `docker compose` needs the [Docker sandbox](#docker-sandbox).
Coordinare never gives a QAre container the host's Docker socket: that would hand pull
request code the host's Docker, including coordinare's own containers.

Opt one symphony in with `overrides.performers`, as for Scrutare.

#### QAre's browser

QAre drives browsers through its own `playwright-core` (1.63.0 at QAre 2026.11.45), and a
Playwright release only launches the Chromium revision it was released with. So the
browser comes from QAre's release, not from the image's other Playwright:

- `Dockerfile.full` pins one QAre image, `ghcr.io/vividynamics/qare-web`, by tag and digest
  (`QARE_IMAGE`). It is QAre's core image of the same release plus the browser driver family
  under `/opt/qare/drivers/web`, so copying `/opt/qare` brings QAre and the Chromium its
  `playwright-core` expects together. The system libraries that browser needs are installed
  by that same `playwright-core` (`install-deps chromium`), with Xvfb and GTK 3 for QAre's
  Electron driver, as `qare-web` does.
- The image also keeps its Python Playwright and the Chromium it installs. The performer's
  own QA workflow and agents use that one. It is unpinned, so its revision moves with each
  Python Playwright release, and neither revision can stand in for the other.
- Both sit in Playwright's default cache, root's `~/.cache/ms-playwright`, as revision
  directories side by side: QAre's are linked in from `/opt/qare/drivers/web/browsers`
  (not copied), with QAre's `.links` entry so a later `playwright install` keeps them. Each
  Playwright finds its own revision through `HOME`, which the stage job passes to QAre.
  No `PLAYWRIGHT_BROWSERS_PATH` is set: QAre passes its environment to every check and
  app it starts, and a path set there would hide the other revision from them.
- That makes the layout depend on how the stage job runs: as root, with `HOME=/root`,
  which is how the full image runs. The stage job never hands QAre `XDG_CACHE_HOME` or
  `PLAYWRIGHT_BROWSERS_PATH`, and `env_passthrough` may not name them (refused at load),
  since either would move Playwright's cache away from the linked revisions. QAre and
  everything it starts therefore use `$HOME/.cache` for caches. A custom
  `image` that runs as another user must link QAre's revision directories and `.links`
  entry into that user's cache the same way.
- The image build checks both browsers in a clean environment (`HOME` and `PATH` only, as
  the stage job gives QAre): QAre's `playwright-core` must be the version its browsers were
  installed for, find Chromium in the default cache, launch it headless and load
  `about:blank`, and the Python Playwright must still launch its own. Before that the
  build runs `playwright install chromium`, whose cache cleanup would delete QAre's
  revisions if their `.links` entry were wrong. CI's image build fails otherwise.

To bump QAre, change `QARE_VERSION`, and set `QARE_IMAGE` to the `qare-web` tag of the same
release with that tag's index digest (`docker buildx imagetools inspect
ghcr.io/vividynamics/qare-web:<release>` prints it). The browser follows the release; there
is no separate browser pin. Keep `stage_providers.<name>.version` equal to `QARE_VERSION`.

### Docker sandbox

With `sandbox: sysbox`, each QAre run gets its own Docker engine, inside its own container,
without privilege (#435). It is off by default; off, runs are exactly as described above.

- **How.** The run's container (and the container that checks QAre's version) starts with
  `--runtime=sysbox-runc` and nothing else added: never `--privileged`, never a Docker
  socket, no seccomp or AppArmor change. With a sandbox, `env_passthrough` may not name a
  Docker client variable (`DOCKER_*`, `BUILDKIT_*`, `BUILDX_*`, `COMPOSE_*`, `CONTAINERD_*`, `PODMAN_*`, `TESTCONTAINERS_*`,
  `CONTAINER_HOST`), which could point the run at another daemon; it is refused at load. Sysbox gives the container its own user namespace,
  so its root is an unprivileged user on the host, and virtualises `/proc` and `/sys`, so a
  Docker engine runs inside it. The sandbox image's entry point starts that engine before
  the run. QAre then finds Docker on its usual socket, the app it boots on `localhost`, and
  the checkout at the same paths, all in the one container. When the engine does not
  answer, the container stops before QAre runs, so the run is retried rather than reported
  as a QA failure of the pull request.
- **Cleanup.** The engine and everything it starts live in the run's container. Removing
  that container, which coordinare does by the run's name (a restarted daemon finds it by
  request id, as for any stage job), removes them too. Nothing is kept on the host: every
  run starts with an empty image cache, and pulls and builds its stack.
- **Host prerequisite: Sysbox, Linux only.** Install Sysbox on the Docker host (its
  [install guide](https://github.com/nestybox/sysbox/blob/master/docs/user-guide/install-package.md):
  a supported distribution, kernel 5.12 or later, systemd), which registers `sysbox-runc`
  with Docker. `coordinare doctor` checks that `docker info` lists `sysbox-runc`. Where it
  does not, the provider says it is unavailable and why, and a stage that names it is
  retried and then blocked with that reason. macOS Docker Desktop has no `sysbox-runc`
  unless Enhanced Container Isolation (a Docker Business feature) provides it; a Linux VM
  with Sysbox is the path on a Mac. Kubernetes is not supported yet (#443).
- **Image.** The engine is in its own image variant, built FROM the full performer image:

  ```sh
  docker build -t coordinare-performer:sandbox -f agent/performer/Dockerfile.sandbox agent/performer/
  ```

  Name it in `image`. It adds Docker's engine, containerd, buildx and compose, pinned (about
  344 MB installed). They are not in the full image, so performers that run pull request
  code without a sandbox carry no engine. With `sandbox: sysbox`, the health check (and
  doctor) asks the image's own engine to answer, with compose and buildx, and reports an
  image that has none.
- **Egress allowlist (#447).** When the runs take an endpoint with `egress_allowlist` (the
  endpoint that serves `qa`, else the first endpoint with an image), it is enforced outside
  the run's container, because nothing inside can be trusted to keep it: root in the sandbox,
  and anything that can use its Docker (`docker run --privileged --network host`), can
  rewrite the container's own firewall. What is enforced where:

  | Where | What | Can code in the sandbox change it |
  | --- | --- | --- |
  | The host's Docker | Each run gets a network of its own, `docker network create --internal`, with no route out, and with no host address on its bridge (`com.docker.network.bridge.inhibit_ipv4`), so host services are not reachable through a gateway address. The run's container is on that network only. | No: the network and the host's forwarding rules are host state, and the sandbox's root is an unprivileged user on the host with no Docker socket. |
  | The proxy container, `<run>-egress` | `performer.egress_proxy`, on the default bridge and on the run's network: the one way out. It relays `CONNECT host:port` and plain `http://` requests only to the allowlisted hosts (exact names, any port, resolved when the connection is made) and answers anything else with 403 without connecting. A plain request is sent with `Connection: close` and a `Host` header naming the host it checked, and the connection ends with its response, so a client can neither reuse it for another host nor ask the allowed server for another site. Inside a `CONNECT` tunnel (TLS) it cannot see the requested site, so a shared front end that serves an allowed name (a CDN) could still be asked for another site behind it. It runs as `nobody` on a read-only root, with the endpoint's `extra_hosts`, and exits after three minutes with no connection. | No: it is another container. |
  | The run's container | Its entry point waits for the proxy before it starts dockerd, and stops the run (it is retried) when the proxy does not answer. It points `HTTP_PROXY` and `HTTPS_PROXY` at the proxy, and `NO_PROXY` at loopback, for the stage job, QAre, git, gh, model calls and dockerd's image pulls, and sets `NODE_USE_ENV_PROXY=1` so QAre's own Node requests use the proxy too, and connects to the proxy every 30 seconds while the run lasts, which keeps it from exiting. | Yes, but that only changes which allowed host a request goes to: without the proxy there is no route at all. |
  | Containers the sandbox's Docker starts | Their traffic is NATed out of the run's one interface, so they have the same single exit. They get no proxy variables (compose services reach each other by name, which a proxy would break); a service or build that needs an allowed host must be given the proxy, for example `${HTTPS_PROXY}` as a compose build argument. | Only as above. |

  The probes (version and engine checks) run with `--network none`; the engine check also
  asks the image for `performer.egress_proxy`, so an image built before this release is
  reported by health. If the network or the proxy cannot be set up, coordinare removes what
  it made and does not start the run. The proxy goes on its own a few minutes after its run;
  the network goes when coordinare stops the run or finds it gone, and each start sweeps the
  rest (labelled networks whose run is gone, older than ten minutes). Each running sandboxed
  run with an allowlist takes one network from Docker's address pools
  (`default-address-pools`), so size them for the concurrency you run. DNS is not part of
  the design: the proxy resolves names. Whether Docker's embedded resolver
  answers outside names on an internal network depends on the Docker version; where it does,
  a DNS query channel remains, as it does for the iptables baseline below. The proxy
  connects to the allowlisted hosts directly: a Docker host that reaches the internet only
  through an upstream proxy is not supported in this mode. The proxy also listens on the
  default bridge (its route out), where other containers on that bridge could reach it;
  that gives them only the allowlisted hosts, which a container on the bridge can already
  reach. The sandbox image still refuses `PERFORMER_EGRESS_ALLOWLIST`, the in-container
  mode.
- **Secrets and the engine (#450).** The run's container is given the GitHub token and the
  `env_passthrough` values, and a process keeps the environment it started with for as long
  as it runs. The entry point therefore starts dockerd (and the containerd it starts) and the
  proxy heartbeat from an empty environment with only `PATH`, the proxy variables and
  `SSL_CERT_FILE`/`SSL_CERT_DIR` (for pulls through a TLS-intercepting proxy), never the
  container's, and only the stage job takes the container's environment.
- **Browser profiles.** The sandbox image is built FROM the full image, so it has QAre's
  browser driver (#442) too: a browser profile whose app boots with `docker compose` runs
  in the sandbox. One that starts its app with a command needs no sandbox.

### What a run does

Each run has a directory of its own under `work_dir`, named by its request id (card, stage,
head and attempt), and a performer container of its own (a stage job) that does the work,
so a daemon restart finds the run still going or finished:

1. `qare --version` must print the pinned version.
2. The performer's workspace layer fetches the head and base, by SHA at depth 1, into the
   container's own filesystem (never the mounted run directory) with coordinare's GitHub
   token, which is never written to disk and never reaches QAre.
3. The work item's criteria are written as `{id, text}`. They come from the architect's
   verification brief when it has criteria, else from the issue's acceptance criteria, and
   each id is a hash of its text, so a criterion keeps its id on every head.
4. `qare plan --criteria` maps each criterion to checks against the change's diff. A QAre
   job whose criteria carry no checks runs nothing (every criterion is unverified), so the
   plan comes first (ViviDynamics/qare#450 asks QAre's docs to say so).
5. `qare run --plan` runs the checks against both sides and writes
   `evidence/result.json`. Its `schemaVersion` is checked before anything else is read.

#### What the pull request's code can read

QAre's checks run the pull request's code as root in the run's container, where it can read
every process's initial environment (`/proc/<pid>/environ`) and, as root, its memory (#450).
So the token is held only while it is needed, and never while that code runs:

- The token reaches the stage job only. The job takes it out of its own environment at once
  and starts itself again (`execve`) without it, reading it from a pipe that is then closed.
- It is used for the checkout (step 2, as an `http.extraHeader` in git's environment, never on
  disk) and for GitHub's compare diff, both before QAre plans. Then the job starts itself once
  more, without it: the process that waits on QAre never held the token, so no copy is left
  in its memory, its environment or its arguments. QAre never gets it.
- With the sandbox, the engine and the proxy heartbeat never see the container's environment
  ([Docker sandbox](#docker-sandbox)); the egress proxy container is given no variable at all.
- What stays readable: the variables `env_passthrough` names and the proxy variables, which
  QAre needs while it runs. Host-side, `docker inspect` of the run's container shows the
  token, as it shows any `--env-file` value, to anyone with the host's Docker.

A step that runs past `timeout_seconds` is stopped with every process it started (each
command runs in a process group of its own). When a run ends its checkouts go with its
container, and a
finished run is shrunk to its record `retain_days` after it ended: its request, status,
`result.json` and publish record stay, so a later dispatch at the same head still finds the
attempt and never posts its evidence twice; the evidence files, plan and log go (they are on
`qa-assets`). A result that was never published is kept whole. A run still going is never
touched. QA needs the pull request's base: while GitHub
gives none, the dispatch is retried rather than run against the head alone.

### Verdicts

| QAre | Coordinare |
| --- | --- |
| `passed` (exit 0), every criterion proven | QA passes for the tested head (`qa_passed`, recorded in the verdict cache) |
| `failed` (exit 1) | the failing criteria, with their reasons and evidence links, go to the implementer (`qa_failed`); the next head is run again |
| `blocked` (exit 2) | the card is held: the environment stopped the run, not a defect; the reasons are in the comment |
| `refused` (exit 3) | the card is held: QAre refused for a named reason (a missing stub, a missing profile) |
| `waived` (exit 5) | the card is held: a person recorded a waiver in QAre, and a waiver is never a pass, so a person decides whether the card moves on |

### Evidence

The run's `result.json`, `plan.json` and every evidence file the result names are committed
to the repository's `qa-assets` branch under `qare/<request id>/`, the branch the built-in
QA keeps its screenshots on. Text is redacted again before it leaves the host; files outside
the evidence directory, symbolic links and binaries other than images are left out. One
"QA Evidence (QAre)" comment, naming the head that was tested, every criterion's outcome and
links to its evidence, goes on the pull request and on the issue. A held run gets the same
comment when it is held on its verdict (blocked, refused, waived, or a pass that is not
proven for every criterion), so the person it is held for can read why.

Publishing can be repeated safely: evidence already on the branch is not committed twice, a
comment carrying the run's marker is not posted twice, and `published.json` in the run's
directory records what is done. A publish GitHub refuses is a retryable system error.

### Failures

| Run ends | What coordinare does |
| --- | --- |
| QAre cannot start, the checkout or the plan fails, GitHub's compare fails for a reason other than size, the run times out, its performer stops without a status, the performer cannot be started | retryable system error; the retry runs the next attempt |
| exit 4 (no verdict), or no `result.json` | retryable system error |
| another version installed than `version` | the card is held: install the pinned release or change the pin |
| an unknown `schemaVersion` or verdict, an exit code that contradicts the verdict, the result of another job | the card is held: the result is outside QAre's contract |
| no profile, or no acceptance criteria | the card is held with what to add |
| result for a head the pull request has moved past | refused as a retryable error and nothing is posted; the current head is run |

### Check

`coordinare doctor` and the startup preflight check that `qare --version` prints the pinned
version in a container of the performer image, that `work_dir` is writable, and that the
configured repository (`github_org` and `project_name`) has the profile on its default
branch. A passed version check stands for five minutes, so a dispatch's health check does
not start a container every time.

## Stage jobs: where QAre and Progettare run

Specs 107 and 555 say coordinare never clones, fetches, rebases or pushes on its own host,
with no exceptions (#423). QAre and Progettare are CLIs that read a checkout, so each of
their runs is a stage job: coordinare starts one container of the performer image per
request id,

```
docker run -d --rm --name coordinare-stage-<hash of the run directory> \
  --label coordinare.stage_job=<request id> -v <run dir>:<run dir> <performer image> \
  sh -c 'exec python -m performer.stage_jobs <qare|progettare> <run dir> >><run dir>/runner.log 2>&1'
```

and the performer's workspace layer makes the checkout inside it, in the container's own
filesystem. Only the request, the status, the evidence and the logs are in the mounted run
directory.

- **Restarts.** The container is named from its run directory and is not labelled
  `coordinare.performer.id`, so the startup sweep of orphaned performers leaves it running.
  A restarted daemon asks Docker about that name while the run has not reported, and reads
  a finished run from its files; submitting the same request id again finds the job and
  never starts a second one.
- **The token** reaches the container through a private `--env-file`, removed as soon as
  `docker run` returns. It is never an argument and never in the run directory. Variables
  named in `env_passthrough` or `pass_env` (and the proxy variables) are passed by name.
- **Isolation** is the role's performer's: the endpoint's `egress_allowlist` and
  `extra_hosts` apply to the stage job as to the role's other work. The allowlist is enforced
  outside the container, with or without the sandbox (#449): the job runs on its own internal
  network behind the same allowlisting proxy as the sandbox ([Docker sandbox](#docker-sandbox),
  [Egress allowlist](egress-allowlist.md)). Without the sandbox, the job runs the performer
  image's own entry point, which has no heartbeat, so the proxy follows the job's name on the
  network and exits once the job is gone. With the endpoint's `egress_mode: iptables`, a job
  without the sandbox gets the old in-container rules instead: a baseline, not a boundary, since
  the container then keeps `NET_ADMIN` and its code runs as root.
- **Values must work from the container.** A passed variable keeps its value, and the
  container is on Docker's bridge network: a model endpoint on `localhost` or `127.0.0.1`
  names the container itself. Point such variables at an address the container reaches
  (for example `host.docker.internal`, with an `extra_hosts` entry on Linux), as for any
  performer.
- **Mounts.** The run directory is mounted at the path it has on coordinare's side. When
  coordinare itself runs in a container that talks to the host's Docker, keep `work_dir`
  and `runs_dir` at the same path on the Docker host, as for env caches.
- **Ownership.** The container runs as root; when a run ends it gives what it wrote back to
  the run directory's owner, so coordinare can read, publish and prune it.
- **Runtime.** Stage jobs need the Docker runtime. Under `agent_transport: kubernetes` the
  provider reports itself unavailable, and a stage that names it is retried and then
  blocked with that reason. A QAre provider with `sandbox: sysbox` also needs the
  `sysbox-runc` runtime ([Docker sandbox](#docker-sandbox)).

## Progettare for the architecting stage

Progettare plans one GitHub issue: a bounded read-only survey, one blueprint, sizing, and a
brief for each reader (implementer, QA, documenter). It commits nothing and posts nothing.
Coordinare runs its released CLI, `progettare blueprint`, at a pinned version in an ephemeral
performer container (a [stage job](#stage-jobs-where-qare-and-progettare-run)), and turns
the run's briefs into the spec-165 blueprint its readers already take their briefs from.

### Configure

```yaml
stage_providers:
  progettare:
    kind: progettare
    version: "2026.11.2"                          # `progettare --version` must print exactly this
    runs_dir: /var/lib/coordinare/progettare-runs  # survives a coordinare restart
    gh_token_env: GITHUB_TOKEN                    # NAME of the env var with a GitHub token
    pass_env: [OPENAI_API_KEY]                    # NAMES of the model credentials it needs
    # command: progettare                         # a name on the performer's PATH or an absolute path
    # image: ghcr.io/vividynamics/coordinare-performer-full:<tag>  # default: the architect performer's image
    # config_path: /etc/coordinare/progettare.yaml
    # timeout_seconds: 3600

performers:
  architect:
    provider: progettare
```

- Progettare runs in the performer image: `image`, else the image of the performer endpoint
  that serves `architect`, else the first endpoint's. The full performer image ships it as
  its release installer installs it (a uv tool with its own release-matched Nare), with
  `git` and `gh`; its release is `PROGETTARE_VERSION` in `agent/performer/Dockerfile.full`.
- `command` is a name on the performer's `PATH` or an absolute path; a relative path is
  refused.
- `version` is the release coordinare speaks to. A run refuses to start under another
  version, and `doctor` says which version is installed.
- `gh_token_env` names the variable that holds a GitHub token able to read the issue and
  clone the repository. The run receives it as `GH_TOKEN`; git gets it through the
  environment, never on a command line. A coordinare that signs in as a GitHub App needs a
  token here as well, since the run cannot mint one.
- `pass_env` names the other variables the run receives, typically the model provider's
  credential for the models `progettare.yaml` selects. The container takes them from the
  daemon's environment by name; it gets nothing else from it beyond the proxy variables.
- `config_path` is the `progettare.yaml` to plan with, on coordinare's side; it is copied
  into the request's directory for the run. Without it, each run uses the one on the
  planned repository's default branch, and a repository without one blocks the card.
- A symphony opts in through `overrides.performers`, as for Scrutare. Progettare pins its
  own harness and models, so the architect's `backend`, `mode`, `workflow` and tuning have no
  effect while it is set.

### Check

`coordinare doctor`, and the same check in the startup preflight, confirms the performer
image has the CLI at the pinned version and `git` and `gh` on `PATH` (in one container of
that image), `gh_token_env` and every `pass_env` variable are set, `config_path` exists when
given, and `runs_dir` is writable.

### How a run is found again

Progettare has no job API and takes no request id (ViviDynamics/progettare#69), so
coordinare names each run itself. Every request gets its own directory under `runs_dir`,
named by the request id, which is derived from the card, the stage, the pin and the
attempt. A stage job's container, with that directory mounted, clones the default branch
into its own filesystem, runs the CLI with the directory's `runs/` as `--runs-dir`, and
ends when the run does. Looking a request up reads its directory:

- no `runner.json` and the container is running: the run is in flight, so a restart or a
  re-dispatch waits for it;
- `run.json` says `complete`: the plan;
- `run.json` says `blocked`: the questions for a person;
- `run.json` says `failed`, or there is no `run.json` (the clone failed, the run timed out,
  or its container was stopped): ended without a plan.

A restart of coordinare leaves the container running, and the next poll finds it by its
name, so a restart during planning never produces a second plan. A restart of the host or
of Docker stops the run; that attempt ended without a plan and is retried, which still
leaves one plan.

### Outcomes

| Progettare | Coordinare |
| --- | --- |
| `complete` | the architecting stage passes (`plan_committed`) with the blueprint: the implementer brief's milestones, the QA brief's testable criteria, the documenter brief's topics (when Progettare's sizing turned the documenter on; Progettare names no location, so the documenter workflow writes each topic on a page under the docs root named by the topic), and the size (`single_turn` is small, `milestone_loop` is large) |
| `blocked` | the card blocks with Progettare's questions |
| `failed`, clone failure, timeout, a run killed by a restart | retryable system error; the retry starts the next attempt, which is safe because the CLI posts nothing |
| a record at another contract version | blocks: coordinare speaks Progettare's artifact contract version 1 and never guesses at another |
| no `progettare.yaml` | blocks with the reason |
| token not set, or another version installed | blocks at dispatch as a configuration error |

Nothing is committed to the branch. As with the spec-165 workflow architect, the blueprint
is held on the card's session and each reader's brief is cut from it at dispatch. The run
directory keeps Progettare's full record (`blueprint.json`, the survey, the briefs) for
people to read; the blueprint's `source.run_dir` points at it.

A finished plan must be of the issue as it is now: when the run ends, the title and body it
recorded are compared with the issue's current ones, and a plan of text that changed since
is refused and the current text planned. The blueprint also records the pin it answers, so
a later dispatch of the architect reuses it only while the card's title, body and recorded
answers still give that pin. An answer recorded while a run is in flight is not seen by
that run (Progettare read the issue's comments when it started); the plan is used, and the
next dispatch of the architect plans again.

The clone goes to `git_base_url`, the git host coordinare's performers clone from
(github.com by default), never to a host a card names. A card whose issue URL is on another host,
port included, blocks instead of being planned, so the token never goes anywhere else.

Feedback routed back to the architect, such as security findings or an observer's
correction, cannot reach Progettare: it reads the issue, not coordinare's feedback. As for
Scrutare, coordinare logs `stage_provider.feedback_not_forwarded`, and the plan of the
unchanged issue stands; it is not planned a second time. If the same findings come back,
coordinare's feedback-cycle budget blocks the card. To make Progettare plan for a finding,
put what the plan must address in the issue (which is a new request), or restart the
architecting stage.

### Answering a blocked run

A new answer starts a new run. When a person answers the card's questions, coordinare
records the answer, and the recorded answers are part of the pin, so the next dispatch is a
new request. Editing the issue body changes the pin too. Progettare reads the acceptance
criteria from the issue body, so a question about them is answered by editing the body's
acceptance section; Progettare reads other answers from the issue's comments. An operator
restart of the architecting stage also starts a new run.

### Timing

A run can take as long as the clone (up to 10 minutes) plus `timeout_seconds` (an hour by
default), and reports no progress while it works. If the architect's `timeout_seconds` or
`dispatcher_dedup.stall_timeout_seconds` is set, set it above Progettare's `timeout_seconds`
plus 600.

### Work items that are not GitHub issues

Progettare plans GitHub issues only. A card whose work item is not one (no GitHub issue URL)
blocks at dispatch with a reason that names ViviDynamics/progettare#68, which tracks
planning a work item handed in as data. Until it lands, keep the architect on a performer
for such a symphony.

