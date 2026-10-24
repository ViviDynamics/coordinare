# Optional private service runtime

Issue #353 / PR #354. Status: approved with the combined fix plan; runtime integration proof in progress.

## Purpose and evidence

Complete the documented Elm development/test environment while retaining generic
commands and clean-consumer isolation. Whole-project scope remains mandatory;
unsupported services cannot become optional because the current image lacks them.

The existing software-guest experiment proves only a small offline container/Compose
workload. It does not prove source sharing, ordinary downstream activation, reachable
services, offline build closure, mixed architecture or full-workload performance.
The current generated manifest never supplied those facilities. See
[runtime-feasibility.md](runtime-feasibility.md) for the measured result and limits.

A fresh planning subagent challenged the coverage-first draft and recommended a
runtime integration gate before more full model runs. Its private evidence review
is retained in `.agents/state/353-elm-fix-subagent-plan.md`.

## Selected candidate and alternatives

Investigate an explicitly configured private Linux service guest, leaving ordinary
project tools and performer shells in their current outer environment. This is a
generic optional platform facility, not an automatically chosen project recipe.
Core logic must not recognize Elm, Rails, Go, Compose filenames or service ports.

Docker supports a normal client connecting to a daemon over SSH; QEMU documents
filesystem exports and restricted user networking with explicit forwarding.
Those mechanisms make the candidate plausible, not proven for our isolation policy.
Primary references: [Docker SSH transport](https://docs.docker.com/engine/security/protect-access/),
[QEMU invocation](https://www.qemu.org/docs/master/system/qemu-manpage.html).

Moving only verification into a VM would prove a different environment from the
outer performer shells activated by `environment_contract.py`. Moving the entire
performer into a VM could solve that mismatch but requires substantially broader
workspace, backend and tool integration. It is an alternative requiring a new
decision if this candidate fails, not an automatic fallback. Host socket access,
privileged execution and enabled clean-consumer egress remain excluded.

## Required runtime boundary

Capability observations declare `execution_context`: `outer` by default or
`private_service` when the operator has configured the pinned platform. Native
plans omit the default field in canonical serialization to preserve their identity.
A private observation starts a fresh platform-only guest for each command, with
read-only complete source and empty cache, fresh scratch, no installed-project
receipt and no network egress. Startup, command execution and teardown consume the
existing 120-second probe window and original attempt expiry. Nonzero exits remain
observations; neither successful observations nor cleanup can confer readiness.

- Use software emulation, no KVM/host devices or additional outer capabilities.
  Pin the outer image/platform, guest artifact/platform, emulator/firmware and
  runtime configuration separately. Do not describe a guest architecture as the
  outer image architecture. Same identities bind installation and verification.
- Pin the configured platform guest base before manifest construction. Project
  installation produces a separate sealed runtime-state artifact in cache.
  Seal only after clean daemon/guest shutdown. Bind output hashes in a trusted
  artifact receipt to the already accepted manifest/source/runtime identities,
  not retroactively inside that manifest's digest. Each consumer uses a read-only base
  plus fresh scratch overlay for runtime mutations. Also mount the installation
  cache actually read-only inside the guest; a writable overlay is not proof that
  cache writes are prohibited. Retain required image layers and build metadata
  offline without sharing live daemon state between runs.
- Export only the owned disposable source, cache and scratch roots. Source paths
  must match ordinary Docker bind-mount paths inside the guest, including relocated
  source roots, executable bits and admitted symlinks. Never export home, parent
  directories or the pinned snapshot. Verify the complete execution-source identity.
- Ordinary outer Docker/Compose commands use the guest via a local SSH control
  endpoint. Use per-run scratch keys and pinned host verification. Do not persist
  client or guest host private keys or inherited provider credentials in cache,
  manifests or logs; create both keypairs in per-run scratch/seed and pin that run's
  public host key. Do not disable SSH host verification.
  Activation must work for normal performer tools, not only verifier commands.
- Service endpoint forwarding is explicit generic address/port data declared in
  the contract. Listeners bind only within the owned outer environment; no host
  publication or global Docker context mutation. Test collisions in concurrent
  consumers. No core parser infers endpoints from project or framework files.
- The clean consumer keeps external networking disabled. The guest may communicate
  internally with its services and explicit local control/forwarded endpoints only.
  Restricted user networking must pass actual egress-denial tests, including host,
  metadata and proxy destinations. If that mechanism cannot enforce the boundary,
  stop and review the transport design; do not silently enable networking.
- Runtime, command, service and cleanup supervision share the original attempt
  deadline. Stop on cancellation, timeout or resource exhaustion, then confirm
  owned processes, listeners, overlays and keys absent. Failed or uncertain teardown
  prevents readiness. Never clean unrelated images or resources.

## Decisive integration gate

Use a synthetic unfamiliar repository and declared fixture commands, clearly marked
diagnostic. They must build from exact fresh source, run multiple communicating
services, serve a request from an ordinary outer tool and shut down. Repeat in a
new consumer with relocated source, read-only cache and no external network. Verify
offline image/build closure; a missing image/layer must fail rather than pull or skip.
Verify immutable artifact hashes and that cache writes fail from both sides.

Prove the mixed-architecture facility required by pinned Elm's explicit amd64
component. Test an amd64 guest on the actual arm64 carrier first; an arm64 guest
with guest-internal emulation is an alternative requiring measured proof.
[Superseded by native-architecture-amendment.md: the guest now matches the carrier
CPU, and the other CPU runs through user-mode emulation inside it.] No
architecture is selected solely because a tool exists. Also measure any nested
guest-tool requirement before declaring it usable. No deployment/hardware operation
is authorized by this plan.

Record cold/warm duration, CPU, peak memory, cache and scratch growth, with bounded
resource/cancellation cases. Current observations are 8 Docker CPUs, about 30 GiB
Docker memory and 57 GiB free host storage (97% used); they are not reservations
or proof of sufficient Elm capacity. Check backing-store capacity and set explicit
image/scratch ceilings and a free-space reserve before building. Insufficient
capacity holds; no global prune or unbounded guest disk is allowed.

Only a complete passing integration gate permits productizing this candidate.
The runtime fixture is not Elm acceptance. Kubernetes support needs its own non-root,
read-only-root and isolation proof; unsupported runtime configurations hold explicitly.
Existing supported adapters retain their current behavior and regression coverage.

## Compatibility and acceptance

Version new runtime and endpoint contracts; bind their canonical digest into execution
capabilities, the accepted manifest and both reports. Missing, changed or unknown
runtime identity invalidates readiness reuse. Legacy records remain inspectable
but cannot gain new-policy acceptance through defaults. Trusted runtime setup must
not invent successful project checks or waive missing evidence.

Independent whole-source coverage review, final executable-manifest review and every
required deterministic installer/clean check and service lifecycle result remain
mandatory. Source additions/deletions and inspected evidence retain provenance.
No generated manifest is edited by an operator to make Elm pass.

The user approved this design and [the combined plan](elm-bootstrap-fix-plan.md)
with "Yes" after the independent planning review. No claim is made that the candidate already supports
Elm or that it will fit the authorized 60-minute execution profile.


## Contract implementation detail

The explicit operator setting is `env_cache.private_service_runtime`. Its
`RuntimePolicy` binds carrier/guest identities, emulator/firmware/configuration
hashes, resource ceilings and control limits before discovery. Models cannot
supply those trusted identities. Capability schema 2 requires this policy and
checks the carrier image/platform separately from the guest platform. Native
schema 1 keeps its existing serialized form and digest.

The immutable carrier supplies `/opt/coordinare-runtime/bundle.json`. A bounded,
network-disabled, read-only inspection hashes the actual guest, emulator and
firmware files; declared digests alone are insufficient. A mismatched or absent
bundle holds. The inspector removes only its uniquely named container, including
on cancellation, and requires confirmed absence. The optional configuration is
explicitly refused on Kubernetes until its separate support proof exists.

Project endpoint requests are compiled with the admitted policy into a
`RuntimeSpec`. They cannot expose the guest control service, bind public
interfaces, collide with control/listeners or exceed the endpoint budget.
`RuntimeArtifactReceipt` binds nonempty sealed artifact hashes to that spec,
manifest and complete source identities after installation. Receipts persist
through snapshots and are discarded when clearing, repairing or restarting a
cache generation. Parsing a receipt does not itself grant readiness; final
execution and consumer gates must verify its binding and actual artifacts.

## Live performer purpose

Ordinary live jobs retain the performer's existing carrier connection for daemon HTTP
control and model traffic. The adapter selects `live_consumer` internally; a manifest
cannot select this purpose. Both live and clean consumers require the same bound
sealed receipt, immutable platform identity, read-only installation cache, fresh guest
and overlay, resource limits and restricted guest networking. The default `consumer`
and private preflight retain the offline outer-network admission gate. Unknown purposes
are refused before admission, and a live consumer cannot start without its receipt.

This separates the existing live control connection from clean verification; it adds
no clean-consumer egress. A report for a live job remains `live_consumer` and cannot be
accepted or persisted as the clean `consumer` verification report. The synthetic HTTP
fixture uses the real performer routes and job runner with a fixed injected executor,
not a model call, and asserts that the same bridged carrier is refused by default clean
admission before exercising live tools. This is protocol evidence, never Elm coverage
approval or readiness.

The validation carrier overlays and independently hash-checks both the current shared
environment package and the current performer package. A correct shared runner with
an older performer is unavailable evidence. Missing or mismatched performer hashes
refuse validation before project/model execution; retained earlier shared-only
fixtures cannot establish current performer lifecycle behavior.
