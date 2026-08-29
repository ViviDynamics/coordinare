# Quickstart: Coordinare on Kubernetes via Helm

## Install

```bash
kubectl create namespace coordinare

kubectl create secret generic coordinare-credentials \
  --namespace coordinare \
  --from-literal=GITHUB_TOKEN=... \
  --from-literal=OPENAI_API_KEY=...

helm install coordinare deploy/helm/coordinare \
  --namespace coordinare \
  --set existingSecret=coordinare-credentials \
  --values my-values.yaml
```

`my-values.yaml` carries your board and model configuration. It must contain no credentials —
the config references environment-variable *names*, and the Secret supplies the values.

## Verify

```bash
kubectl -n coordinare get statefulset,pod,pvc
kubectl -n coordinare logs statefulset/coordinare -f
```

The controller is Ready when its readiness endpoint answers. One replica is expected and is not a
misconfiguration.

## Reach the dashboard

```bash
kubectl -n coordinare port-forward svc/coordinare 8090:8090
# http://127.0.0.1:8090
```

The in-cluster Service name also works, because the chart adds exactly that hostname to
`dashboard.trustedHosts`. **The dashboard has no authentication** — anyone who can reach it can
change configuration and cancel work. Nothing is exposed outside the cluster by default, and it
should stay that way until spec 143 adds authentication.

## Upgrade

```bash
helm upgrade coordinare deploy/helm/coordinare --namespace coordinare --reuse-values
```

The state volume is retained: the claim belongs to the StatefulSet's identity, so the rescheduled
Pod reattaches the same volume and resumes from the existing snapshot.

## Uninstall

```bash
helm uninstall coordinare --namespace coordinare
```

**The state claim is not removed with the release.** That is deliberate — an uninstall should not
destroy work. Delete it explicitly when you mean to:

```bash
# Scoped to THIS release. `app.kubernetes.io/name=coordinare` would match every
# coordinare release in the namespace and delete their state too.
kubectl -n coordinare delete pvc -l app.kubernetes.io/instance=coordinare
```

## No cluster to hand?

```bash
kind create cluster --name coordinare-dev
```

The integration tests skip, rather than fail, when no cluster is reachable, and their skip message
names this command.
