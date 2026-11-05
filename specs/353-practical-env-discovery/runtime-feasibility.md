# Private container runtime feasibility

## Result, 2026-10-18

A software-emulated Linux guest successfully supplied a private Docker daemon
inside an ordinary Docker consumer container. The fresh consumer created a
container, ran a Compose service, removed both workloads and their network, and
powered down the guest. The run exited 0 in 95.11 seconds.

This is a small runtime capability experiment, not Elm validation or a production
adapter. The independent coverage rejection in [validation-results.md](validation-results.md)
still applies.

## Isolation and reproducibility

Installer and consumer used the same immutable outer image. QEMU 10.0.13 ran with
software TCG, without KVM or another host device. Both outer containers dropped
all Linux capabilities, prohibited privilege escalation, and published no ports.
Neither mounted a host Docker socket.

The consumer used `--network none`; its guest had no network interface supplied
by QEMU. The guest created its own internal container network. Its installed
base disk and bootstrap seed were mounted read-only. All guest writes went to a
fresh QCOW2 overlay in disposable consumer storage. An independent rehash confirmed
the base disk's hash and size were unchanged.

The installer downloaded an official Alpine aarch64 cloud image and verified its
published SHA-512. It installed Docker and Compose inside the guest and cached
BusyBox 1.37.0. Earlier failures are preserved: missing optional network boot ROM,
guest cgroup initialization, and a daemon-start readiness race. The eventual
installer result followed a successful daemon health check and image retrieval;
the clean result required actual execution, rather than a CLI version check.

Both owned outer containers were removed and absence verified. The temporary
private control key was deleted. The diagnostic image and guest disk remain
explicitly owned local test artifacts pending the current investigation.

## Evidence and limits

Private artifacts are under `.agents/state/353-vm-feasibility/`: isolation
inspection, original failures, installer output, clean command and serial output,
base disk identity, terminal result and container cleanup proof. A separate agent
reviewed those artifacts and independently rehashed the disk.

This does not establish Elm builds, multi-service communication, database/browser
requirements, offline image completeness, source sharing, activation, normal
manifest lifecycle integration, or full-workload performance. Kubernetes's
non-root and read-only-root constraints were not tested. Cancellation and resource
exhaustion cleanup were not tested. No new product runtime or ecosystem recipe
has been implemented.

## Elm validation outcome

The `vm-tools-elm60m-high` profile ran existing production discovery, independent
review and deterministic installer/clean-consumer paths against the same pinned
Elm snapshot. Its immutable image adds only optional QEMU, firmware, ISO creation
and SSH platform tools. It supplies no prepared guest disk and does not edit a
generated manifest.

This separately labeled profile uses `onprem/glm-5.3-flash`, high reasoning effort,
canonical JSON transport and the approved 3,600-second ceiling. Earlier low-effort
profiles are retained. Original repair limits and every acceptance gate remain
unchanged. Exact proposed probes and execution scopes require inspection before
execution. The capability experiment cannot waive missing coverage or any failed,
missing or unrun required check. Other repositories remain stopped until Elm
actually satisfies those conditions.

The run held before installation after 1,261.65 seconds. The reviewer correctly
raised coverage objections, but set `capability_unavailable` without an open
capability finding. Coordinare incorrectly reported that contradiction as a source
citation problem. The reviewer repaired citations instead of the flag, exhausting
the existing schema correction allowance. The pinned source remained unchanged.
This is a repair-diagnostic defect, not evidence that Elm installation completed or
that its required services are impossible.

The next `accurate-review-elm60m-high` run reached a valid independent rejection
with four open findings, but terminated before either coverage revision because
the capability flag overrode `revise`. It held after 1,326.00 seconds without
installation. The correction routes explicit revision requests through the
already approved bounded repair loop; it does not supply or validate a runtime.
The small private VM experiment remains distinct from full Elm acceptance.


## Integrated source and service fixture, 2026-10-18

The optional runtime candidate now has an opt-in executable fixture at
`tests/integration/test_353_private_service_runtime.py`. It is a diagnostic
facility, not a Coordinare runtime adapter or Elm readiness evidence. All results
explicitly carry `readiness_evidence: false`.

The full fixture passed two tests in 736.66 seconds. Ordinary outer Docker and
Compose commands built from real source, bound that source at the identical guest
path, reached HTTP services, and removed their workloads. A fresh consumer used a
relocated source root and a read-only cache with no outer network. Both phases
verified binary content, executable bits and symlink targets using Git modes.
The consumer rebuilt offline and rejected a missing image. Actual writes to the
cache failed from both the outer container and guest.

The full fixture also passed these cases:

