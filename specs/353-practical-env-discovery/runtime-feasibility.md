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
