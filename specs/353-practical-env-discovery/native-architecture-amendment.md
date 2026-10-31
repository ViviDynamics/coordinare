# Native architecture amendment

Issue #353. Supersedes the guest-architecture choice in `private-service-runtime-amendment.md` (the "amd64 guest on the
actual arm64 carrier first" decision). Records the maintainer's finding: "Why is it building the amd64 variant on arm64
hardware? That seems like a serious bug. Make sure the bootstrapper optimizes for the native architecture."

## Problem

The carrier runs install commands natively. The private service guest that hosts the Docker daemon was hard-wired to
x86_64: an Alpine x86_64 image, `qemu-system-x86_64`, and SeaBIOS. On an arm64 carrier without KVM that means every
`docker pull`, `docker build` and `docker run` for every repository runs under full-system x86 emulation on arm64.
In the v41 run, `docker compose build web worker` took more than 21 minutes for that reason alone.

The discovery model made it worse. It saw `platform: linux/aarch64` beside `guest_platform: linux/x86_64` and downloaded
`linux_amd64` helm and gh binaries into the arm64 carrier's cache.

## Decision

1. **Native guest by default.** The guest's CPU follows the carrier's. An arm64 carrier gets an Alpine 3.23.6
   `aarch64-uefi-cloudinit` guest run by `qemu-system-aarch64` with `QEMU_EFI.fd`, `-M virt -cpu cortex-a72`, and KVM when
   `/dev/kvm` is usable (`-cpu host` then). An x86_64 carrier keeps its Alpine x86_64 BIOS guest and `qemu-system-x86_64`.
   `prepare_guest.py --arch` selects the profile; the Dockerfile selects packages and the pinned image by `uname -m`.
2. **Foreign platforms on demand, by user-mode emulation.** The carrier installs Debian's static `qemu-user-static`
   interpreters. Preparation copies the other CPU's static interpreter into the guest image at
   `/usr/local/lib/coordinare/qemu-<arch>-static`; boot registers it with binfmt_misc (`:OCF`, ELF machine and mask per
   architecture). Docker containers then run `--platform linux/amd64` on an arm64 guest without a second guest. The
   registration is best effort: a kernel without binfmt_misc keeps the native platform usable and the foreign one absent.
3. **Capabilities say where work runs.** Discovery's identity context carries `platform_roles`:
   `install_and_check_platform` (the carrier, where installs and checks run), `container_daemon_platform` (the guest's
   native platform) and `emulated_platforms` (derived from the guest platform, never from prose). The manifest
   instructions state the rule: download artifacts for the install platform; request an emulated platform only when the
   repository explicitly requires it. No capability digest or persisted schema changes. The emulated set follows from
   `guest_platform`, which is already part of the runtime identity digest.
4. **Admission rule `foreign_architecture_artifact`.** A fixed `curl`, `wget`, `aria2c` or `gh ... download` segment that
   names an architecture other than the install platform's is a named correctable error with the command id only. A
   command that detects the running CPU (`case`, `uname`, `--print-architecture`) selects its artifact at run time and is
   not judged. Image platform flags (`docker buildx --platform`, `docker pull --platform`) are not fetches.

## Pinned inputs

| Input | Value | Verification |
| --- | --- | --- |
| aarch64 guest | `alpine-3.23.6-aarch64-uefi-cloudinit-r1.qcow2`, 247857152 bytes | SHA-512 `d13783e0...4c41` equals the official `.qcow2.sha512` file |
| x86_64 guest | `alpine-3.23.6-x86_64-bios-cloudinit-r1.qcow2` | SHA-512 `2853e42b...76c` equals the official `.sha512` file |
| carrier base | `ghcr.io/vividynamics/coordinare-performer-full@sha256:2b68979a...93d` | unchanged |
| aarch64 emulator / firmware | Debian trixie `qemu-system-arm` 1:10.0.13+ds-0+deb13u1, `qemu-efi-aarch64` 2025.02-8+deb13u1 | from the image build log |
| foreign interpreter | Debian trixie `qemu-user-static` 1:10.0.13+ds-0+deb13u1, `qemu-x86_64-static` | sha256 `9af86352...4a59` recorded in `upstream.json` |

