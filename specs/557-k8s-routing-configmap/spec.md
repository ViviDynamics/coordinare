# 557: Kubernetes performer routing ConfigMap

Issue 223 follow-up: operators can supply the spec-556 routing table to performer
pods without a derived image. Work stays local on branch 223-k8s-routing-configmap.

## Requirements and acceptance

1. Each endpoint may opt into `kubernetes_routing_configmap: {name, key}`.
   Default is null; key defaults to `routing.yaml`. Validate name as a Kubernetes
   DNS-1123 subdomain (253 characters maximum), and key as a ConfigMap data key.
   Subprocess endpoints reject the setting, like other container settings.
2. Kubernetes projects only that key as `routing.yaml` in a read-only directory
   at `/etc/coordinare/routing`. It sets `SELFHOSTED_ROUTING_CONFIG` to
   `/etc/coordinare/routing/routing.yaml`, overriding endpoint env for that key.
   Missing ConfigMaps/keys prevent pod startup through normal Kubernetes handling.
3. No setting means unchanged pod mounts and environment. Existing cache mounts,
   other endpoint env, Docker host-path handling and verifier pods stay unchanged.
4. Reference an externally managed ConfigMap in the performer namespace. No new
   daemon API call or RBAC permission. Helm passes the endpoint setting through;
   chart-managed routing ConfigMap creation is outside this change.
5. Update the main example, config reference and operator guide with garden steps,
   authentication, validation, replacement-pod behavior and derived-image alternative.

Budget: zero additional Kubernetes API calls during dispatch, no routing-file I/O
in the daemon, one volume and mount per configured pod. Verify through fake-API
tests. Run relevant unit and Helm suites, lint, mypy and example CLI validation.
