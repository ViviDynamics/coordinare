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

This applies when a Docker performer endpoint sets `egress_allowlist`. Every
performer on such an endpoint now runs on an internal Docker network whose only
exit is an allowlisting egress proxy, and the performer no longer gets
`NET_ADMIN`. This covers ephemeral performers of every role and stage provider
jobs (#449). Endpoints without `egress_allowlist` are unchanged. Details are in
[egress allowlist](egress-allowlist.md).

### Update performer images before starting the upgraded daemon

The proxy runs from the endpoint's own image. Images built before #448 have no
`performer.egress_proxy` module, so on an endpoint with an allowlist every
performer start fails at "attach its proxy", and the error says to rebuild the
image. Nothing starts without the proxy.

Update the images after installing the new release and before starting the
daemon. For a setup made by init, the image update command above pulls and pins
the image that matches the release:

```sh
coordinare init --force --run-mode docker
```

An endpoint whose `image` you build yourself must be rebuilt from this release.

### Tools that ignore proxy settings fail closed

The performer's network has no route out and resolves no outside names, so a
tool that ignores the proxy variables cannot connect. It fails; it does not
leak. This includes:

- git over SSH, `ssh` and `scp` (coordinare clones over HTTPS)
- Java, Maven and Gradle, unless the build's own settings name the proxy
- raw TCP to an allowlisted host, such as a database or SMTP server
- Node older than 22.21 pinned by a project (the image's own Node uses the proxy)

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
    egress_allowlist: [llm, api.github.com, github.com, repo.maven.apache.org]
    egress_mode: iptables          # advisory: root in the container can undo it
```

### Where the allowlist is not enforced

- **Kubernetes:** `egress_allowlist` is not enforced by a NetworkPolicy yet.
  The Helm chart's opt-in policy takes CIDRs, not hostnames. Tracked in #454.
- **Persistent performers** (`mode: persistent`): the operator starts these
  containers, so coordinare has never applied `egress_allowlist` to them. This
  release does not change that.
