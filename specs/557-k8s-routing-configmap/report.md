# Result

Implemented locally on `223-k8s-routing-configmap`, without network, commits or
cluster operations. Spec analysis before implementation covered all five
requirements with five tasks, with no consistency or payload-contract findings.

## Config and behavior

Add to the existing ephemeral performer endpoint:

```yaml
kubernetes_routing_configmap:
  name: coordinare-performer-routing
  key: routing.yaml  # optional; defaults to routing.yaml
```

Default is null. Kubernetes projects only the chosen key into a read-only
`/etc/coordinare/routing` directory and sets
`SELFHOSTED_ROUTING_CONFIG=/etc/coordinare/routing/routing.yaml`, overriding
that endpoint's explicit env value. Names use DNS-1123 subdomain validation,
maximum 253 characters; keys accept ASCII letters/digits, dots, underscores and
hyphens, length 1-253, excluding `.` and `..` prefixes. Unknown nested fields
and subprocess use are rejected.

The ConfigMap belongs in `kubernetes_namespace`, not necessarily the daemon's
namespace. The pod references it; the kubelet retrieves it. No additional daemon
API call, routing-file read or RBAC permission is needed. Helm RBAC remains pod
management and pod-log reading only. ConfigMap creation remains operator-managed;
no Helm value, template or default behavior was added. Docker is unchanged.

## Files

- `src/coordinare/models/performer_endpoint.py`: optional model and validation.
- `src/coordinare/services/kubernetes_runtime.py`: env injection and pod projection.
- `tests/unit/test_557_k8s_routing_configmap.py`: validation, YAML loading,
  defaults/null, key selection, cache coexistence, isolation and fake-API dispatch.
- `tests/unit/test_147_helm_deployment.py`: endpoint passthrough and RBAC regression.
- `config.example.yaml`: opt-in fields and copyable Kubernetes example.
- `docs/operators/containerized-performers.md`: config reference.
- `docs/onboarding/05-configuration.md`: configuration composition.
- `docs/operators/litellm-proxy.md`: primary ConfigMap path and image alternative.
- `specs/557-k8s-routing-configmap/`: brief spec, plan, tasks and this handoff.

## Verification

- `make lint`: passed.
- `make typecheck`: passed, 317 source files.
- New spec-557 unit suite: 39 passed, including CLI validator integration with
  the main example plus valid/invalid endpoint routing config.
- Combined relevant daemon suites: 566 passed, 2 skipped. Includes Kubernetes
  transport, Helm rendering, egress, pod leak/ownership, endpoint models, config,
  config CLI, verifier/provenance, mount rules, descriptors and issue-223 dispatch.
  Both skips require the absent gitignored production `config.yaml`.
  After tightening reserved ConfigMap key prefixes during review, the final
  spec-557 plus endpoint-model run passed all 75 tests (39 spec-557 cases).
- Performer `test_223_mixed_chunk.py`, `test_routing_translate.py`, `test_routing.py`:
  48 passed, 7 failed. All seven fail at aiohttp startup with
  `PermissionError: [Errno 1]` binding `127.0.0.1:0`, before reaching their
  HTTP assertions. They need a runner that allows loopback listeners.
- `COORDINARE_GITHUB_TOKEN=ghp_placeholder .venv/bin/python -m coordinare config validate --config config.example.yaml`:
  passed. No real credential used.
- `git diff --check`: passed. No Docker runtime or Helm RBAC changes.

The shared `.venv` is a symlink to the main checkout's environment and lacks
editable source paths. Commands used this local-only environment prefix:

```sh
export PYTHONPATH=src:agent/performer/src:packages/service_inference/src:packages/routing_models/src:packages/ci_detection/src
```

Pytest commands also used
`env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW`.
Initial collection failures from absent editable paths were resolved with this
prefix; daemon and performer test roots were run separately because both define
`tests.conftest`. No dependency installation was required.

## Garden infra handoff

These steps are for the infra agent, and were not executed. Assumptions below:
release, StatefulSet and daemon namespace `coordinare`; endpoint ID
`claude-ephemeral`; default Helm state path. Substitute the actual endpoint ID,
performer namespace and paths before running. Deploy a daemon image containing
this change first. The existing performer image must already contain spec 556's
translate route; it needs no routing-specific rebuild.

