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
identity, aggregates required outcomes and checks required-check coverage. It excludes
agent calls, subprocess execution, network, source reads and provenance walks.
Ten warmup iterations precede 100 measured iterations; p95 uses the nearest-rank
95th sample. The process exits nonzero when p95 exceeds 250 ms.

Measured hardware: Apple M1 Ultra, arm64, 20 logical CPUs, 128 GiB memory,
macOS 26.5.2, Python 3.14.6. This is the development host, **not a CI runner**.

| Metric | Result |
| --- | ---: |
| Median | 24.48 ms |
| p95 | 30.72 ms |
| Maximum | 33.70 ms |
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
