# Installing release packages

Each GitHub release contains the daemon and optional performer wheels, the shared
package wheels they require, source distributions, and
`SHA256SUMS` and `install.sh`.

## Install without a checkout

Linux and macOS on x86_64 and arm64 are supported. On Windows, run this command
inside WSL. Python does not need to be preinstalled; uv supplies Python 3.12.

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh | sh
```

For people who prefer to inspect scripts, download first, inspect, then run:

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh -o install.sh
less install.sh
sh install.sh
```

The installer checks every required release wheel against `SHA256SUMS` before
installing anything, including uv. A malformed manifest, missing wheel, duplicate
entry or checksum mismatch aborts. These checks detect corruption; authenticity
relies on HTTPS and the release publisher. GitHub attestations are also available
for independent provenance verification.

If uv is missing, the installer asks through `/dev/tty` before running the
[official Astral installer](https://docs.astral.sh/uv/getting-started/installation/).
Without a terminal, it fails with instructions unless consent is provided with
`--yes` or `COORDINARE_YES=1`. The script never reads prompts from the piped script
input. It does not edit shell profiles.

Pin a release or consent to installing uv in an unattended job:

```sh
sh install.sh --version 2026.10.84 --yes --no-init
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh | sh -s -- --yes --version 2026.10.84 --no-init
```

Use `COORDINARE_VERSION` instead of `--version`; the flag takes precedence.
`COORDINARE_YES`, `COORDINARE_NO_INIT`, `COORDINARE_UNINSTALL`, and
`COORDINARE_PURGE` match their flags and accept `1`/`0`, `true`/`false`, or
`yes`/`no`. `--no-init` is accepted for unattended callers. Initialization is
always an explicit next step in this version, including in a terminal:

```sh
coordinare init
```

The installer does not create configuration or launch the daemon. Init is
shipped and offers foreground startup or a user login service after preflight.
The solo preset needs the performer command alongside coordinare, so both are installed in the same isolated uv tool environment.
Local mode needs an authenticated harness CLI, GitHub board and credentials;
the [first launch guide](first-launch.md) describes these requirements.

Installed commands live in `~/.local/bin`. If necessary, the installer prints:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Add that line to your shell profile as well as the current shell. The environment
lives in `~/.local/share/uv/tools/coordinare`, with managed Python under
`~/.local/share/uv/python` and download cache under `~/.cache/uv`. The installer
uses these home locations even if global uv storage overrides are set. No sudo
or system Python changes are needed. Re-running resolves latest again, or installs
the pinned version, and replaces the tool in place while keeping configuration
and state. It does not guarantee third party dependency versions remain identical.

```sh
sh install.sh --uninstall
sh install.sh --uninstall --purge
```

First remove any login registration with `coordinare service uninstall`, or stop
the foreground process with Ctrl+C. Installer uninstall removes the tool and its
bundled performer. Without `--purge`, all configuration and state remain. Explicit purge removes `~/.coordinare`, including
its config, caches and any state stored there. It does not purge init's XDG
config or data directories. Custom config paths and project
state such as `./coordinare.state.json` remain operator managed. uv, its managed
Python installations and shared download caches remain installed.

`COORDINARE_RELEASE_ROOT` can point at a trusted HTTPS mirror or a local `file://`
release tree. Its layout is `latest/download/` and `download/<tag>/`, each with
the release manifest and five wheels. This is also the offline fixture seam used
by tests; external dependencies and missing uv still require network access.

See [upgrading](upgrading.md), [uninstalling](uninstalling.md),
[unattended setup](unattended-install.md) and [file locations](files-and-paths.md).

## Release packaging

Install the daemon from a wheel URL on the GitHub release with `uv tool install
<wheel URL>`. Its shared dependencies use direct URLs to wheels from the same
GitHub release. PyPI and mirrors that serve only PyPI packages cannot resolve
these packages. Deleting a release breaks installs of that version.

The GitHub release and the `latest` container images are separate tracks. The
Python release stays a draft until its build, locked dependency smoke tests,
attestation, checksums, and asset upload succeed. A failed attempt leaves the
draft available for reuse by a workflow rerun. Image promotion retains its own
image build dependencies, so a Python packaging failure does not block the
`latest` image tags. The two tracks can temporarily advertise different versions.

The build job writes `SHA256SUMS` after both smoke tests and transfers it with
the artifacts. The publication job verifies those checksums and the exact file
inventory before GitHub attestation. It uploads only the verified files and that
same manifest. Provenance is recorded by GitHub attestation rather than uploaded
as a separate release asset. When reusing a draft, publication first deletes
existing assets whose names match the new uploads. A checksum mismatch, missing
or extra file, failed deletion, or failed upload leaves the release unpublished.

Smoke tests install external dependencies exported with hashes from `uv.lock`.
The optional performer installation also uses its own lock file. Local release
wheels are installed without dependency resolution while their GitHub draft URLs
are private, and dependency compatibility is checked after installation.

Legacy tags such as `2026.09.01.3` remain valid download paths. Package metadata,
wheel filenames, and runtime versions normalize numeric components to
`2026.9.1.3`, as required by Python version normalization. New tags retain their
existing version spelling.

## Release verification record

Before release, an operator must follow only the
[agent setup guide](setup-for-agents.md) on a clean supported
machine with the listed facts. Record OS/architecture, release version, selected
harness/mode, sanitized validation and doctor JSON, exit codes and service status
in the PR. Do not record credentials or raw logs. Mocked tests are not this manual
check. Record any environment limitation and complete the release check on a
machine that permits installation, network access and listener binding.
