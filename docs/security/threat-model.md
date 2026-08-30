# Coordinare Threat Model

**Status**: current as of 2026-08-27 | **Spec**: 144 | **Issue**: [#198](https://github.com/ViviDynamics/coordinare/issues/198)

> **Read this before exposing coordinare to any network.**

This document says what coordinare trusts, what it defends against, and what it leaves exposed.
The last part matters most. Every trust boundary below states its **residual risk**, because a
security document that lists only mitigations is one a reader stops believing the moment they find
the first gap themselves.

## What coordinare is, from a security standpoint

Four properties define the risk, and they compound:

1. It **executes AI-generated code** in containers.
2. It holds a **GitHub token with write access** to your repository.
3. It feeds **public, attacker-authored text** (issue bodies, pull request comments, review
   threads) into model prompts that drive that code and that token.
4. Its daemon container mounts **`docker.sock`**.

Any one of those is ordinary. Together they mean a successful attack on coordinare is an attack on
your repository and, via `docker.sock`, on the host running it.

## Trust boundaries

### Operator host to daemon container

**What crosses**: the daemon runs with a mounted `docker.sock` so it can start and stop performer
containers.

**Trusted assumption**: the operator intends coordinare to control containers on their host.

**Mitigations**: none, and that is the honest word for it. This is an explicit trust decision, not
a defended boundary.

**Residual risk**: mounting `docker.sock` is **root-equivalent** access to the host. Any code
execution inside the daemon container can start a privileged container, mount the host filesystem,
and take over the machine. Coordinare does not sandbox itself away from this and cannot: launching
containers is what it does. Run the daemon on a host you are willing to lose, or on one dedicated
to it. Do not run it on a workstation holding credentials for anything else you care about.

### Operator host to daemon container, on Kubernetes

**What crosses**: nothing. This is the boundary's absence, and it is spec 146's
headline result.

**Trusted assumption**: the daemon may create and delete Pods in **one namespace**.

**Mitigations**: the daemon runs under a namespace-scoped ServiceAccount granted
`pods` and `pods/log` only — no ClusterRole, no access to secrets, no Docker
socket. The root-equivalent trust decision described above **does not apply on
this path**. See [deploy/kubernetes/README.md](../../deploy/kubernetes/README.md).

**Residual risk**: anyone who can create Pods in that namespace can run containers
there, so the namespace is a trust boundary and should not be shared with
unrelated workloads. The grant is also only as small as the cluster makes it: a
Role bound to the same ServiceAccount under a different name keeps granting
whatever it lists, and reading `rbac.yaml` will not show it — verify with
`kubectl auth can-i --list`, which is what coordinare's own integration test does.
Performer Pods set `automountServiceAccountToken: false`, so the AI-generated code
inside them holds no credential for the cluster API. Egress allowlisting is **opt-in and unverified by default** on Kubernetes (`performers.egress.enabled`, off unless you turn it on), and is CIDR-level rather than hostname-level: the
Docker path's in-container iptables needs `NET_ADMIN`, and the native NetworkPolicy
equivalent only applies if the cluster's CNI enforces it, which several common
distributions do not by default. Nothing silently substitutes for it. Support is often *partial*: measured on kind, kindnet enforces ingress but not egress, so a policy there is inert in the direction that matters. `coordinare doctor --check egress` measures enforcement rather than inferring it, and until it reports enforcement a performer on Kubernetes can reach whatever the cluster's network permits.

### Daemon to performer containers

**What crosses**: task payloads out, model-authored code and structured results back. Performers
receive repository credentials scoped to the work they do.

**Trusted assumption**: the performer container is a boundary against *accidents*, not against a
determined attacker who already controls the model's output.

**Mitigations**: performers are ephemeral, one per task, and are discarded afterwards. Spec 131
blocks agents from committing their own tool-configuration directories, closing one route by which
a compromised performer could persist instructions into the repository.

**Residual risk**: a performer that runs hostile code can reach anything its container can reach,
including the network and the credentials it was given for its task. Container escape is not
defended against beyond what the container runtime provides. Treat a performer as a machine you
have handed a scoped credential to and expect it to use.

### Performers to public GitHub content

**What crosses**: issue bodies, pull request descriptions, review comments, and commit messages
flow into model prompts. **All of it is attacker-authored** on a public repository: anyone can
open an issue.

**Trusted assumption**: none. This content is hostile by default.

**Mitigations**: see the prompt-injection section below, which is the substance of this boundary.

**Residual risk**: covered in detail below. Summarised: prompt injection is mitigated
structurally, not prevented.

### Coordinare to model endpoints

**What crosses**: prompts out, generated code and structured verdicts back.

**Trusted assumption**: the endpoint is one the operator chose and controls or trusts. Coordinare
does not verify model identity beyond the transport.

**Mitigations**: performer output is treated as untrusted input. It is scanned by the spec-083
security gate (semgrep plus bandit over changed files) before a pull request can advance, and no
model verdict alone merges anything.

**Residual risk**: the security scan gate catches **known patterns in changed files**. It does not
catch logic that is malicious but idiomatic, a subtle backdoor expressed in ordinary-looking code,
or anything in a file the change did not touch. A compromised or hostile model endpoint sees every
prompt, which includes your repository's code. Point coordinare at endpoints you would be
comfortable pasting your source into, because that is what you are doing.

## Prompt injection: the defining threat

This is the threat class coordinare exists inside, and it has no clean solution.

**The attack**: someone opens an issue on your public repository whose text instructs the model to
do something other than the task. That text reaches a performer holding repository write
credentials. There is no reliable way to make a language model distinguish instructions from data
when both arrive as text.

**What actually constrains it**, none of which is prevention:

| Mitigation | What it stops |
|---|---|
| Reviewer verdicts are **binary** | A hijacked reviewer cannot write arbitrary approval prose that a later stage acts on. |
| **Only humans approve** pull requests | Injected instructions cannot cause a merge on their own. This is the load-bearing one. |
| Spec 131 agent-config blocking | Stops a performer persisting instructions into the repository for the next run to read. |
| Spec 083 security scan gate | Catches known-bad patterns in changed files before a pull request advances. |
| Spec 142 closed contributions | No external pull requests, so the supply chain has no public write path. |
| Branch protection | `main` requires passing checks; a performer cannot push to it directly. |

**Residual risk**, stated plainly: an injected prompt can cause a performer to write code that
does something you did not ask for, open a pull request containing it, and describe it
misleadingly. Every automated gate above can be satisfied by code that is malicious but passes a
pattern scan. **The human reviewing the pull request is the last line of defence and, for this
threat class, the only reliable one.** Read the diff, not the summary. Be especially careful when
a card originated from an issue you did not write.

Coordinare does not attempt to detect prompt injection in incoming text. Such detection is
unreliable, and a filter that works most of the time mainly moves the risk somewhere less visible.

## The dashboard

The dashboard is **unauthenticated by design**. There is no login, and every visitor who can reach
it has full control: rewriting configuration, cancelling work, deleting symphonies.

**Why**: it is a single-operator control plane bound to **loopback** (`dashboard_host` defaults to
`127.0.0.1`). Adding authentication is tracked as **spec 143**
([#197](https://github.com/ViviDynamics/coordinare/issues/197)).

**What defends it today** (spec 144, this document's own work): a localhost guard rejects any
request whose `Host` is not local, and any state-changing request whose `Origin` is not local.
Those are two different defences:

- The **`Origin`** check on mutating methods stops **cross-site request forgery**: a page in your
  browser trying to drive the dashboard behind your back. In that attack the `Host` genuinely is
  `localhost`, so only `Origin` reveals the page is foreign.
- The **`Host`** check on *every* request, reads included, stops **DNS rebinding**, where an
  attacker's domain resolves to `127.0.0.1` so your browser believes their page shares an origin
  with the dashboard. Browsers send no `Origin` on a same-origin GET, so the origin check is
  structurally blind to it and read endpoints such as `GET /api/config/global` would otherwise
  leak your configuration.

A request with **no** `Origin` is allowed: browsers always send one on non-GET requests, so its
absence means the caller is not a browser, and forgery requires a browser. Scripts and probes keep
working; the `Host` check still applies to them.

**Residual risk**: the guard is a locality check, not authentication. **Anyone who can make
requests from your machine, or from any host you have added to `trusted_dashboard_hosts`, has full
control.** That includes any other process on your machine and anyone with a shell on it. If you
bind `dashboard_host` beyond loopback, you have put an unauthenticated control plane on a network,
and coordinare warns loudly at startup when you do. It cannot stop you.

## Health endpoints

`/health`, `/live`, `/ready`, and `/metrics` are served on a **separate port** that binds
`0.0.0.0` by default (`health_check_host`). They are read-only; none of them changes state, and
the localhost guard is deliberately not applied to them.

**Why the default is not loopback**: these are liveness signals meant to be polled by an
orchestrator or load balancer, and a containerised daemon bound to loopback is unreachable from
its own host. Narrowing the default would silently break a documented deployment. It is
configurable via `health_check_host` for operators who want it tighter.

**Residual risk**: `/metrics` is Prometheus output disclosing operational detail — card counts,
model identifiers, error rates, cycle timings — to **anyone who can reach the port**. That is
information about your work and your infrastructure, not credentials, but it is more than you may
intend to publish. Set `health_check_host` to `127.0.0.1` if the port is reachable from anywhere
you do not control.

## GitHub token permissions

Use a **fine-grained** personal access token scoped to the single repository coordinare works on,
plus the project board. Do not use a classic token, and do not grant organisation-wide access.

Audited against the code on 2026-08-27 by enumerating the GraphQL operations in
`src/coordinare/services/github.py` rather than from recollection. The operation that requires
each permission is named, so you can check the claim yourself.

| Feature | Permission | Access | Needed for a minimal run? | Why |
|---|---|---|---|---|
| Read the project board, move cards | Projects | Read and write | **Yes** | `PollBoard`, `FindProject`, `GetProjectFields`, `MoveCard` |
| Read issues and card context | Issues | **Read and write** | **Yes** | `GetIssueDetails` and `ListOpenIssues` read, but `AddComment` posts clarifying questions back to issues, and `CreateLabel` / `AddLabels` manage labels, which GitHub scopes under Issues even on pull requests |
| Clone the repository, push branches | Contents | Read and write | **Yes** | `GetFileContent`, `GetFileBlobSha`, plus performer pushes |
| Open and update pull requests, merge | Pull requests | Read and write | **Yes** | `PR`, `CheckMergeability`, `GetPRReviews`, `RequestReviews`, `SquashMerge` |
| Read CI results to gate advancement | Checks / Actions | Read | **Yes** | check conclusions gate stage advancement |
| **Read branch protection** | **Administration** | **Read** | **Yes** | `GetBranchProtection`, used by the monitor to reason about required checks (`monitor_performer.py`) |
| Read repository metadata | Metadata | Read | **Yes** | mandatory for every fine-grained token |

**Two corrections made after auditing the code**, both of which would otherwise have surfaced as a
mid-run failure rather than at setup, which is exactly what this matrix exists to prevent:

- **Issues needs write, not read.** Coordinare comments on issues and manages labels. An earlier
  draft listed it read-only.
- **Administration: read was missing entirely.** Branch-protection lookups need it.

A **minimal first run** needs the rows marked yes. Everything else can be granted later, when you
enable the feature that needs it.

**Direction of travel**: a GitHub App is the better long-term answer, since it gets
per-installation tokens that expire rather than a long-lived PAT. The foundations exist in spec
085 (`AppAuth`), and moving to it is future work.

**Residual risk**: coordinare holds this token in memory and passes scoped credentials to
performers. A compromised performer or daemon has the token's full authority for as long as it is
valid. Fine-grained scoping limits the blast radius to one repository; it does not prevent damage
within it. Rotate the token if you suspect anything.

## How to verify these claims

The behavioural claims above are held up by tests rather than by assertions about this document's
wording. Run them:

```bash
.venv/bin/pytest tests/unit/test_144_threat_model_trust_boundaries.py -v
```

| Claim | Test |
|---|---|
| Foreign origins cannot make changes | `test_row5_foreign_origin_is_rejected_on_mutating_requests` |
| Foreign hosts cannot read configuration | `test_read_route_rejects_a_foreign_host_through_the_app` |
| Non-browser callers still work | `test_row4_absent_origin_is_allowed_on_mutating_requests` |
| Every mutating route is guarded, including future ones | `test_guard_is_fail_closed_for_every_mutating_route` |
| All loopback spellings are accepted | `test_permitted_hosts_accepts_every_loopback_spelling` |
| Only explicitly trusted hostnames widen the guard | `test_permitted_hosts_honours_the_operator_allowlist` |
| The webhook exemption still authenticates | `test_webhook_path_is_exempt_because_it_authenticates_itself` |
| A non-loopback bind is flagged | `test_non_loopback_bind_is_flagged` |
| The health bind default is a decision, not an oversight | `test_health_check_host_defaults_to_all_interfaces` |

If you change a claim here, the test named beside it is what will contradict you. That is the
point: the claim and its evidence sit together, and neither can drift far from the other without
someone noticing.

## What this document does not cover

- **Authentication for the dashboard.** Spec 143 ([#197](https://github.com/ViviDynamics/coordinare/issues/197)).
- **Sandboxing performers more strongly**, or removing the `docker.sock` mount. Both are
  substantially larger pieces of work than this document's hardening.
- **Rate limiting or audit logging** for the dashboard.
- **Supply-chain security of coordinare's own dependencies.** See the dependency licence audit in
  `specs/142-license-and-legal-posture/` for what ships, and note that licence compatibility is a
  different question from provenance.

## Reporting a problem

See [SECURITY.md](../../SECURITY.md). Report privately; do not open a public issue for a
vulnerability.