The Dockerfile arguments are now `PLATFORM_GUEST_URL_X86_64`, `PLATFORM_GUEST_SHA512_X86_64`,
`PLATFORM_GUEST_URL_AARCH64` and `PLATFORM_GUEST_SHA512_AARCH64`. The former single `PLATFORM_GUEST_URL` and
`PLATFORM_GUEST_SHA512` build arguments are gone; an operator who overrode them must set the per-architecture pair.

## Measurements

Host: Docker Desktop on an Apple M1 Ultra (8 vCPU, 32 GiB), kernel 7.0.14-linuxkit, `linux/aarch64`, no `/dev/kvm`, so
every guest here runs under TCG. Each guest gets the production memory rule (half of a 4096 MiB profile: 2048 MiB) and
4 vCPUs. The harness boots a guest exactly as the supervisor does (`cloud_configuration`, `qemu_arguments`, the bundle
read from the image). Timings are wall-clock seconds from one run each.

Workload, identical in both guests: `FROM debian:trixie-slim`, `apt-get install gcc libc6-dev`, compile and run a
hello program. The amd64 variant differs only by `FROM --platform=linux/amd64`.

| Step (seconds) | Native aarch64 guest (new) | x86_64 guest (pre-change) |
| --- | ---: | ---: |
| Boot to dockerd ready (`docker info` over ssh) | 115.5 | 82.7 |
| Pull `debian:trixie-slim`, native arch | 25.3 | 23.5 |
| Build the workload, native arch (arm64 / amd64) | 189.5 | 218.9 |
| Pull `debian:trixie-slim`, `--platform linux/amd64` | 23.2 | not applicable (native amd64 in the x86 guest) |
| Build the workload, amd64 (binfmt user-mode in native guest / native in x86 guest) | 999.7 | 218.9 |
| Run result | `hello from build`, `uname -m` aarch64 | `hello from build`, `uname -m` x86_64 |
| Native arm64 run in the native guest | `hello from build`, `uname -m` aarch64 | not applicable |
| amd64 run through binfmt in the native guest | `hello from build`, `uname -m` x86_64 | not applicable |

Preparation: the arm64 platform guest was prepared inside the carrier image build. Its layer timestamps put the
preparation at about 2m45s; the pre-change x86 carrier image built in 162s on this host. Image sizes: the native carrier
was 5.97 GB and the pre-change carrier 4.99 GB, the difference being the arm64 UEFI firmware and the static
interpreters.

Reading these numbers:

- Native work is native. The arm64 build in the new guest is 13 percent faster than the same build in the x86 guest,
  but boot is about 33 seconds slower, because the aarch64 guest pays for UEFI firmware. First-run total, boot plus pull
  plus build, is about equal (330 s against 325 s).
- Foreign work is slow. On this host, with no KVM, an amd64 build through binfmt user-mode emulation inside a
  TCG-emulated arm64 guest takes 999.7 s, 4.6 times the same build in the pre-change x86 guest (218.9 s). The x86 guest
  runs amd64 code under one layer of translation; the binfmt path runs it under two. A repository whose images are
  amd64-only will be slower under the native default on this host, not faster.
- The benefit of the native default is therefore conditional. It is real on an arm64 Linux host with KVM, where the
  guest runs natively and the x86 guest could never use KVM. It is not demonstrated here, because this host has no
  `/dev/kvm`.

Resolved by the maintainer (round 2, below): the guest is chosen per repository, not for the whole deployment.

## Per-repository guest choice (round 2)

Maintainer decisions: (1) the guest architecture is chosen per repository. The native guest is the default; the other
CPU's full-system guest is used only when the repository explicitly requires that platform. (2) The guest vCPU count
rises to 8; memory is unchanged.

1. **Both guests are prepared in every carrier image.** `/opt/coordinare-runtime/<cpu>/` holds each guest's pinned
   image, `bundle.json` and `upstream.json`. The Dockerfile installs both system emulators and both static interpreters
   on either carrier, so either guest can boot anywhere the image runs. Each guest has its own identity digest.
   Preparation: the x86_64 guest took 133 s and the aarch64 guest 143 s inside the image build. The image grew from
   5.97 GB (one arm guest) to 6.87 GB (both guests, both system emulators).
2. **Selection is deterministic and happens before discovery, from the pinned source only.** After the pinned
   inventory is read, `guest_selection.pinned_requirements` reads only candidate files (Dockerfiles, compose files,
   CI workflows, Makefiles, `.sh` files and `bin/`), each under 256 KiB, never symlinks, and never a model. An explicit
   requirement is a literal Linux platform in `--platform`/`--platform=`, a `platform:` key, or
   `DOCKER_DEFAULT_PLATFORM`. Comments, variables, pip tags and documentation do not count. The result (required
   platforms, triggering paths, unsupported literals) is stored on the cache state (`environment_guest`), written to the
   attempt record (`diagnostics.guest`) and logged as `env_cache.guest_selected`. Paths only; no file content.
