{{/* Chart name, overridable. */}}
{{- define "coordinare.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully-qualified release name. */}}
{{- define "coordinare.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "coordinare.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "coordinare.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "coordinare.labels" -}}
app.kubernetes.io/name: {{ include "coordinare.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: coordinare
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "coordinare.selectorLabels" -}}
app.kubernetes.io/name: {{ include "coordinare.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Labels that never change with a release.

Kubernetes forbids updates to a StatefulSet's volumeClaimTemplates metadata,
and the claim's only mutable-by-design fields live in its spec, not its
metadata. Anything derived from chart metadata (app.kubernetes.io/version,
helm.sh/chart) therefore rejects every upgrade before the spec is even
compared. The version label belongs on the Pod template, which rolls freely.
*/}}
{{- define "coordinare.stableLabels" -}}
{{- include "coordinare.selectorLabels" . }}
app.kubernetes.io/managed-by: coordinare
{{- end -}}

{{- define "coordinare.serviceAccountName" -}}
{{- default (include "coordinare.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}

{{/*
The Service's in-cluster DNS name.

Single source for both the Service object and the trusted-host entry that makes
it usable. If these two ever disagree the dashboard returns 403 for a reason
nobody can see, so they are derived rather than written twice.
*/}}
{{- define "coordinare.serviceDNS" -}}
{{- printf "%s.%s.svc.cluster.local" (include "coordinare.fullname" .) .Release.Namespace -}}
{{- end -}}

{{/*
Trusted dashboard hosts.

Spec 144 leaves this list empty by default: the guard refuses any request whose
Host is not loopback. The chart adds the Service's own DNS name and the short
forms Kubernetes resolves it by, because otherwise the Service it ships would
403 on every request.

Deliberately narrow: exact hostnames only. A wildcard here would hand the guard
away entirely, and a test asserts none appears.
*/}}
{{- define "coordinare.trustedHosts" -}}
{{- $auto := list (include "coordinare.serviceDNS" .) (printf "%s.%s" (include "coordinare.fullname" .) .Release.Namespace) (include "coordinare.fullname" .) -}}
{{- concat $auto (.Values.dashboard.trustedHosts | default list) | uniq | toYaml -}}
{{- end -}}

{{/*
The replica guard.

Coordinare's state is a single-process JSON snapshot (state_store.py). A second
replica does not share that state — it races on the same file and corrupts it.
So this is a correctness constraint, not a tuning default, and the render fails
rather than obeying.

Failing here rather than documenting it is deliberate: `--set replicaCount=3` is
exactly what an operator types when they assume coordinare scales horizontally,
and this is the one moment they are guaranteed to read the reason.
*/}}
{{- define "coordinare.checkReplicas" -}}
{{- if and (ne (int .Values.replicaCount) 1) (not .Values.allowUnsafeMultiReplica) -}}
{{- fail (printf "\n\nreplicaCount is %d, but coordinare supports exactly 1.\n\nCoordinare's state is a single-process JSON snapshot on one volume. A second\nreplica does not divide the work; it races on that file and will corrupt or\nlose state. This is a correctness constraint, not a performance default.\n\nIf you understand that and still need it, set allowUnsafeMultiReplica=true.\n" (int .Values.replicaCount)) -}}
{{- end -}}
{{- end -}}

{{/*
The namespace performer Pods run in. It is the only namespace a devenv claim can
live in, because a PersistentVolumeClaim is namespaced and a Pod mounts claims
from its own namespace. Single source for the performer RBAC, the daemon's
kubernetes_namespace, the devenv claim and the devenv PVC.
*/}}
{{- define "coordinare.performerNamespace" -}}
{{- .Values.performers.namespace | default .Release.Namespace -}}
{{- end -}}

{{/*
How the performer environment cache is provided: "create", "wire" or "off".

devenvCache.create is a TRI-STATE. Helm cannot tell an unset value from a
default of true, so the default is null, and this helper resolves it:

  enabled  create (explicit)  existingClaim / performers.cacheClaim  mode
  false    -                  -                                      off
  true     true               -                                      create
  true     false              -                                      wire (and FAIL if neither claim is named)
  true     unset (null)       set                                    wire  (the migration path for performers.cacheClaim)
  true     unset (null)       none                                   create

A pre-existing install that set only performers.cacheClaim therefore keeps
working: it resolves to wire, the same claim, and no PVC is rendered.
*/}}
{{- define "coordinare.devenvMode" -}}
{{- $c := .Values.devenvCache -}}
{{- $legacy := .Values.performers.cacheClaim | default "" -}}
{{- if not $c.enabled -}}
off
{{- else if kindIs "bool" $c.create -}}
{{- if $c.create -}}create{{- else -}}wire{{- end -}}
{{- else if or $c.existingClaim $legacy -}}
wire
{{- else -}}
create
{{- end -}}
{{- end -}}

{{/*
The name of the performer environment cache claim, or "" when the cache is off.

The one place that decides which claim performers mount, and so the one place
that fails on a genuine contradiction. A claim name guessed wrong binds
performers to a different volume than the one the operator meant, so every
ambiguous case fails rather than picking one.

Fails:
  - enabled false together with performers.cacheClaim
  - create set to true explicitly together with existingClaim or performers.cacheClaim
  - create set to false explicitly with no claim named
  - existingClaim and performers.cacheClaim naming different claims
  - devenvCache.namespace that differs from performers.namespace
  - devenvCache.create unset/true with an empty devenvCache.name

Not a failure: performers.cacheClaim alone. It is accepted, resolves to the same
claim with no PVC rendered, and NOTES.txt says how to migrate.
*/}}
{{- define "coordinare.devenvClaim" -}}
{{- $c := .Values.devenvCache -}}
{{- $legacy := .Values.performers.cacheClaim | default "" -}}
{{- $existing := $c.existingClaim | default "" -}}
{{- $ns := include "coordinare.performerNamespace" . -}}
{{- $mode := include "coordinare.devenvMode" . -}}
{{- if and $c.namespace (ne $c.namespace $ns) -}}
{{- fail (printf "\n\ndevenvCache.namespace is %q, but performers run in %q.\n\nA PersistentVolumeClaim is namespaced, so performer Pods can mount a devenv claim only from their own namespace. Set devenvCache.namespace to %q, or leave it empty." $c.namespace $ns $ns) -}}
{{- end -}}
{{- if eq $mode "off" -}}
{{- if $legacy -}}
{{- fail "\n\nperformers.cacheClaim is set, but devenvCache.enabled is false. Those two contradict each other. Remove performers.cacheClaim, or set devenvCache.enabled to true." -}}
{{- end -}}
{{- if $existing -}}
{{- fail "\n\ndevenvCache.existingClaim is set, but devenvCache.enabled is false, so no claim would be wired and the name would be silently ignored. Remove existingClaim, or set devenvCache.enabled to true." -}}
{{- end -}}
{{- if eq (toString $c.create) "true" -}}
{{- fail "\n\ndevenvCache.create is true, but devenvCache.enabled is false, so the claim would never be created. Set enabled to true, or clear create." -}}
{{- end -}}
{{- else if eq $mode "create" -}}
{{- if $c.existingClaim -}}
{{- fail "\n\ndevenvCache.existingClaim is set, but devenvCache.create is true, so the chart would create devenvCache.name and ignore the existing claim. To use a claim you already have, set devenvCache.create to false. To have the chart create one, clear existingClaim." -}}
{{- end -}}
{{- if and (kindIs "bool" $c.create) $legacy -}}
{{- fail "\n\nperformers.cacheClaim names an existing claim, but devenvCache.create is set to true, so the chart would also create devenvCache.name. Clear performers.cacheClaim, or set devenvCache.create to false (or remove the explicit create setting, which lets the migration path wire the claim)." -}}
{{- end -}}
{{- required "devenvCache.name must be set when the chart creates the claim" $c.name -}}
{{- else -}}
{{- if and $existing $legacy (ne $existing $legacy) -}}
{{- fail (printf "\n\ndevenvCache.existingClaim is %q and performers.cacheClaim is %q. Keep only devenvCache.existingClaim, and remove performers.cacheClaim." $existing $legacy) -}}
{{- end -}}
{{- if not (or $existing $legacy) -}}
{{- fail "\n\ndevenvCache.create is false, so the chart needs the name of a claim that already exists. Set devenvCache.existingClaim to it. The claim must be in the performer namespace. Or set devenvCache.create to true to have the chart create one." -}}
{{- end -}}
{{- $existing | default $legacy -}}
{{- end -}}
{{- end -}}
