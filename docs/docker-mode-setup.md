# Docker mode setup

Install and start Docker Desktop, or Docker Engine with permission to use its socket.
Run `coordinare init --run-mode docker`. The default full image adds Chromium,
security scanners and test tools to the base image's Python, Node and git tools.
Allow approximately 4 GiB free for base or 10 GiB for full. These are conservative
setup disk budgets, not measured download sizes; sizes vary by release and architecture.
Use `--image-variant base` for the smaller image when those extra tools are unnecessary.
Older base releases without the harness installation entrypoint are refused; choose full.

Init pulls `ghcr.io/vividynamics/coordinare-performer-full:<installed-version>`
(or `coordinare-performer-base`) and inspects its harness setup entrypoint. It writes
`:<version>@sha256:<digest>` when Docker reports a matching registry digest; otherwise
it retains the version tag and says so. It never falls back to a floating tag or builds locally.
For a development or prerelease install with no corresponding published image, choose
an existing release explicitly with `--image-tag VERSION`. A missing registry tag gives
this same guidance. An override can differ from the daemon version; doctor warns.

Example unattended setup:

```sh
export COORDINARE_INIT_GITHUB_TOKEN='your-token'
export OPENAI_API_KEY='your-model-key'
coordinare init --non-interactive --run-mode docker --repository owner/repo \
  --project-number 1 --harness codex --human-reviewers your-login
```

The image tag and variant also accept YAML answers or the environment variables
`COORDINARE_INIT_IMAGE_TAG` and `COORDINARE_INIT_IMAGE_VARIANT`.
Secrets stay in the private `coordinare.env` file, mode 0600, beside config.yaml.
No model or GitHub credential is stored in config.yaml, an image or a compose file.
Init also generates a random authentication token for each ephemeral endpoint in
`coordinare.env`; config.yaml stores only its environment reference. Forced reruns
retain these tokens. The daemon sends the endpoint token as an HTTP bearer token
and supplies it to Docker in a private temporary `--env-file`, removed after launch.
Token values never enter Docker command arguments. The daemon passes model and
GitHub credentials in the per-job init payload at runtime, using the existing HTTP
performer secret channel. Host CLI login directories are not mounted;
native providers in Docker require a model API key rather than a host harness login.
Self-hosted endpoints without authentication can omit the API key.

Workspace clones, temporary files (`TMPDIR=/workspace`) and workspace-local caches
live in the Docker named volume mounted at `/workspace`. Its name is
`coordinare-workspaces-<12 hex digits>`, derived from the resolved config directory
so separate installs do not share workspaces. Find the exact `volume_name` in the
starter endpoint's `volumes` list. Inspect it with:

```sh
docker run --rm -v <vol>:/w alpine ls /w
```

This example may pull Alpine; use an available local image for offline inspection.
Tools can save retained output under `/artifacts`, which remains a host bind under
`${XDG_DATA_HOME:-$HOME/.local/share}/coordinare/artifacts`. Transcripts arrive over
HTTP and run logs via `docker logs`; the daemon writes both in its own data paths.
Optional diagnostic log/capture binds and env-bootstrap toolchains can still create
root-owned host files. They retain their existing behavior; the workspace volume
does not fix ownership in those separately configured paths. Environment caches
remain host-visible because the daemon seeds, verifies and reads them. The env cache is mounted per job
by the existing bootstrap lifecycle; the entire cache root is never writable by a
performer. Containers use Docker's default bridge network and a host gateway alias.
Use a model endpoint reachable from both the host and containers, such as a LAN
address. `localhost` inside a container refers to the container itself. The host is
reachable as `host.docker.internal`; this alias need not resolve on a Linux host.

The generated endpoint publishes its port on `127.0.0.1` by default. When the daemon
runs in a container, explicitly set the endpoint's `bind_host: 0.0.0.0` and
`connect_host: host.docker.internal` (or a hostname reachable from that container).
The bearer token is required for performer requests on that published port.

Existing `preset: containers` configurations keep their previous expansion, including
the local `coordinare-performer:full` fallback image. Only init writes the new image,
mounts, cache root, host alias and TMPDIR settings explicitly. The lifecycle's localhost
port binding is a separate security change; containerized daemons must opt into the
publish and connect addresses above.

After pulling and inspecting the image, init runs a named, labelled throwaway container
through the image's real entrypoint. It checks `/healthz`, authenticated `/status`,
and the selected harness's advertised capability, then probes GitHub and the model's
models API from inside the container without consuming model tokens. Startup has a
five-minute budget and each Docker invocation is bounded. Cleanup always runs by
container name, including after a start timeout. A cleanup failure reports a removal
command and prevents setup success. Use `--skip-smoke` on slow machines to skip this
verification explicitly. A smoke failure leaves existing setup files untouched.
Normal host GitHub/model preflight also runs.

The Docker Setup Smoke workflow resolves the newest published release through the
GitHub releases API using the job token. It runs unattended init with account/model
probes stubbed and pulls the published full image without registry login. It also
builds the checkout's base image to exercise this PR's entrypoint change, since older
published base images lack the installer. Both probes require harness readiness
through the shared product smoke function. No live card is dispatched.

If GHCR returns an access error, authenticate with `docker login ghcr.io` using a token
with `read:packages` and access to the package. Public packages need no login. Package
visibility cannot be established from the source tree; the anonymous CI pull checks
it for the selected tag. Docker Desktop and remote contexts also require sufficient
space in their daemon disk allocation; init can only measure a Docker root visible
on the host or the local config filesystem.

After upgrading Coordinare, rerun `coordinare init --force --run-mode docker` to pull
and repin the matching release, retaining private backups of existing setup files.
Run `coordinare doctor` to detect image tag mismatch and check the configured harness.
Stop active work before changing performer settings. No daemon compose deployment is
generated by init. If you mount a Docker socket into a daemon container yourself,
that container can control the host Docker daemon. Read the
[threat model](security/threat-model.md) before doing so.

For an existing init setup, stop the daemon and run
`coordinare init --force --run-mode docker` with the original `--config-dir`, if
custom. This changes the starter's `/workspace` host bind to a named volume and
preserves unrelated mounts and settings. Existing configs work unchanged until
this explicit migration. Custom named volumes are retained. Old clones are not
copied; performers clone again on the next job.

After confirming migration, remove old root-owned workspace directories once:

```sh
sudo rm -rf <data>/coordinare/workspaces
```

Here `<data>` means the old XDG data root, normally `~/.local/share`. Check that
this is the old workspace path before deleting it. Installer uninstall offers to
remove the managed workspace volume; `--yes` permits removal unattended. Supply
`--config-dir` for a custom setup. See [uninstalling](uninstalling.md).