1. Prepare `routing.yaml` using the translate example in
   `docs/operators/litellm-proxy.md`. Set the exact dispatched model string,
   gateway URL and optional `upstream_model`. Keep
   `upstream_auth_env: ANTHROPIC_AUTH_TOKEN`; the existing catalog endpoint's
   `auth_env` supplies that bearer in the job. Keep credentials out of ConfigMaps.

2. Create the routing ConfigMap in the performer namespace:

   ```sh
   NS=coordinare
   PNS=coordinare  # set to kubernetes_namespace if performers run elsewhere
   ENDPOINT_ID=claude-ephemeral
   kubectl -n "$PNS" create configmap coordinare-performer-routing \
     --from-file=routing.yaml=./routing.yaml --dry-run=client -o yaml | kubectl apply -f -
   ```

3. Run `kubectl -n "$NS" edit configmap coordinare`. In its `config.yaml`,
   add the config block above under the existing endpoint serving `assessor`.
   Preserve its image, roles, other env and cache settings. Remove its old
   `env.SELFHOSTED_ROUTING_CONFIG` and routing host-path mount. Keep the same
   endpoint block in the Helm values source under `config.performer_endpoints`.

4. With Helm's default persistent configuration, the ConfigMap is only a seed.
   After current work drains, also update the live config. This command edits
   only the chosen endpoint, validates a candidate with the pod's existing env,
   and saves a backup before replacing the live file. Avoid concurrent dashboard
   saves during this operation:

   ```sh
   kubectl -n "$NS" exec -i coordinare-0 -c coordinare -- python - "$ENDPOINT_ID" <<'PY'
   import subprocess
   import sys
   from datetime import datetime, timezone
   from pathlib import Path
   import yaml

   path = Path('/var/lib/coordinare/config/config.yaml')
   original = path.read_bytes()
   config = yaml.safe_load(original)
   matches = [e for e in config['performer_endpoints'] if e['id'] == sys.argv[1]]
   assert len(matches) == 1, 'Expected exactly one matching endpoint'
   endpoint = matches[0]
   assert endpoint['mode'] == 'ephemeral' and 'assessor' in endpoint['roles']
   endpoint['kubernetes_routing_configmap'] = {
       'name': 'coordinare-performer-routing', 'key': 'routing.yaml',
   }
   endpoint.get('env', {}).pop('SELFHOSTED_ROUTING_CONFIG', None)
   candidate = path.with_name('config.routing-candidate.yaml')
   candidate.write_text(yaml.safe_dump(config, sort_keys=False))
   subprocess.run([
       sys.executable, '-m', 'coordinare', 'config', 'validate', '--config', str(candidate),
   ], check=True)
   assert path.read_bytes() == original, 'Config changed during validation; retry'
   stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
   path.with_name(f'config.before-routing.{stamp}.yaml').write_bytes(original)
   candidate.replace(path)
   PY
   ```

   For ConfigMap-only deployments, skip this live-file edit and validate
   `/etc/coordinare/config.yaml` after the ConfigMap update propagates:

   ```sh
   kubectl -n "$NS" exec coordinare-0 -c coordinare -- \
     python -m coordinare config validate --config /etc/coordinare/config.yaml
   ```

5. Restart the daemon and dispatch a fresh assessor:

   ```sh
   kubectl -n "$NS" rollout restart statefulset/coordinare
   kubectl -n "$NS" rollout status statefulset/coordinare
   ```

6. For the new performer pod, inspect `kubectl -n "$PNS" get pod <pod> -o yaml`
   for `selfhosted-routing`, its ConfigMap key, read-only mount and env path.
   Validate the mounted file while the pod is alive:

   ```sh
   kubectl -n "$PNS" exec <pod> -- python -c \
     'from performer.proxy.routing import RoutingTable; RoutingTable.from_yaml_file("/etc/coordinare/routing/routing.yaml")'
   ```

   Confirm `selfhosted_layer.translate_launched` in performer logs and upstream
   `/v1/chat/completions` traffic. Missing ConfigMap/key causes pod mount errors;
   use `kubectl -n "$PNS" describe pod <pod>`. Existing jobs do not reload routing.
   The daemon's config validator checks the endpoint setting, not routing YAML.
