# Make the egress allowlist binding for every Docker performer

Issue #449. Generalises #447 / PR #448 (the QAre sandbox) to every Docker performer.

Date: 2026-11-27
Status: design gate cleared (below); built in this branch. Live proof stays open on #449.

## What exists

- `egress_allowlist` on a performer endpoint becomes `--cap-add NET_ADMIN` and
  `PERFORMER_EGRESS_ALLOWLIST` on two kinds of container: ephemeral performers
  (`performer_lifecycle.start_ephemeral`, every role) and stage-provider job containers
  without the sandbox (`DockerStageJobs._isolation`). `agent/performer/entrypoint.sh` turns it
  into iptables `OUTPUT` rules. The container keeps `NET_ADMIN` and everything in it runs as
  root, so pull request code can run `iptables -P OUTPUT ACCEPT` and the allowlist is gone.
- PR #448 fixed only the sandboxed QAre path: an `--internal` network per job, an allowlisting
  proxy container (`performer.egress_proxy`) as its only exit, fail closed. #435's live run
  proved it on Docker 29.10.1: root in the job could not reach `1.1.1.1` after
  `iptables -P OUTPUT ACCEPT; iptables -F`, outside names did not resolve, host services were
  unreachable, and allowlisted hosts were reached through the proxy.

## Which containers this changes

