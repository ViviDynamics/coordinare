# Egress allowlist for Docker performers

A performer endpoint's `egress_allowlist` limits where its performers can connect. Since #449
it is enforced outside the performer's container for every Docker performer coordinare starts:
ephemeral performers of every role, and stage-provider jobs (QAre, Progettare) with or without
the [QAre sandbox](stage-providers.md#docker-sandbox). Code in the performer, pull request code
included, runs as root, and that does not matter: it cannot change the enforcement.

```yaml
performer_endpoints:
  - id: workers
    mode: ephemeral
    roles: [implementer, reviewer, qa, documenter]
    image: coordinare-performer:full
    extra_hosts: ["llm:192.0.2.10"]      # how the model gateway's name resolves
    egress_allowlist:
      - llm                              # the model gateway, by the name the performer uses
      - api.github.com
      - github.com
      - codeload.github.com
      - objects.githubusercontent.com
      - registry.npmjs.org
      - pypi.org
      - files.pythonhosted.org
    # egress_mode: proxy                 # the default; `iptables` is the old advisory mode
```

Endpoints without `egress_allowlist` are unchanged: no network, no proxy.

## How it works

For each performer, coordinare runs:

1. A Docker network of its own, `docker network create --internal -o
   com.docker.network.bridge.inhibit_ipv4=true coordinare-egress-<id>`. It has no route out, and
   no host address on its bridge, so host services are not reachable through a gateway address.
2. A proxy container, `coordinare-egress-<id>`, from the endpoint's image:
   `performer.egress_proxy`, as `nobody` on a read-only root, with the endpoint's `extra_hosts`. It
   is on the default bridge (its route out) and on the performer's network. It relays
   `CONNECT host:port` and plain `http://` requests only to the allowlisted hosts (exact names,
   any port, resolved when the connection is made) and answers anything else with 403 without
   connecting.
3. The performer, on that network only, with `PERFORMER_EGRESS_PROXY` naming the proxy. It gets
   no `NET_ADMIN` and no published port. Its entry point waits for the proxy, then sets
   `HTTP_PROXY`, `HTTPS_PROXY` (both cases), `NO_PROXY=localhost,127.0.0.1,::1` and
   `NODE_USE_ENV_PROXY=1` before anything else runs, the backend CLI install included. These
   replace any proxy variables the endpoint's `env` sets: an upstream proxy is not supported in
   this mode.

Docker publishes no port of a container whose only network is internal, so the proxy publishes
the performer's port (`bind_host`, `port`) and relays each connection to the performer. The
daemon reaches the performer at the same `http://<connect_host>:<port>` as before. The relay goes
to one fixed address, the performer itself, so it is no way out. A restarted daemon finds a
running performer's port on its proxy.

If the network or the proxy cannot be set up, coordinare removes what it made and the start
fails like any failed `docker run` (the dispatch is retried). If the proxy does not answer within
60 seconds, the performer's entry point exits, so the performer never becomes ready. A performer
never runs with a route out that the allowlist does not cover.

| Where | What | Can code in the performer change it |
| --- | --- | --- |
| The host's Docker | The internal network: no route out, no host address. | No: host state. |
| The proxy container | The allowlist. | No: another container. |
| The performer | Proxy variables, so clients use the proxy. | Yes, but without the proxy there is no route at all. |

## What reaches what

| Destination | How |
| --- | --- |
| Model gateway (LiteLLM, or any OpenAI or Anthropic compatible endpoint) | Through the proxy. List the host the performer uses (for example `llm`, with `extra_hosts`). Plain `http://` works; the proxy sends each request upstream with `Connection: close` and the checked `Host`. |
| GitHub API, git over HTTPS | Through the proxy with `CONNECT`. |
| npm, pip, uv, apt, gem, go, cargo | Through the proxy; they read the proxy variables. List the registry hosts and their CDNs. |
| Backend CLI installs at container start | Through the proxy, for example `claude.ai`, `opencode.ai` or `app.primeintellect.ai`. |
| The coordinare daemon | Never: the daemon polls the performer, not the other way round. |
| Anything else | Refused with 403. |

## Tools that ignore proxy variables

The performer's network has no route and resolves no outside names, so a tool that ignores the
proxy variables fails to connect. It fails closed. It does not leak.

| Tool | What happens |
| --- | --- |
| git over SSH, `ssh`, `scp` | Fails. coordinare clones over HTTPS. A `ProxyCommand` that speaks `CONNECT` reaches an allowlisted host on any port. |
| Node's own `fetch` and `http` | Use the proxy through `NODE_USE_ENV_PROXY=1` on Node 22.21 and later (the image's Node). A project's pinned older Node ignores it and fails. |
| Java, Maven, Gradle | Fail unless the build's own settings name the proxy. |
| Raw TCP to an allowlisted host (a database, SMTP) | Fails unless the client speaks `CONNECT`. |
| DNS lookups of outside names | Not answered; the proxy resolves names. The DNS channel the iptables mode leaves open is gone. |

When a job needs one of these, set `egress_mode: iptables` on the endpoint (below).

## `egress_mode: iptables`

Restores the old behaviour exactly: `--cap-add NET_ADMIN` and `PERFORMER_EGRESS_ALLOWLIST`, which
the entry point turns into iptables `OUTPUT` rules in the container. It is advisory. Code in the
container runs as root with `NET_ADMIN` and can remove the rules, DNS stays open, and addresses are
resolved once at start. Use it only as a deliberate trade. A QAre provider with `sandbox: sysbox`
always uses the proxy, because the in-container mode cannot cover the sandbox's own Docker.

## Operating it

- **Images.** The proxy runs from the endpoint's image, which needs `performer.egress_proxy`
  (every performer image built since #448). An older image makes every start fail at "attach its
  proxy"; rebuild it.
- **Address pools.** Each running performer with an allowlist holds one Docker network, so
  Docker's `default-address-pools` bounds how many run at once. Size it for your concurrency.
- **Cleanup.** When coordinare stops a performer it removes the proxy and network with it. The
  proxy also follows its performer and exits once the performer is gone, however it was stopped;
  it is a `--rm` container. Each proxied start removes egress networks older than ten
  minutes that nothing is attached to (Docker refuses to remove one still in use).
- **Labels.** The performer, its network and its proxy carry `coordinare.performer.egress=<name>`;
  the network and proxy also carry `coordinare.performer.egress.for=<endpoint id>`. The proxy does
  not carry `coordinare.performer.id`, so it is never taken for a performer.
- **Limits.** Inside a `CONNECT` tunnel the proxy sees only the host it was asked for, not TLS SNI,
  so a shared front end serving an allowed name (a CDN) could be asked for another site behind it.
  Other containers on the default bridge can reach the proxy, which gives them only the allowlisted
  hosts.

## Where it does not apply

- **Persistent performers** (`mode: persistent`): the operator starts the container, so coordinare
  never applied `egress_allowlist` to them. To get the same enforcement, run the topology above
  yourself; the image's entry point honours `PERFORMER_EGRESS_PROXY`.
- **Kubernetes**: `egress_allowlist` is not applied. The Helm chart's opt-in NetworkPolicy
  (`performers.egress`) takes CIDRs, not hostnames, and needs a CNI that enforces egress
  (`coordinare doctor --check egress`). Tracked in #454.

Design: `docs/plans/2026-11-27-egress-allowlist-all-performers.md`.