- A live outer canary and model-proxy canary were reachable from installation,
  remained live outside the clean guest, and were inaccessible from inside it.
  External and metadata requests also failed.
- A symlink toward an outer-only parent marker could not expose that marker.
  Replacing the pinned SSH host key caused authentication to fail.
- Two concurrent consumers served port 8080 in distinct network namespaces.
- Cancellation during active guest boot, Docker build, service health and stop
  reaped QEMU, removed ephemeral control keys, and released the control listener.
- A 32 MiB guest filesystem reached ENOSPC and was unmounted. A separate 512 MiB
  outer memory limit produced an observed cgroup OOM; that phase held and its
  resources were reaped. These expected diagnostic failures cannot waive real
  project failures.

The installer took 145.345 seconds and peaked at 1,743,986,688 cgroup bytes; the
clean consumer took 107.671 seconds and peaked at 1,984,032,768 bytes. Admission
recorded 2 CPUs, 4 GiB memory, an 8 GiB cache ceiling, 16 GiB scratch ceiling and
16 GiB free-space reserve under one 1,800-second aggregate deadline. Inspection
confirmed capability drop, no privilege escalation, no devices, no published host
ports, and the intended network and cache modes. Every owned outer container was
confirmed absent, and the sealed guest hash remained unchanged.

A separate real controller-SIGTERM test passed in 1.06 seconds: the controller
observed its live QEMU process before interruption and confirmed owned container
absence afterward. The test harness also verifies graceful controller cleanup
when its own timeout interrupts collection.

A separate BuildKit fixture passed in 290.81 seconds using a carrier with Buildx
0.13.1. Installation cached a digest-pinned Dockerfile frontend and a cache-mount
marker; the network-disabled consumer executed a different build argument and
read the retained marker. This proves an actual offline build rather than a
version check. Installer/consumer times were 164.282/122.249 seconds, with peaks
of 1,897,738,240/2,105,319,424 bytes. The artifact remained unchanged and both
owned containers were confirmed absent.

Failures remain part of the evidence. The old carrier had no Buildx plugin and
failed before VM startup. An early harness attempt inherited pytest's 30-second
timeout; the wrapper now allows the configured runtime deadline plus cleanup and
terminates its controller gracefully on interruption. A macOS/Linux symlink-mode
difference was corrected by using Git-significant modes. The first nested-kernel
repeat booted Linux successfully during installation, but its clean phase found
installer temporary files in the cached guest root. The guest now mounts a fresh
temporary filesystem on each boot. The repeat passed in both roles: nested Linux
boot took 132.238 seconds during installation and 129.988 seconds in the clean
consumer. Total phase times were 284.866/247.624 seconds. The immutable artifact
was unchanged and both owned containers were confirmed absent. This is a tiny
synthetic kernel boot, not Elm's fsverity or application tests.

Raw reports, frozen code/configuration hashes, command exits, isolation snapshots,
resource measurements and cleanup receipts remain in private profiles
`353-runtime-integration-pytest-004`, `353-runtime-controller-cancel-008`,
`353-runtime-buildkit-010`, failed `353-runtime-nested-006` and corrected
`353-runtime-nested-011` under `.agents/state/`.
Earlier failed profiles are retained. They use pinned Alpine 3.23.6 x86_64 guest
inputs with published SHA-512 verification, executing via software TCG on the
actual ARM carrier without KVM or host devices. Actual carrier content IDs and
exact inputs are recorded with each profile rather than mutable tag claims.

This still does not prove Elm's kernel, services, full dependency closure or
workload timing. Kubernetes non-root/read-only-root support remains unproven.
No project-specific recipe or source modification was introduced. Before product
reuse, transient guest state must be isolated, artifact identities must be bound
to the accepted manifest, and ordinary performer jobs must retain the same
facility through their complete lifetime.


The final full repeat on the Buildx-enabled carrier and fresh-temporary-filesystem
code passed two tests in 797.53 seconds. Installer/consumer phase times were
154.446/120.868 seconds; their peaks were 1,703,022,592/2,006,618,112 bytes. All
four active cancellation cases, both concurrent consumers, disk exhaustion and
observed cgroup OOM passed. All ten owned outer containers were confirmed absent.
The 535,887,872-byte sealed artifact had identical before/after SHA-256:
`88cba9cc2d9c755cd3b2f1e2ca14c4a8b7915ab589c3a1ed54e05cc5e60f306b`.
Raw evidence is retained in `353-runtime-full-final-012`. This completes the
synthetic runtime integration gate; ordinary Coordinare lifecycle integration and
real Elm acceptance remain separate required work.
