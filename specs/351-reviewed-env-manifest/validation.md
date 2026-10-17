# Reviewed environment manifest validation

Results recorded 2026-10-17 against the working implementation based on `ea0a1457`.
This report records focused local evidence. Full required checks, CI and deployment
validation are separate gates; the results below do not substitute for them.

## SC-006 contract processing benchmark

Reproduce with:

```sh
.venv/bin/python scripts/benchmark_environment_manifest.py
```

The script creates 1,000 distinct requirements and 1,000 distinct required checks,
with each requirement mapped to its own check. Each measured iteration validates
the entire manifest, serializes it canonically and hashes it, validates the matching
coverage assessment and 1,000-result verification report, verifies their shared
identity, aggregates required outcomes and invokes the daemon’s execution-report
admission path, including required-check coverage. It excludes
agent calls, subprocess execution, network, source reads and provenance walks.
Ten warmup iterations precede 100 measured iterations; p95 uses the nearest-rank
95th sample. The process exits nonzero when p95 exceeds 250 ms.

Measured hardware: Apple M1 Ultra, arm64, 20 logical CPUs, 128 GiB memory,
macOS 26.5.2, Python 3.14.6. This is the development host, **not a CI runner**.

| Metric | Result |
| --- | ---: |
| Median | 58.05 ms |
| p95 | 67.94 ms |
| Maximum | 74.43 ms |
| Acceptance threshold | 250 ms |
| Fully serialized manifest | 199,994 bytes |
| Manifest/assessment document cap | 262,144 bytes |
| Serialized assessment | 367 bytes |
| Serialized verification report, empty output tails | 218,039 bytes |
| Rendered verification wrapper | 200,390 bytes |

The compact fixture fits the existing document and 256 KiB rendered-script bounds;
its execution evidence fits the 1 MiB transport result bound. No bound was increased.
The benchmark excludes source payloads and per-command output and therefore does not
claim that every combination of maximum-size inputs fits a transport request.
Admission and output-bound tests remain necessary for those combinations. Existing
provenance walks are unchanged and excluded from the timing; this measurement does
not estimate their cost. SC-006's requested CI-runner measurement remains outstanding.

## Executable ecosystem and failure matrix

Reproduce with:

```sh
env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW \
  .venv/bin/pytest -q tests/unit/test_351_environment_acceptance_matrix.py
```

Nine tests pass. Each ecosystem's tool names, versions, dependency names and
repository layout exist only in test data. A declared installation action writes
a deterministic synthetic tool into the cache. The same generic runner then checks
the declared version, dependency loadability, dependency version and working
directory, first after installation and then with fresh consumer scratch state and
no installer result ledger. Both runs retain the accepted manifest digest.

| Fixture | Explicit constraint data | Working directory |
| --- | --- | --- |
| Ruby/Rails | Ruby 3.4.2, Rails 8.0, Gemfile evidence | Repository root |
| Rust | rustc 1.90, application 1.0, Cargo.toml evidence | Repository root |
| Go | Go 1.25, application 1.0, go.mod evidence | Repository root |
| Unfamiliar toolchain | quuxforge 17.4, quuxstdlib 9, construction.notes | Repository root |
| Monorepo | Same unfamiliar constraints, `apps/api/build guide.txt` | `apps/api` |

The four negative cases independently demonstrate that an accepted contract cannot
make an incorrect installed version, explicit `false`, absent executable, or failed
required service health pass. The service case executes initialization in scratch,
starts a real background child, records failed health, withholds dependent checks
and executes shutdown. Fixture acceptance is schema-validated deterministic data;
the separate discovery suite exercises independent reviewer conversations and reads.

These are executable **synthetic toolchain fixtures**, not installations of Ruby,
Rails, Rust or Go. They demonstrate generic execution without ecosystem-specific
core additions, but do not prove compatibility with those real tools or live
services. Clean scratch is exercised locally; read-only cache mounts, network
isolation, image identity enforcement and container teardown need their transport
tests and live Docker/Kubernetes verification.

## Live Docker smoke

Local Docker 29.8.2 executed the current shared package in the existing trusted
performer image `sha256:2b68979af834c1d89eb8dc8a57689f16ed6da5fe26b30782462367147168d93d`.
A generic synthetic installation wrote a cache artifact; a separate fresh container
verified it through a read-only mount with `--network none`. Both results retained
manifest digest `b427fde0bf2a00b13cdf53711414c537aaa434f39108d4eb29a5ef7f93d8a9fb`.
Replacing the required check with `false` returned exit 1 and `passed: false`.
This validates runner isolation, not a full published-image daemon deployment.

The Linux supervisor was also exercised in Docker against detached `setsid`
children with closed streams, timeout cleanup and a daemonized service lifecycle.
Each descendant was killed and reaped before completion. Host tests retain Linux
specific skips on macOS; these scenarios execute on Linux CI.

The benchmark is now exercised by a normal unit test on CI runners. The local
measurement above remains the only recorded timing until CI completes.


## Review transfer and platform regressions

Discovery now admits accepted source inputs against the existing 256 KiB serialized
UTF-8 JSON transport limit before coverage acceptance; reviewer context retains its
separate inspection budget. Multi-file and escaped-text overflow regressions reject
contracts that cannot be transferred. Docker and Kubernetes preserve identical bytes.

Linux Docker reproduced the per-string execution limit with 200 KiB of UTF-8 source.
Docker source setup now uses stdin, while Kubernetes source, verification and provenance
payloads use 64 KiB chunks reconstructed by trusted Python. Actual Linux runs execute
large source setup, verification and provenance walking successfully. Aggregate source
and Pod bounds remain unchanged. These are container proofs, not live cluster validation.

Non-Linux live services explicitly hold with indeterminate completion evidence, while
immediate successful starts remain supported. Linux supervisor lifecycle regressions
continue to pass. The GitHub inventory protocol has a strict type-check proof against
the concrete service, and kind RBAC assertions cover the required Secret lifecycle and
forbid broader mutations or cross-namespace reads. Full-suite status remains CI-owned.

Credential transfer uses the same 64 KiB fragment size within its existing
384 KiB encoded total bound. Independent Linux execution restores a 100,800-byte
credential with identical SHA256, removes transport controls from the check
environment, and redacts printed original values. The final rebuilt image also
passes all three Docker trust scenarios in 7.41 seconds. Strict type checks cover
321 daemon modules and all four shared environment modules; no new suppressions
or increased baseline debt were introduced.
