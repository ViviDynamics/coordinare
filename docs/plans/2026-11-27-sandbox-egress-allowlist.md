# Egress allowlist inside the QAre Sysbox sandbox

Issue #447. Follows #435 and PR #446.

Date: 2026-11-27
Status: design gate cleared (below); built in this branch. Live proof joins #435's open
acceptance criteria.

## What exists

- `egress_allowlist` on a performer endpoint becomes `--cap-add NET_ADMIN` and
  `PERFORMER_EGRESS_ALLOWLIST` on the container. `agent/performer/entrypoint.sh` resolves each
  host once and sets the `OUTPUT` chain to DROP, with ACCEPT for loopback, established
  traffic, port 53 to anywhere, and each resolved address.
- It is advisory already, sandbox or not. The container keeps `NET_ADMIN` for its whole life
  and every process in it runs as root (no `USER`, no capability drop after the rules go in),
  so any code the performer runs, pull request code included, can run
  `iptables -P OUTPUT ACCEPT`. The entry point says so itself: "a defense-in-depth baseline,
  NOT a compliance boundary". Fixing that for performers other than the QAre sandbox is out
  of scope here.
- PR #446 refuses the sandbox with an allowlist: at config time (`stage_jobs_for`), in
  `DockerStageJobs._require_runtime`, and in `sandbox-entrypoint.sh`.

## How traffic leaves the sandbox

The job container has one interface, `eth0`, on whatever Docker network the host's Docker
put it on. Its own processes (the stage job, QAre, git) send from the container's network
namespace through `OUTPUT`. The inner dockerd makes a bridge (`docker0`) in that same
namespace; an inner container's packets arrive on `docker0`, cross `FORWARD`, and are
masqueraded out of `eth0` (inner dockerd's `POSTROUTING` NAT). So every packet from the
sandbox, inner or not, leaves through the job container's `eth0` and then the host's
bridge for that network.

## Where enforcement can sit

| Option | Covers inner containers | Can code in the sandbox turn it off |
| --- | --- | --- |
| 1. iptables or nftables in the job container's namespace, set by the entry point before dockerd (`OUTPUT` plus `FORWARD`/`DOCKER-USER`) | yes, inner traffic is forwarded through it | **yes.** Under Sysbox, root in the container holds every capability over its own network namespace (it must, for dockerd to make bridges and NAT rules). QAre runs as root, so does pull request code it runs, and anything with the inner Docker can start `docker run --privileged --network host` and get the same namespace. Dropping root for QAre does not help: access to the inner Docker is root in the sandbox. |
| 2. Inner dockerd configured so inner containers reach only an allowlisting proxy | only for containers started the configured way | **yes.** `daemon.json`, the inner bridge and the inner iptables all belong to root in the sandbox; `--network host` and `--privileged` bypass them. |
| 3. Outside the job container: a Docker network with no route out, and an allowlisting proxy in a separate container as its only exit | yes, inner traffic is NATed out of the same `eth0` | **no.** The route out, the host's forwarding rules and the proxy all live in the host's network namespace or in another container. The sandbox's root is an unprivileged user on the host (Sysbox's user namespace) with no capability there, and has no host Docker socket. |

Only option 3 holds against code inside the sandbox. It is the design.

## Design

Only for a QAre provider with `sandbox: sysbox` whose runs take an endpoint with
`egress_allowlist`. Everything else is unchanged from #446 and #432, including the iptables
baseline for unsandboxed runs.

Per job, with the job container's name `<job>`:

1. **Network.** `docker network create --internal -o com.docker.network.bridge.inhibit_ipv4=true
   <job>-egress`. Docker gives an internal network no gateway route out: the host drops
   traffic from its bridge to any other interface and adds no masquerade. Without a host
   address on the bridge, the job cannot open connections to host services through a gateway
   address either (`--internal` stops forwarding, not traffic to the host itself). That is
   host state the sandbox cannot touch.
2. **Proxy.** A container named `<job>-egress` of the same image, `--entrypoint python`,
   running `performer.egress_proxy` on the default bridge (so it has a route out), then
   `docker network connect <job>-egress <job>-egress`. It runs as `nobody` on a read-only
   root, with the endpoint's `extra_hosts` (so a model host such as `llm` resolves the same
   way it does for the job today). It allows `CONNECT host:port` and plain `http://`
   requests only to hosts in the allowlist (exact names, case and trailing dot ignored, any
   port, as the iptables rules allowed any port), and answers everything else with 403
   without opening a connection. It resolves names itself, at connect time, so rotating
   addresses keep working. A plain request goes upstream with `Connection: close` and the
   client connection ends with the response, so a keep-alive client cannot send a second
   request, for another host, down a connection already relayed. It exits after three
   minutes with no connection (`--idle-exit 180`).
