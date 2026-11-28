# QAre Docker sandbox: design gate (#435)

Date: 2026-11-27
Status: decided (Sysbox on Linux hosts, comment below), built in this branch. Live proof waits for a
Linux host with Sysbox.

## The decision being met

QAre jobs (stage jobs since #423 / PR #432) get their own isolated Docker. The host's
Docker socket is never mounted into a container that runs pull request code. Opt-in per
symphony or provider, default off; off means #432's behaviour exactly.

Gate rule for this issue: proceed only if one option meets that without `--privileged` on
the default runtime and without a host prerequisite the operator must install. A
documented, doctor-checked prerequisite (Sysbox, for example) is acceptable only after
reporting it first.

## What QAre needs from a sandbox

From QAre's own docs (`docs/pipeline.md`, ADR-0005 in ViviDynamics/qare):

- `docker compose` v2 and `buildx` (a command-booted app always builds an image).
- The app at `localhost:<port>`: QAre picks a free port, passes it as `QARE_APP_PORT`,
  and its health check, command checks and browser flows all use `localhost`. ADR-0005
  chose host networking for exactly this. So the QAre process must share a network
  namespace with wherever the daemon publishes ports.
- Compose bind mounts name checkout paths, so the daemon must see the checkout at the
  same path the QAre process does.
- Suites run inside the stack through `docker compose -p qare-<run id> exec`.

QAre's ADR-0005 reached the same fork in the road for its own CI pipeline: it rejected
Docker-in-Docker because "the inner daemon needs a privileged container, and privileged is
root on the runner again", and called a rootless daemon "a property of how the runner is
built, not something a workflow can switch on".

## Options

| | Privilege on the default runtime | Host prerequisite | Isolation from host and from coordinare's containers | Linux | macOS Docker Desktop | Kubernetes |
| --- | --- | --- | --- | --- | --- | --- |
| A. `docker:dind` sidecar | `--privileged` (the image's docs require it) | none | none in practice: all capabilities, all devices, no seccomp/AppArmor, so PR code can mount the host disk | works | works, root in the Docker Desktop VM, which holds every container and the shared `/Users` mounts | needs `privileged: true`, rejected by Pod Security baseline |
| B. `docker:dind-rootless` | `--privileged` per Docker's docs, to turn off seccomp, AppArmor and the procfs masks. The unofficial alternative is `seccomp=unconfined`, `apparmor=unconfined`, `systempaths=unconfined`, `/dev/fuse`, `/dev/net/tun` | on Ubuntu 24.04 and later, a host change: `kernel.apparmor_restrict_unprivileged_userns=0` or an AppArmor profile, because unconfined processes may not create user namespaces | better than A (inner root maps to an unprivileged uid) but the default seccomp profile is off, so PR code gets unprivileged user namespaces and the full syscall surface, the usual route for kernel privilege escalation | possible with relaxations plus host change | unverified, not supported by Docker's docs | needs `procMount: Unmasked` and unconfined seccomp; not PSS restricted |
| C. Sysbox (`--runtime=sysbox-runc`) | none: the container runs unprivileged in its own user namespace, dockerd inside it works | install the Sysbox package, add it to `daemon.json`, restart Docker; systemd, kernel 5.12 or later (ID-mapped mounts) | strong: inner root is an unprivileged host uid, procfs/sysfs virtualised, no host socket | supported (supported distro list) | not installable. Docker Desktop's Enhanced Container Isolation uses Sysbox internally but needs a Docker Business subscription and an admin setting | `sysbox-deploy-k8s` DaemonSet and a RuntimeClass; installs CRI-O on nodes |
| D. Rootless dockerd on the host, socket mounted into the job only | none for the job container | install `docker-ce-rootless-extras`, `uidmap`, subuid ranges, a dedicated user, a systemd unit per pool (and the Ubuntu 24.04 AppArmor profile) | good from the host's root, but the job controls a daemon that can bind-mount anything that user can read. Per pool it is shared between jobs; per job it is a daemon lifecycle coordinare would manage on the host | supported | no: Docker Desktop has no second host daemon; it would run in a VM, which is option A or C again | not applicable (per-node daemon) |
| E. gVisor (`--runtime=runsc`), Docker inside it | `--cap-add all` inside gVisor, which grants no host capability | install `runsc`, `runsc install`, restart Docker; Docker-in-gVisor needs extra `runtimeArgs` | strong (user-space kernel) | supported, with I/O overhead on image pulls and builds | not available | GKE Sandbox or a RuntimeClass |
| F. A VM per job (Lima, Firecracker, Kata) | none | a hypervisor and images | strongest | heavy | heavy | Kata RuntimeClass |

Common to all of them:

- **Startup.** dockerd starts in a few seconds. The real cost is a cold image cache: every
  job pulls and builds its compose stack. Caching needs one data root per running daemon
  (two dockerds cannot share `/var/lib/docker`), so a pool of cache volumes leased one per
  concurrent job, or a pull-through registry mirror on the host. Sysbox can keep an inner
  data root on a bind-mounted host directory.
- **Cleanup.** With the daemon inside the job's own container (C, E, B), removing the
  container removes everything it started, and #432's lookup by request id still finds it.
  A, D and a sidecar variant add a second object per job to find and remove after a
  restart, labelled with the same request id.
- **Networking and paths.** One container that runs both QAre and its dockerd keeps
  `localhost:<port>` and the checkout paths true as QAre expects. A sidecar would need
  `--network container:<sidecar>` and the checkout in a shared volume at the same path.

## Gate result

Not cleared. Every option that works on the default runtime without a host install needs
`--privileged` (A, B as documented) or turns off the default seccomp and AppArmor
confinement and, on current Ubuntu, a host sysctl or AppArmor change (B unofficial). Every
option that needs no privilege needs a host prerequisite (C, D, E, F). On macOS Docker
Desktop, which runs coordinare today, nothing without privilege works short of a Docker
Business subscription with Enhanced Container Isolation.

## Recommendation

1. **Docker: Sysbox, as a documented, doctor-checked prerequisite, Linux hosts only.**
   - Opt-in per provider: `stage_providers.<name>.sandbox: sysbox` (default off). Validated
     at load; the stage job then starts with `--runtime=sysbox-runc` and nothing else
     changes. Never `--privileged`, never the host socket, asserted by a test over every
     `docker run` argument list a stage job produces.
   - One container per job running both dockerd and QAre (a QAre sandbox image built FROM
     the full performer image plus `docker-ce`, `containerd`, compose and buildx), so
     `localhost` and checkout paths line up and the existing request-id cleanup covers the
     daemon too.
   - `coordinare doctor` reports whether `docker info` lists `sysbox-runc`; a provider
     configured for the sandbox on a host without it is unavailable with that reason,
     exactly as Kubernetes is today.
   - Image cache: start cold. Add leased cache volumes only if measured startup hurts.
   - macOS Docker Desktop: unsupported (doctor says so) unless the operator has ECI. A Linux
     VM with Sysbox installed is the path for a Mac operator.
   - Kubernetes: left for a follow-up issue (Sysbox RuntimeClass, or user namespace pods
     now GA in Kubernetes 1.36). The provider keeps reporting itself unavailable there.

   Rejected: A (privileged is no better than the socket the decision rules out), B
   (relaxations that disable the kernel hardening that matters most for untrusted code,
   plus a host change on Ubuntu 24.04; its own docs say privileged), D (a host daemon
   lifecycle per pool or job is more coordinare-managed host state than Sysbox's one
   package, and is shared across jobs per pool), E (workable, but heavier on builds and
   less used for Docker workloads than Sysbox; worth keeping as the alternative if Sysbox
   is not acceptable), F (cost).

2. **Browser profiles: split out, no sandbox needed.** QAre drives browsers with its own
   `playwright-core` (1.63.0), not Docker. QAre's published `qare-web` image adds the
   browser family under `/opt/qare/drivers/web` (`playwright-core install --with-deps
   chromium`, Xvfb, GTK 3). The full performer image already installs Python Playwright and a
   Chromium, unpinned, so not necessarily the browser build QAre's `playwright-core` 1.63.0
   looks for, nor where it looks. Copying the qare-web
   driver tree (pinned by digest, like qare-core in #432) into the performer image, or
   adding that one install step, unblocks browser profiles that start their app by
   command or `package.json`, with no privilege and no host change. Browser profiles that
   boot with `docker compose` still need item 1. Suggest a separate issue so it can ship
   now.

## Questions for Jason (answered)

- Accept Sysbox as a required host install (Linux only) for the QAre Docker sandbox, with
  macOS and Kubernetes reported unavailable? Or prefer gVisor, or a different trade?
- Split the browser driver into its own issue and ship it now?

## Decision

Jason, 2026-11-27: Sysbox on Linux hosts, a documented prerequisite that doctor checks. Opt-in per
QAre provider with `sandbox: sysbox`, off by default. Hosts without `sysbox-runc` (macOS Docker
Desktop without Enhanced Container Isolation, and Kubernetes for now) report the provider
unavailable. Kubernetes is #443. Browser-only profiles are #442.

## Sources

- Docker Hub `docker` image docs (dind, dind-rootless require `--privileged`):
  https://github.com/docker-library/docs/blob/master/docker/README.md
- Docker rootless tips (dind-rootless example with `--privileged`):
  https://docs.docker.com/engine/security/rootless/tips/
- Docker rootless troubleshooting (Ubuntu 24.04 AppArmor user namespace restriction):
  https://docs.docker.com/engine/security/rootless/troubleshoot/
- BuildKit rootless (`seccomp=unconfined`, `apparmor=unconfined`, `systempaths=unconfined`):
  https://github.com/moby/buildkit/blob/master/docs/rootless.md
- Sysbox install and distro compatibility:
  https://github.com/nestybox/sysbox/blob/master/docs/user-guide/install-package.md,
  https://github.com/nestybox/sysbox/blob/master/docs/distro-compat.md
- Docker Desktop Enhanced Container Isolation (Sysbox based, Business subscription):
  https://docs.docker.com/enterprise/security/hardened-desktop/enhanced-container-isolation/
- gVisor, Docker in gVisor: https://gvisor.dev/docs/tutorials/docker-in-gvisor
- Kubernetes user namespaces GA in 1.36:
  https://kubernetes.io/blog/2026/04/23/kubernetes-v1-36-userns-ga/
- QAre ADR-0005 and `docs/pipeline.md`, `images/web/Dockerfile` (ViviDynamics/qare).

## Implementation plan

Issue #435

### Scope

In:
- `stage_providers.<name>.sandbox: sysbox` on QAre providers, validated, default off.
- With it on, every container of that provider (the job and its probes) gets
  `--runtime=sysbox-runc` and nothing else. Never `--privileged`, never a Docker socket, no
  `--security-opt`.
- A Docker that does not list `sysbox-runc` (`docker info`) makes the provider unavailable with
  the reason; under Kubernetes it stays unavailable (#443).
- Health checks, in sandbox mode, that the job image's own Docker engine answers.
- Doctor reports whether `docker info` lists `sysbox-runc` for each sandboxed provider.
- A separate image variant, `agent/performer/Dockerfile.sandbox`, built FROM the full image,
  with the Docker engine, containerd, buildx and compose pinned, and an entry point that starts
  dockerd before the job. Built by CI's Docker Images job, not published by the release.
- Docs: `docs/stage-providers.md`, `config.example.yaml`, AGENTS.md.

Out:
- Kubernetes (#443). Browser-only profiles (#442). A shared image cache (start cold; add leased
  cache volumes only if measured startup hurts). Publishing the sandbox image in the release.
- Live proof: a `docker compose` profile passing in the sandbox and a daemon killed mid-job need
  a Linux host with Sysbox. They are run with the stage provider live runs after #376.

### Assumptions

- **Separate image, not `Dockerfile.full`.** The engine adds about 344 MB installed (83 MB
  compressed download), from Docker's trixie index: docker-ce 112.8 MB, containerd.io 86.9 MB,
  docker-buildx-plugin 73.5 MB, docker-ce-cli 37.3 MB, docker-compose-plugin 32.9 MB. In the
  full image every role would pull that, and every performer that runs pull request code
  without a sandbox (the implementer, reviewer, documenter) would carry dockerd, containerd and
  runc it can never use, plus their CVE reports on every image scan. Only an operator who opts
  in builds and pulls the sandbox image. `Dockerfile.full` stays as #432 left it, which also keeps
  this change apart from #442's browser work there.
- The image starts dockerd, not coordinare: coordinare's argument list changes by exactly
  `--runtime=sysbox-runc`. Outside Sysbox, dockerd cannot start; the entry point says so and
  runs the job without Docker, so the image never hides a misconfiguration (health reports it).
- With an egress allowlist, the entry point also limits traffic the inner containers send out
  (the `DOCKER-USER` chain) to the same hosts, or stops dockerd when it cannot: forwarded
  traffic does not pass the `OUTPUT` rules the base entry point sets.
- Cleanup needs nothing new: dockerd and everything it starts live in the job's one container,
  so #432's lookup by request id (`docker rm -f <job name>`) removes the inner Docker too. The
  inner data root is not a host volume.

### Tasks

- [x] 1. Config: `sandbox` validated, default None: `test_435_*::test_sandbox_*config*`.
- [x] 2. `DockerStageJobs(runtime=...)` adds exactly `--runtime=sysbox-runc` to the job and probes,
  and refuses with the reason when `docker info` does not list it.
- [x] 3. A test over every docker invocation a QAre provider makes, sandbox off and on: no Docker
  socket, no `--privileged`, no `--security-opt`, no capability but the egress `NET_ADMIN`.
- [x] 4. Orphan cleanup after a restart, by request id, removes the one container.
- [x] 5. Health in sandbox mode checks the inner engine; doctor checks `sysbox-runc`.
- [x] 6. Off means #432 unchanged: the argument lists are identical, no #432 test modified.
- [x] 7. `Dockerfile.sandbox` and `sandbox-entrypoint.sh`, with entry point tests on fake
  `dockerd`, `docker` and `iptables`; CI builds the image and reports its size.
- [x] 8. Docs.