3. **The guest is bound into the runtime identity.** `RuntimePolicy` gains an optional `alternate_identity` (the other
   CPU's guest of the same carrier). `RuntimePolicy.for_guest` returns the policy holding exactly the selected identity,
   so capabilities, probes, approvals, execution and the clean consumer all see one guest. Capability resolution
   derives the guest from the stored requirements, so clean verification reproduces the decision; a requirement with no
   configured identity is refused. A repository with no requirement gets the carrier's guest and does not need the
   alternate.
4. **Binfmt is registered in both guests.** An incidental foreign image runs through user-mode emulation in the native
   guest, slowly but correctly.
5. **vCPUs.** No product default exists: `RuntimeResources.cpus` is required in each operator policy, and the evidence
   policies (Dettmore v19-v40) set 2. `-smp` already follows `policy.resources.cpus`. The operator's policy must be
   raised to 8; memory is unchanged at 4096 MiB (2048 MiB to the guest).

Boot check from the two-guest image, 8 vCPUs, 2048 MiB to the guest (`boot` mode, one run each): the aarch64 guest
reached dockerd in 132.3 s, and an incidental `--platform linux/amd64` container ran through binfmt (`uname -m` x86_64);
the x86_64 guest reached dockerd in 86.1 s. Both `qemu` argument lists carry `-smp 8 -m 2048`. The workload timings
above were measured at 4 vCPUs and have not been repeated at 8.

### What the pinned snapshots select

Read-only evaluation at each snapshot's pinned HEAD (`git archive` of candidate files only):

- Dettmore (`bf498e84`): **linux/aarch64**, the native guest. Its amd64 build steps in `main-branch-build.yml` and
  `pull-request-build.yml` are publish-only (`--push` inside `nick-fields/retry`'s `with.command`), so they are excluded
  and named in `excluded_publish_steps`. The publish decision is made per step from the parsed YAML: a buildx or build
  with `--push` or `--output type=registry`, a step that runs `docker push`, or `docker/build-push-action` with
  `push: true`. A step is excluded only when it also names a Linux platform.
- Elm (`bc224a78`): **linux/x86_64**, the foreign guest. Triggers: `compose.full.yaml` (service `platform: linux/amd64`)
  and `guest-image/Dockerfile` (`FROM --platform=linux/amd64` on two stages). Its `cd.yml` publish step names no
  platform, so it is not recorded.

Elm therefore runs in the x86_64 guest on this arm64 carrier, the case where the measured slowdown applies (about 4.6
times for an amd64 build through binfmt, versus the pre-change x86 guest on this host). Dettmore runs natively.

Publish-only CI steps (decided per step) are not a dev/test requirement, per the maintainer's decision of round 3.
Dockerfiles, compose files, scripts, Makefiles and non-publishing CI steps still count. A CI step that cannot be
classified counts and is listed in `unclassified_ci`. A workflow that does not parse counts whole.

vCPUs: no product default was changed. The count comes from each operator's policy (`resources.cpus`), and the
operator's run profile sets 8.

## Not proven

- Discovery end to end on Dettmore or Elm with a configured two-identity policy. The selection and binding are unit-tested;
  the snapshots were evaluated read only.
- The workload timings at 8 vCPUs, and whether 8 vCPUs changes the 4.6x foreign-build ratio.
- Operator configs. Any policy with only the x86 identity on an arm carrier is now refused for a repository without a
  requirement until its native identity is added. The v41 policy is not touched by this change.

- KVM. This host has no `/dev/kvm`, so the KVM branch of `qemu_arguments` (`-cpu host`) has no runtime evidence.
- `-cpu cortex-a72` against `-cpu max`. The model was kept; `max` was not measured.
- The production supervisor's binfmt path. The measurement harness reproduces the registration line and the guest
  command, not the supervisor's admission, cleanup or receipt path.
- Preparation time. The arm64 preparation completed inside the image build, and its layer timestamps put it at about
  2m45s, but that is not a clean timed run.
- Old-image preparation time and any end-to-end run of a real repository (Elm) on the new guest.