3. **Job.** `docker run --runtime=sysbox-runc --network <job>-egress
   -e PERFORMER_EGRESS_PROXY=http://<proxy address>:3128 ...`. It is attached to no other
   network and gets no `NET_ADMIN` and no `PERFORMER_EGRESS_ALLOWLIST`.
4. **Entry point.** `sandbox-entrypoint.sh`, given `PERFORMER_EGRESS_PROXY`, waits for the
   proxy to answer **before it starts dockerd**, and exits (the run is retried) when it
   does not. It sets `HTTP(S)_PROXY` to it and `NO_PROXY` to loopback, so the stage job,
   QAre, git, gh and model calls go through the proxy, the app on `localhost` does not, and
   dockerd pulls images through it. While the job runs it connects to the proxy every 30
   seconds, so the proxy stays up exactly as long as the job. It still refuses
   `PERFORMER_EGRESS_ALLOWLIST`: that is the in-container mode, which cannot cover the
   sandbox.
5. **Probes** (the version and engine checks) need no network and run with
   `--network none`. The engine check also imports `performer.egress_proxy`, so health
   reports an image built before this release.
6. **Failure.** If the network, the proxy, the connect, or reading the proxy's address
   fails, coordinare removes what it made and raises a retryable error; the job container
   is never started. A job never runs with an unrestricted route.
7. **Cleanup.** The proxy exits on its own once the job's heartbeats stop. `stop()` and a
   `state()` that finds the job gone remove `<job>-egress` (container, then network). A restarted daemon, or a job that ended between polls, is
   covered by a sweep at each sandboxed start: any `-egress` network labelled by coordinare
   whose job container is gone and that is older than ten minutes is removed with its
   proxy. The grace keeps a concurrent start from losing its proxy between creating it and
   starting its job.

Inner containers get no proxy settings by default. Compose services talk to each other by
service name on the inner bridge, which a proxy variable would break; with the allowlist on,
an inner container's only way out is to name the proxy itself (its address is
`PERFORMER_EGRESS_PROXY` in the job's environment, available to compose interpolation as a
build argument or variable). A build that downloads packages therefore needs the profile to
pass the proxy, and the hosts in the allowlist. Image pulls work through dockerd.

### What it does not cover

- An upstream proxy. The egress proxy connects to allowlisted hosts directly; a Docker host
  that reaches the internet only through a corporate proxy is not supported in this mode.
- Other containers on the default bridge can reach the proxy (it listens there too, for its
  route out). That gives them the allowlisted hosts only, which they can already reach.
- Docker address pools: each running sandboxed run with an allowlist holds one network.

- DNS. The job container uses Docker's embedded resolver on its network. Whether that
  resolver forwards outside names for a container on an internal network depends on the
  Docker version; nothing here needs it to, because the proxy resolves names. If it does
  forward, a DNS query channel remains, as it does today (port 53 is open to anywhere).
- Hosts on the internal network's own subnet: only the proxy and the job are on it.
- The proxy's own correctness: it is the enforcement point, kept small (standard library
  only) and covered by unit tests over real sockets.

## Tasks

- [x] 1. `performer.egress_proxy`: refuses a host outside the allowlist without connecting,
  relays an allowed `CONNECT` and an allowed plain `http://` request
  (`agent/performer/tests/unit/test_egress_proxy.py`).
- [x] 2. `DockerStageJobs` with a runtime and an allowlist: network, proxy, then job, in that
  order; the job only on the internal network, with the proxy address and no `NET_ADMIN`;
  probes with `--network none`; setup failure starts no job; cleanup and sweep
  (`tests/unit/test_447_sandbox_egress.py`).
- [x] 3. Remove #446's refusal in `stage_jobs_for` and `_require_runtime`; #446's assertions
  about the refusal change (listed in the PR).
- [x] 4. `sandbox-entrypoint.sh`: waits for the proxy before dockerd, refuses to start when it
  does not answer, sets the proxy variables.
- [x] 5. Docs: `docs/stage-providers.md` says what is enforced where.
- [ ] 6. Live proof on a Linux host with Sysbox, added to #435's open criteria: an inner
  container is refused a host outside the allowlist and reaches an allowed one.