| Container | Started by | Today | After |
| --- | --- | --- | --- |
| Ephemeral performer (implementer, reviewer, qa, documenter, architect, assessor, security, closer, env bootstrap, and the rest) | coordinare, `start_ephemeral` | iptables in the container, `NET_ADMIN` | internal network, proxy, no `NET_ADMIN` |
| Stage-provider job, no sandbox (QAre, Progettare) | coordinare, `DockerStageJobs` | iptables in the container, `NET_ADMIN` | internal network, proxy, no `NET_ADMIN` |
| Stage-provider job, `sandbox: sysbox` | coordinare | proxy (#448) | unchanged |
| Persistent performer (`mode: persistent`) | the operator | `egress_allowlist` never applied: coordinare does not start the container | unchanged, now documented |
| Kubernetes performer Pod | coordinare | `egress_allowlist` not applied; the Helm chart has an opt-in CIDR NetworkPolicy | unchanged; follow-up filed (below) |

Endpoints without `egress_allowlist` get exactly today's `docker run` arguments.

## Design

The #448 topology, for one performer:

1. **Network.** `docker network create --internal -o com.docker.network.bridge.inhibit_ipv4=true
   coordinare-egress-<id>`: no route out, no host address on its bridge.
2. **Proxy.** A container `coordinare-egress-<id>` of the endpoint's image, `--entrypoint python
   -m performer.egress_proxy --allow <hosts>`, `--user 65534:65534 --read-only`, the endpoint's
   `extra_hosts`, on the default bridge (its route out), then `docker network connect` to the
   performer's network. It also publishes the performer's port (below).
3. **Performer.** `docker run --network coordinare-egress-<id> --network-alias performer
   -e PERFORMER_EGRESS_PROXY=http://<proxy address>:3128`, no `-p`, no `NET_ADMIN`, no
   `PERFORMER_EGRESS_ALLOWLIST`.
4. **Fail closed.** If any step fails, coordinare removes what it made and raises the same
   error a failed `docker run` raises today (`ContainerStartError` for a performer,
   `TransportError` for a stage job). No performer starts with a route out the allowlist does
   not cover.

### How each destination is reached

| Destination | Path |
| --- | --- |
| Model gateway (LiteLLM, or any OpenAI or Anthropic compatible endpoint) | Through the proxy: a plain `http://llm:4000` request in absolute form, or `CONNECT` for `https`. The gateway's host must be in the allowlist, as it must today. The proxy gets the endpoint's `extra_hosts`, so a name like `llm` resolves the same way it did in the performer. The in-performer shims (`ClaudeCodeShim`, `SelfHostedShim`, `DualModelProxy`) use httpx, which reads the proxy variables; agent CLIs reach the shims on loopback, which `NO_PROXY` keeps direct. |
| GitHub API | Through the proxy with `CONNECT`: the performer's GitHub calls are httpx with default settings. |
| git | Over HTTPS with the token header (`workspace._git_credential_env`, `stage_checkout.git_env`), through the proxy; git reads `https_proxy`. git over SSH is not supported in this mode (below). |
| Package registries (npm, pip, uv, apt, gem, go, cargo) | Through the proxy; all of these read the proxy variables. The registry hosts must be in the allowlist, as today. |
| The backend CLI install at container start (`entrypoint.sh`, for example `claude.ai`, `opencode.ai`, npm) | Through the proxy: the entry point sets the proxy variables before the install runs. |
| The coordinare daemon | The performer never connects to the daemon. The HTTP transport is the daemon polling the performer (next section). |

### The daemon reaching the performer

Docker does not publish ports of a container whose only network is internal, and with
`inhibit_ipv4` the host has no address on that network either. So the proxy container, which
is on the default bridge, publishes the port instead: `-p <bind_host>:<port or 0>:8088`, and
`performer.egress_proxy --forward 8088=performer:8088` relays each connection to the
performer's alias on the internal network. `start_ephemeral` reads the host port with
`docker port <proxy> 8088/tcp` and returns the same `http://<connect_host>:<port>` endpoint
as today. The relay goes to one fixed address, the performer itself, so it opens no way out.

A restarted daemon adopts running performers by `docker port <container>` (`DockerExecutor.port_of`,
076 reconciliation). The performer carries a label naming its proxy, and `port_of` follows it
when the container has no published port of its own.

### Tools that ignore proxy variables

The internal network has no route and Docker's embedded resolver answers no outside names
there (#435 evidence, Docker 29.10.1). A tool that ignores the proxy variables therefore
fails to connect. It does not leak. What that means per tool:

| Tool | Behaviour | Decision |
| --- | --- | --- |
| git over SSH, `ssh`, `scp` | ignores proxy variables; fails | Unsupported. coordinare clones over HTTPS. A tool that needs SSH can use the proxy with a `ProxyCommand` that speaks `CONNECT` (the proxy allows any port of an allowlisted host). |
| Node's built-in `fetch` and `http` (pi, openclaw, scripts) | ignore proxy variables unless `NODE_USE_ENV_PROXY=1`, which Node 22.21 and later honour | The entry point sets `NODE_USE_ENV_PROXY=1`. The image installs the current Node 22 LTS. A project's pinned older Node (from the environment cache) ignores it and fails closed. |
| Java, Maven, Gradle | ignore proxy variables | Fail closed. Configure the proxy in the build's own settings, or use `egress_mode: iptables` (below). |
| aiohttp clients with default settings | ignore proxy variables (`trust_env=False`) | The only one in the performer is the codex backend's websocket to its local app server, on loopback. |
| Raw TCP to an allowlisted host (a database, SMTP) | no route | Fail closed unless the client speaks `CONNECT`. Use `egress_mode: iptables` if a job needs it. |
| Docker in Docker | non-sandboxed performers have no Docker engine | Not applicable. The sandbox keeps #448's behaviour. |
| DNS lookups of outside names | not answered | Not needed: the proxy resolves names. Removes the DNS channel the iptables mode leaves open. |

### What happens to the in-container iptables code and `NET_ADMIN`

coordinare no longer adds `NET_ADMIN` or `PERFORMER_EGRESS_ALLOWLIST` in the default mode.
The entry point's iptables block stays, unchanged, for containers coordinare does not start
(an operator's persistent performer, a manual `docker run`) and for the explicit opt-out
below. It is documented as advisory, as its own comment already says. It cannot be made
binding inside the container: root there with `NET_ADMIN` can always undo it.

`entrypoint.sh` gains one block, before the backend install: with `PERFORMER_EGRESS_PROXY`, wait
up to 60 seconds for the proxy to answer and exit 1 if it does not (the performer never
becomes ready, and the dispatch fails and is retried as for any start failure), then export
`HTTP_PROXY`, `HTTPS_PROXY` (both cases), `NO_PROXY=localhost,127.0.0.1,::1` and
`NODE_USE_ENV_PROXY=1`. These override any proxy variable the operator passed, because an
upstream proxy is not supported in this mode (as in #448). It refuses
`PERFORMER_EGRESS_ALLOWLIST` and `PERFORMER_EGRESS_PROXY` together.

### Cleanup

- `stop()` reads the performer's proxy label, stops the performer, then removes the proxy and
  the network.
- Performers are also stopped by paths that bypass `stop()` (reconciliation's reaper, 218's
  `confirm_stopped`, a daemon crash). So the proxy runs with `--follow performer`: it exits once
  the performer's alias, having resolved, stops resolving (the performer is gone), or never
  resolves within ten minutes. Its container is `--rm`.
- Networks: each proxied start and `cleanup_orphaned` sweep labelled networks older than ten
  minutes with `docker network rm`, which Docker refuses for a network that still has a
  container on it, so a live performer's network is never removed. The grace protects a
  network made by a start running at the same time. `cleanup_orphaned` also removes proxies
  whose performer it stopped.
- Proxies do not carry `coordinare.performer.id`, so reconciliation never mistakes one for a
  performer.

Non-sandboxed stage jobs use the same proxy as #448 (`DockerStageJobs`, now proxied for any
allowlist), with `--follow <job name>` in place of the sandbox entry point's heartbeat, since
the base entry point has none. The sandboxed path keeps its arguments exactly.

### Rollout

The proxy is the default for every endpoint with `egress_allowlist`, from this release. The
issue asks for the allowlist to be binding, and the old mode never was. For a config that
depends on what only the old mode allowed (raw TCP to an allowlisted host, a tool that ignores
proxy variables), a new endpoint field `egress_mode: iptables` restores today's behaviour exactly,
`NET_ADMIN` included, and is documented as advisory. `egress_mode` defaults to `proxy`; it has no
effect without `egress_allowlist`, and a sandboxed QAre provider always uses the proxy (the only
mode that covers its inner Docker). A config flag that kept the old default was considered and
rejected: it would leave every existing allowlist advisory until someone flipped it, and the
opt-out gives the same rollback without that.

Each running performer with an allowlist holds one Docker network, so Docker's address pools
(`default-address-pools`) bound how many run at once; the docs say so.

### Kubernetes

`egress_allowlist` is not applied on the Kubernetes transport (`kubernetes_runtime.py` says so).
The Helm chart's opt-in NetworkPolicy (`performers.egress`) takes CIDRs, not hostnames, and only
works on a CNI that enforces egress (`coordinare doctor --check egress` measures it). NetworkPolicy
does not enforce the allowlist there. Follow-up filed: a proxy Pod per namespace plus a
NetworkPolicy that lets performer Pods reach only that proxy and DNS.

### What it does not cover

- The proxy's known limits from #448 hold here too: no upstream corporate proxy; other
  containers on the default bridge can reach the proxy (only the allowlisted hosts); inside a
  `CONNECT` tunnel the proxy cannot see TLS SNI, so a shared front end serving an allowed name
  could be asked for another site behind it.
- Persistent performers: the operator starts them, so coordinare cannot put them on a network.
  An operator can run the same topology by hand: the image's entry point honours
  `PERFORMER_EGRESS_PROXY`.

## Tasks

- [ ] 1. `performer.egress_proxy --forward` relays a port to a fixed address and `--follow` exits once a
  name stops resolving (`agent/performer/tests/unit/test_egress_proxy.py`, real sockets).
- [ ] 2. `entrypoint.sh` with `PERFORMER_EGRESS_PROXY`: waits for the proxy before the backend install,
  refuses to start when it does not answer, exports the proxy variables, refuses both modes
  together (`tests/unit/test_performer_entrypoint.py`).
- [ ] 3. `start_ephemeral` with an allowlist: network, proxy, connect, address, performer, port, in that
  order; root in the performer cannot reach a host outside the allowlist even after
  `iptables -F` (network model over the docker arguments); no `NET_ADMIN`; setup failures start
  no performer and tear down; no allowlist gives the same arguments as before
  (`tests/unit/test_449_performer_egress.py`).
- [ ] 4. `stop`, `cleanup_orphaned` and `port_of` follow the proxy (same file).
- [ ] 5. `DockerStageJobs` without the sandbox uses the proxy; `egress_mode: iptables` keeps the old
  arguments (same file; one assertion in `test_423_providers_in_performer.py` changes).
- [ ] 6. Docs: `docs/stage-providers.md`, `config.example.yaml`, `deploy/kubernetes/README.md`, AGENTS.md.
- [ ] 7. Live proof on a disposable Linux VM, commands posted on #449.
