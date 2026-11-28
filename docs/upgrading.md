# Upgrading

Let active work finish, then stop the foreground daemon with Ctrl+C or stop its
user service:

```sh
coordinare service stop
```

Rerun the installer to replace Coordinare and its bundled performer with the
latest release, and nare with the release that version pins. Configuration and
state are preserved.

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh | sh
```

Or download and inspect the script first:

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh -o install.sh
less install.sh
sh install.sh
```

To select a specific release, use its numeric tag:

```sh
sh install.sh --version 2026.10.84 --yes --no-init
```

The example tag is also used in installer tests. `--yes` consents to installing
uv if missing. `--no-init` is accepted for automation; init is always a separate
step. Reinstalling does not guarantee identical third party dependency versions.

For Docker performers, update the pinned image after upgrading:

```sh
coordinare init --force --run-mode docker
```

If init originally used a custom directory, add `--config-dir` with that same
directory to the image update command. Use `--config` with its `config.yaml` path
for validation, doctor and foreground startup. Otherwise these commands can
select an unrelated default setup.

This also migrates the starter workspace host bind to a per-config Docker named
volume. It preserves other mounts and settings, and keeps existing custom named
volumes. Old workspaces are not copied; the next job clones again. Once migration
is verified, remove the old root-owned tree with
`sudo rm -rf <data>/coordinare/workspaces`, where `<data>` is the previous XDG data
root (normally `~/.local/share`). Existing configs keep working without migration.

Keep the daemon stopped during this step. Decline init's start offers if you
want to inspect the result first. Forced setup keeps private backups. Advanced
configurations that init refuses must have their performer images updated by
the operator. See [Docker setup](docker-mode-setup.md).

If any Docker endpoint sets `egress_allowlist`, every image those performers use
must also be on the new release before the daemon starts again, including images
init does not manage. See [Egress allowlist: the proxy is now the default](#egress-allowlist-the-proxy-is-now-the-default).

Check the configuration while the daemon is stopped:

```sh
coordinare config validate
coordinare doctor
```

For a registered service, reinstall its registration so the executable and
recorded version are current. Installation starts it immediately:

```sh
coordinare service install
coordinare service status
```

Use `coordinare service install --config /absolute/path/config.yaml` for a
custom setup directory. For a foreground process, run `coordinare` or the
explicit config command printed by init. See [user services](service.md).

## Egress allowlist: the proxy is now the default

This applies when a Docker performer endpoint sets `egress_allowlist`. Since
2026.11.36, every ephemeral performer and stage provider job (QAre, Progettare)
on such an endpoint runs on an internal Docker network whose only exit is an
allowlisting egress proxy, and the performer no longer gets `NET_ADMIN` (#449). Endpoints without
`egress_allowlist` are unchanged. Details are in
[egress allowlist](egress-allowlist.md).

### Update performer images before starting the upgraded daemon

The proxy runs from the performer's own image, with options that images from
releases before 2026.11.36 do not have (images before 2026.11.34 have no
`performer.egress_proxy` module at all). With such an image, every ephemeral
performer and stage job start on an endpoint with an allowlist fails at "attach
its proxy". Nothing starts without the proxy. The error says the image predates
the proxy module; for a 2026.11.34 or 2026.11.35 image the fix is the same.

This covers every allowlisted endpoint on the default `egress_mode: proxy`,
which is all of them when you upgrade from a release before 2026.11.36, where
`egress_mode` did not exist. A QAre provider with `sandbox: sysbox` always uses
the proxy. An endpoint on `egress_mode: iptables` does not run the proxy, but
keep its images on the daemon's release as well: images carry other fixes, such
as keeping the GitHub token away from pull request code in stage jobs (#450).

Update the images after installing the new release and before starting the
daemon. The image update command above pulls and pins the matching release
image, but only for the `starter` endpoint that init created:

```sh
coordinare init --force --run-mode docker
```

Move every other image those performers use to the new release, or rebuild it
from the new release, by hand:

- the `image` of any other endpoint, whether a published release tag or one you
  build yourself
- the `image` of a stage provider (`stage_providers.<name>.image`), which
  overrides its endpoint's image and also runs the proxy

### Tools that ignore proxy settings fail closed

The performer's network has no route out and resolves no outside names, so a
tool that ignores the proxy variables cannot connect. It fails; it does not
leak. This includes:

- git over SSH, `ssh` and `scp` (coordinare clones over HTTPS)
- Java, Maven and Gradle, unless the build's own settings name the proxy
- raw TCP to an allowlisted host, such as a database or SMTP server
- Node that does not honour `NODE_USE_ENV_PROXY`, pinned by a project: Node 20,
  Node 22 before 22.21, and for `http` and `https` Node 24 before 24.5 (the
  image's own Node uses the proxy)

If an endpoint's jobs need one of these, set `egress_mode: iptables` on that
endpoint. It restores the previous behaviour exactly: `NET_ADMIN` and iptables
rules inside the container. That mode is advisory. Code in the performer runs
as root with `NET_ADMIN` and can remove the rules, and DNS stays open. A QAre
provider with `sandbox: sysbox` always uses the proxy.

```yaml
performer_endpoints:
  - id: java-builds
    mode: ephemeral
    roles: [implementer]
    image: coordinare-performer:full
    extra_hosts: ["llm:192.0.2.10"]  # how the model gateway's name resolves
    egress_allowlist: [llm, api.github.com, github.com, repo.maven.apache.org]
    egress_mode: iptables            # advisory: root in the container can undo it
```

### Where the allowlist is not enforced

- **Kubernetes:** `egress_allowlist` is not applied in either mode. The Helm
  chart's opt-in NetworkPolicy takes CIDRs, not hostnames. Tracked in #454.
- **Persistent performers** (`mode: persistent`): the operator starts these
  containers, so coordinare has never applied `egress_allowlist` to them. This
  release does not change that.
